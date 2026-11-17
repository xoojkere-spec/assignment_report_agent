import json

import pytest
from test_grounded_writing import automatic_state
from test_research import research_network as research_network

from assignment_agent.fake_model import FakeModel
from assignment_agent.grounded_writing import AutomaticWriting, EvidenceFailure
from assignment_agent.paper_figures import save_asset
from assignment_agent.research import ResearchCollector, source_id
from assignment_agent.research_budget import SearchBudget
from assignment_agent.research_store import ResearchStore, context_hash
from assignment_agent.research_types import Paper, ResearchError
from assignment_agent.schemas import Artifact, AssignmentSpec, RunState


class EmptySearch:
    providers = ("web",)

    def search(self, provider, query):
        return []


class PageFetcher:
    body = "Current documented server support and deployment constraints. " * 12

    def get(self, url, **kwargs):
        raw = (
            '<html><head><meta name="citation_title" content="Server requirements">'
            '<meta name="citation_author" content="Actual Author">'
            '<meta name="citation_publication_date" content="2025-01-10"></head>'
            f"<body><article><h1>Server requirements</h1><p>{self.body}</p></article></body></html>"
        ).encode()
        return url, "text/html", raw


def prepare(tmp_path):
    run = RunState(
        spec=AssignmentSpec(title="Server choices", sections=["Analysis"], word_limit=900),
        automatic_research=True,
    )
    store = ResearchStore(tmp_path / "agent.sqlite")
    paper = Paper(
        title="Server requirements",
        authors=["Actual Author"],
        year=2025,
        url="https://publisher.example/server",
        provider="web",
    )
    source = {
        "id": source_id(paper),
        "paper": paper.model_dump(),
        "title": paper.title,
        "authors": paper.authors,
        "year": paper.year,
        "url": paper.url,
        "verified": False,
    }
    store.save_run("run", context_hash(run), status="writing")
    collector = ResearchCollector(store, fetcher=PageFetcher(), search=EmptySearch())
    collector.inspect_source("run", context_hash(run), source)
    source = store.entries("run", context_hash(run), "source")[0]
    store.writing_event(
        "run",
        context_hash(run),
        "section-1",
        2,
        status="passed",
        evidence={
            "sources": {
                source["id"]: {"asset": source["page_asset"], "text_asset": source["text_asset"]}
            }
        },
    )
    return run, store, source


@pytest.mark.parametrize("stage", ["check", "refresh"])
@pytest.mark.parametrize("persistent", [False, True])
def test_known_source_retries_one_403_without_search(tmp_path, stage, persistent):
    run, store, old = prepare(tmp_path)
    requests = []

    class Flaky(PageFetcher):
        def get(self, url, **kwargs):
            requests.append(url)
            if persistent or len(requests) == 1:
                raise ResearchError("公开资料服务返回 HTTP 403")
            return super().get(url, **kwargs)

    digest = context_hash(run)
    if stage == "check":
        pipeline = AutomaticWriting(store.database, FakeModel(), fetcher_factory=Flaky)
        if persistent:
            with pytest.raises(EvidenceFailure, match="HTTP 403"):
                pipeline.check_links("run", run, [old["id"]])
        else:
            pipeline.check_links("run", run, [old["id"]])
    else:
        stale = {**old, "verified": False}
        collector = ResearchCollector(store, fetcher=Flaky(), search=EmptySearch())
        issue = collector.refresh_source("run", digest, stale)
        assert bool(issue) == persistent
    latest = store.entries("run", digest, "source")[0]
    assert latest["verified"] == (not persistent)
    if not persistent:
        assert latest["text_asset"] == old["text_asset"]
    assert requests == [old["url"], old["url"]]
    assert SearchBudget(store, "run", chapter=1).usage()["total_used"] == 0


@pytest.mark.parametrize("manual_reject", [False, True])
def test_changed_used_web_source_is_refetched_without_search_rediscovery(tmp_path, manual_reject):
    run, store, old = prepare(tmp_path)

    class Changed(PageFetcher):
        body = "Updated current server hardware support and deployment constraints. " * 12

    pipeline = AutomaticWriting(store.database, FakeModel(), fetcher_factory=Changed)
    with pytest.raises(EvidenceFailure, match="正文已变化"):
        pipeline.check_links("run", run, [old["id"]])
    invalid = store.entries("run", context_hash(run), "source")[0]
    invalid.update(retired=True, review={"action": "reject"} if manual_reject else None)
    store.save("run", context_hash(run), "source", invalid)
    collector = ResearchCollector(store, fetcher=Changed(), search=EmptySearch())
    collector.collect("run", run, ["server requirements"], refresh=True)
    fresh = store.entries("run", context_hash(run), "source")[0]
    if manual_reject:
        assert not fresh["verified"] and fresh["review"]["action"] == "reject"
        assert fresh["retired"]
    else:
        assert fresh["verified"] and not fresh["retired"]
        assert fresh["automatic_review"] is None
        assert fresh["text_asset"] != old["text_asset"]
        body = json.loads((store.assets / fresh["text_asset"]["filename"]).read_bytes())[0]["text"]
        assert "Updated current server" in body
        with store.connect() as connection:
            events = [
                json.loads(row[0])["code"]
                for row in connection.execute("SELECT data FROM research_events ORDER BY id")
            ]
        assert "source.refreshed" in events
        assert not store.accepted(fresh)


def test_prior_sources_are_scoped_to_run_context_and_latest_passed_chapter(tmp_path):
    _, store, old = prepare(tmp_path)
    digest = store.run("run")["context_hash"]
    store.writing_event("run", digest, "section-1", 3, status="failed")
    assert store.passed_source_ids("run", digest, "section-1") == [old["id"]]
    store.writing_event(
        "run", digest, "section-1", 4, status="passed", evidence={"sources": {"new-source": {}}}
    )
    store.writing_event(
        "other-run",
        digest,
        "section-1",
        5,
        status="passed",
        evidence={"sources": {"wrong-run": {}}},
    )
    assert store.passed_source_ids("run", digest, "section-1") == ["new-source"]
    assert store.passed_source_ids("run", "other-context") == []
    assert store.passed_source_ids("run", digest, "section-2") == []


def test_sufficient_inventory_still_refetches_stale_current_chapter_source(
    tmp_path, research_network
):
    run, store, old = prepare(tmp_path)
    _, _, factory = research_network
    ResearchCollector(store, fetcher=factory()).collect("run", run, ["Other evidence"])
    fetched = []

    class Changed(PageFetcher):
        body = "Updated current server hardware support and deployment constraints. " * 12

        def get(self, url, **kwargs):
            assert url == old["url"]
            fetched.append(url)
            return super().get(url, **kwargs)

    class NoSearch(FakeModel):
        def research_plan(self, state, targets=None):
            pytest.fail("重新取证应直接访问旧来源网址")

    pipeline = AutomaticWriting(store.database, NoSearch(), fetcher_factory=Changed)
    with pytest.raises(EvidenceFailure, match="正文已变化"):
        pipeline.check_links("run", run, [old["id"]])
    stale_pool = pipeline.pool("run", run)
    assert old["id"] not in {source["id"] for source in stale_pool["sources"]}
    assert pipeline.inventory_counts(stale_pool)[1:] == (0, 0)

    budget = SearchBudget(store, "run", chapter=1)
    pool = pipeline.prepare_evidence("run", run, budget, [], attempt=2)
    current = next(source for source in pool["sources"] if source["id"] == old["id"])
    assert any("Updated current server" in passage["text"] for passage in current["passages"])
    assert fetched == [old["url"], old["url"]]
    assert budget.usage()["chapter_used"] == budget.usage()["total_used"] == 0


def test_final_audit_rejects_old_draft_after_source_text_refresh(tmp_path, research_network):
    _, _, factory = research_network
    run = automatic_state()
    pipeline = AutomaticWriting(tmp_path / "agent.sqlite", FakeModel(), fetcher_factory=factory)
    content, bundle = pipeline.write("pdf", run, {}, 1)
    run.drafts = [Artifact(artifact_id="section-1", version=1, content=content)]
    run.grounded_drafts = {"section-1": bundle}
    store = pipeline.store
    assert pipeline.audit("pdf", run, {}) == (None, [])
    assert store.reference_checks("pdf", context_hash(run))
    current = store.entries("pdf", context_hash(run), "source")[0]
    current["text_asset"] = save_asset(
        store.assets, b'[{"page":1,"text":"Updated source"}]', "json"
    )
    store.save("pdf", context_hash(run), "source", current)
    index, issues = pipeline.audit("pdf", run, {})
    assert index == 0 and any("来源快照已更新" in issue for issue in issues)
