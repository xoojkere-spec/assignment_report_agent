from types import SimpleNamespace

import httpx
import pytest
from test_research import research_network as research_network

from assignment_agent.fake_model import FakeModel
from assignment_agent.grounded_writing import AutomaticWriting, authors
from assignment_agent.public_fetch import PublicFetcher
from assignment_agent.research import ResearchCollector
from assignment_agent.research_search import openalex_paper, plain
from assignment_agent.research_store import ResearchStore, context_hash
from assignment_agent.research_types import ResearchError
from assignment_agent.schemas import AssignmentSpec, OutlinePlan, RunState, SectionPlan


def state():
    return RunState(
        spec=AssignmentSpec(
            title="Kernel architecture", sections=["Analysis", "Conclusion"], word_limit=900
        )
    )


def test_openalex_normalizes_doi_and_repository_locations():
    paper = openalex_paper(
        {
            "id": "https://openalex.org/W1",
            "title": "&lt;div&gt;Kernel architecture&lt;/div&gt;",
            "doi": "10.1234/paper0",
            "publication_year": 2024,
            "authorships": [{"author": {"display_name": "Alice Researcher -"}}],
            "open_access": {"is_oa": True},
            "locations": [
                {"pdf_url": "https://publisher.example/paper.pdf", "source": {"type": "journal"}},
                {
                    "pdf_url": "https://repository.example/paper.pdf",
                    "source": {"type": "repository"},
                },
            ],
            "abstract_inverted_index": {"kernel": [1], "A": [0]},
        }
    )
    assert paper.url == "https://doi.org/10.1234/paper0"
    assert paper.title == "Kernel architecture"
    assert paper.pdf_urls[0] == "https://repository.example/paper.pdf"
    assert paper.abstract == "A kernel"
    assert paper.authors == ["Alice Researcher"]
    assert plain("&lt;span&gt;Title&amp;nbsp;text&lt;/span&gt;") == "Title text"


def test_citation_author_never_guesses_family_name_from_word_order():
    assert (
        authors({"authors": ["Thallapally Abhilash"], "paper": {"family_names": [None]}})
        == "Thallapally Abhilash"
    )
    assert (
        authors({"authors": ["Alice Researcher"], "paper": {"family_names": ["Researcher"]}})
        == "Researcher"
    )


def test_rate_limited_provider_is_not_repeated_for_every_query(tmp_path, research_network):
    calls, flags, factory = research_network
    flags["s2_error"] = True
    ResearchCollector(ResearchStore(tmp_path / "agent.sqlite"), fetcher=factory()).collect(
        "probe", state(), ["kernel architecture", "kernel performance"]
    )
    assert sum(call.headers["host"] == "api.semanticscholar.org" for call in calls) == 1
    assert any(call.headers["host"] == "api.openalex.org" for call in calls)


def test_official_web_queries_are_not_sent_to_academic_indexes(tmp_path, research_network):
    calls, _, factory = research_network
    ResearchCollector(ResearchStore(tmp_path / "agent.sqlite"), fetcher=factory()).collect(
        "queries",
        state(),
        ["kernel research"],
        web_queries=["Windows Server site:learn.microsoft.com"],
    )
    web_calls = [r for r in calls if r.headers["host"] == "html.duckduckgo.com"]
    queries = [r.url.params["q"] for r in web_calls]
    assert "Windows Server site:learn.microsoft.com" in queries
    assert "Windows Server" in queries
    assert any("site:techcommunity.microsoft.com" in q for q in queries)
    for call in calls:
        if call.headers["host"] in {
            "api.openalex.org",
            "api.crossref.org",
            "api.semanticscholar.org",
        }:
            assert "site%3A" not in str(call.url) and "site:" not in str(call.url)


def test_web_cooldown_survives_collector_recreation(tmp_path, research_network):
    calls, flags, factory = research_network
    flags["web_limit"] = True
    store = ResearchStore(tmp_path / "agent.sqlite")
    for query in ["kernel research", "kernel isolation"]:
        ResearchCollector(store, fetcher=factory()).collect(
            "cooldown", state(), [query], refresh=True
        )
    assert sum(r.headers["host"] == "html.duckduckgo.com" for r in calls) == 1
    assert store.run("cooldown")["web_cooldowns"]["duckduckgo"] > 0
    assert any(
        e["code"] == "search.free_engine" and e.get("status") == "cooldown"
        for e in store.summary("cooldown", state())["events"]
    )


def test_metadata_queries_do_not_consume_pdf_download_budget(tmp_path, research_network):
    _, _, factory = research_network
    result = ResearchCollector(
        ResearchStore(tmp_path / "agent.sqlite"), fetcher=factory(budget=6)
    ).collect("probe", state(), ["kernel architecture", "kernel performance", "kernel isolation"])
    assert sum(bool(item.get("verified")) for item in result["sources"]) == 3
    assert len(result["figures"]) == 3


def test_source_selection_rejects_unknown_ids_before_downloading(tmp_path, research_network):
    calls, _, factory = research_network
    with pytest.raises(ResearchError, match="不存在的来源编号"):
        ResearchCollector(ResearchStore(tmp_path / "agent.sqlite"), fetcher=factory()).collect(
            "probe", state(), ["kernel"], ranker=lambda sources: ["src-" + "0" * 24]
        )
    assert not any(call.url.path.endswith(".pdf") for call in calls)


def test_figures_follow_outline_diagram_needs_and_conclusion_reuses_sources(tmp_path):
    names = [
        "Introduction",
        "Operating system choices",
        "Architecture designs",
        "Comparison",
        "Implementation",
        "Conclusion",
    ]
    run = RunState(
        spec=AssignmentSpec(title="Kernel architectures", sections=names, word_limit=2500),
        outline_plan=OutlinePlan(
            parts=[
                SectionPlan(
                    title=name,
                    word_budget=100,
                    purpose="Explain the topic",
                    evidence_needs=["Two diagrams for the two operating systems"]
                    if index == 2
                    else ["Reliable source"],
                )
                for index, name in enumerate(names)
            ]
        ),
    )
    pipeline = AutomaticWriting(tmp_path / "agent.sqlite", FakeModel())
    first = pipeline.targets(run)
    assert first["new_sources"] == 0
    assert first["new_figures"] == 0
    run.current_index = 2
    assert pipeline.targets(run)["new_figures"] == 2
    run.current_index = 4
    assert pipeline.targets(run)["new_sources"] == 3
    run.current_index = 5
    target = pipeline.targets(run)
    assert target["section_role"] == "conclusion"
    assert target["new_sources"] == 0 and target["new_figures"] == 0


def test_download_has_absolute_deadline_even_when_chunks_keep_arriving(monkeypatch):
    from assignment_agent import public_fetch

    ticks = iter([0, 0, 41])
    monkeypatch.setattr(public_fetch, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    fetcher = PublicFetcher(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"slow content")),
        resolver=lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    with pytest.raises(ResearchError, match="总耗时上限"):
        fetcher.get("https://papers.example/slow.pdf")


def test_retry_preserves_verified_full_text_without_repeated_search_hits(
    tmp_path, research_network
):
    _, _, factory = research_network
    store = ResearchStore(tmp_path / "agent.sqlite")
    collector = ResearchCollector(store, fetcher=factory())
    run = state()
    initial = collector.collect("probe", run, ["kernel architecture"])
    source_ids = {item["id"] for item in initial["sources"] if item.get("verified")}
    assert len(source_ids) == 3

    class EmptySearch:
        providers = ("empty",)

        def search(self, provider, query):
            return []

    retried = ResearchCollector(store, fetcher=factory(), search=EmptySearch()).collect(
        "probe", run, ["new specific query"], refresh=True, ranker=lambda sources: []
    )
    assert {
        item["id"]
        for item in retried["sources"]
        if item.get("verified") and not item.get("retired") and item["integrity_ok"]
    } == source_ids
    # 被破坏的证据不能因为曾通过元数据核验而继续占用有效名额。
    damaged = store.entries("probe", context_hash(run), "source")[0]
    (store.assets / damaged["text_asset"]["filename"]).write_text("changed", encoding="utf-8")
    assert not collector.reusable(damaged)
