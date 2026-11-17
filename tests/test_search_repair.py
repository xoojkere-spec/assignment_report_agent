import json

import pytest
from test_research import research_network as research_network

from assignment_agent.fake_model import FakeModel
from assignment_agent.free_search import FreeWebSearch
from assignment_agent.grounded_writing import AutomaticWriting
from assignment_agent.paper_figures import save_asset
from assignment_agent.research import ResearchCollector
from assignment_agent.research_store import ResearchStore, context_hash
from assignment_agent.research_types import WebCandidate
from assignment_agent.schemas import AssignmentSpec, RunState
from assignment_agent.web_sources import IncompleteBody, page_text


def test_nonempty_first_engine_does_not_hide_other_evidence_and_site_is_strict():
    class Network:
        def get(self, url, **kwargs):
            if "duckduckgo" in url:
                raw = (
                    b'<html><a class="result__a" href="https://a.example/article">'
                    b"Windows NT microkernel</a></html>"
                )
            else:
                raw = b"<rss><channel><item><title>Windows NT microkernel</title><link>https://b.example/article</link></item></channel></rss>"
            return url, "text/html", raw

        def json(self, url, params=None):
            return {"results": []}

    search = FreeWebSearch(Network())
    found = search.search("site:learn.microsoft.com Windows NT microkernel")
    assert {p.url for p in found} == {"https://a.example/article", "https://b.example/article"}
    scoped = [e for e in search.events if "site:learn.microsoft.com" in e["query"]]
    assert all(e.get("results", 0) == 0 for e in scoped)
    assert any(e["query"] == "Windows NT microkernel" for e in search.events)
    assert any("site:techcommunity.microsoft.com" in e["query"] for e in search.events)
    assert not search.matches_scope(
        "site:learn.microsoft.com", "https://learn.microsoft.com.evil.test/"
    )
    assert not search.matches_scope("-site:example.com", "https://docs.example.com/")


def test_placeholder_is_not_evidence_and_article_is_preferred_over_navigation():
    with pytest.raises(IncompleteBody):
        page_text(b"<html><main>Loading...</main></html>", require_body=True)
    body = (
        "The core Kernel implements low level mechanisms "
        "while the Executive remains in kernel mode. " * 2
    )
    raw = f"<html><main>Loading...</main><article>{body}</article></html>".encode()
    assert page_text(raw, require_body=True) == body.strip()


def test_metadata_only_and_captcha_cannot_masquerade_as_full_body():
    for raw in [
        (
            '<html><head><script type="application/ld+json">'
            + json.dumps({"description": "Kernel details. " * 100})
            + "</script></head></html>"
        ).encode(),
        (
            "<html><head><title>Captcha</title></head><main>"
            + "Navigation words " * 100
            + "</main></html>"
        ).encode(),
    ]:
        with pytest.raises(IncompleteBody):
            page_text(raw, require_body=True)


def test_real_javascript_article_is_not_mistaken_for_loading_screen():
    text = (
        "To enable JavaScript, change this setting. "
        + "The article explains runtime behavior in detail. " * 30
    ).strip()
    assert page_text(f"<html><article>{text}</article></html>".encode(), require_body=True) == text


@pytest.mark.parametrize("always_placeholder", [False, True])
def test_incomplete_body_is_retried_once_before_verification(tmp_path, always_placeholder):
    class Search:
        providers = ()

    class Fetcher:
        calls = 0

        def get(self, url, **kwargs):
            self.calls += 1
            body = (
                "Loading..."
                if self.calls == 1 or always_placeholder
                else "Real evidence about system service boundaries and kernel execution modes. "
                * 3
            )
            raw = (
                '<html><head><meta name="citation_title" content="Kernel architecture">'
                '<meta name="citation_author" content="Actual Author">'
                '<meta name="citation_publication_date" content="2024"></head>'
                f"<main>{body}</main></html>"
            ).encode()
            return url, "text/html", raw

    fetcher = Fetcher()
    state = RunState(spec=AssignmentSpec(title="Kernel", sections=["Introduction"], word_limit=900))
    result = ResearchCollector(
        ResearchStore(tmp_path / "agent.sqlite"), fetcher=fetcher, search=Search()
    ).collect(
        "retry",
        state,
        ["kernel"],
        model_candidates=[
            WebCandidate(title="Kernel architecture", url="https://publisher.example/article")
        ],
    )
    assert fetcher.calls == 2
    assert bool(result["sources"][0]["verified"]) is (not always_placeholder)
    assert any(e["code"] == "source.body_retry" for e in result["events"])


def test_evidence_gap_brings_later_relevant_page_into_pool(tmp_path, research_network):
    _, _, factory = research_network
    state = RunState(
        spec=AssignmentSpec(title="Kernel architecture", sections=["Introduction"], word_limit=900)
    )
    store = ResearchStore(tmp_path / "agent.sqlite")
    ResearchCollector(store, fetcher=factory()).collect("gap", state, ["kernel"])
    source = store.entries("gap", context_hash(state), "source")[0]
    pages = [
        {"page": i, "text": "Kernel architecture generic introduction. " * 80} for i in range(1, 9)
    ]
    pages.append({"page": 9, "text": "Windows NT microkernel Executive protection design. " * 10})
    source["text_asset"] = save_asset(store.assets, json.dumps(pages).encode(), "json")
    store.save("gap", context_hash(state), "source", source)
    state.feedback = "补充Windows NT的microkernel Executive设计特征证据"
    pool = AutomaticWriting(store.database, FakeModel(), fetcher_factory=factory).pool("gap", state)
    chosen = next(p for p in pool["sources"] if p["id"] == source["id"])
    assert chosen["passages"][0]["page"] == 9
