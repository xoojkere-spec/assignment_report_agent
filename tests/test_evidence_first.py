import json

import httpx
import pytest
from test_grounded_writing import automatic_state
from test_research import research_network as research_network
from test_search_budget import mock_fetcher

from assignment_agent.evidence_types import EvidenceGap
from assignment_agent.fake_model import FakeModel
from assignment_agent.free_search import FreeWebSearch
from assignment_agent.grounded_writing import AutomaticWriting, EvidenceFailure
from assignment_agent.paper_figures import save_asset
from assignment_agent.research import ResearchCollector
from assignment_agent.research_budget import SearchBudget
from assignment_agent.research_store import context_hash
from assignment_agent.research_types import ResearchPlan


@pytest.mark.parametrize("kind", ["source", "figure"])
def test_reuse_then_search_only_for_explicit_gap_and_stop_after_first_usable_batch(
    tmp_path, research_network, kind
):
    calls, _, factory = research_network
    run = automatic_state()
    db = tmp_path / "agent.sqlite"
    paper_url = "https://papers.example/paper3.pdf"
    detail = "必答的隔离边界缺少直接来源" if kind == "source" else "必需的隔离边界示意图"
    term = "isolation boundary evidence"
    record = factory().json("https://api.openalex.org/works/https://doi.org/10.1234/paper3")
    searches = []

    def respond(request):
        if request.url.path == "/works":
            searches.append(request)
            assert request.headers["host"] == "api.openalex.org"
            assert request.url.params["search"] == term
            assert ("locations.license" in request.url.params["filter"]) == (kind == "figure")
            return httpx.Response(200, json={"results": [record]})
        return factory().transport.handle_request(request)

    class RequiredGap(FakeModel):
        writes = 0
        planned = []

        def research_plan(self, state, targets):
            assert self.writes == 1
            assert targets["evidence_gaps"] == [{"kind": kind, "detail": detail}]
            assert targets["new_sources"] == targets["new_figures"] == 0
            self.planned.append(targets)
            return ResearchPlan(
                queries=[term] if kind == "source" else ["ignored academic query"],
                figure_queries=[term] if kind == "figure" else [],
            )

        def section_with_evidence(self, state, context, pool):
            self.writes += 1
            assert any(item["pdf_url"] == paper_url for item in pool["sources"]) == (
                self.writes == 2
            )
            selected = {
                **pool,
                "sources": sorted(pool["sources"], key=lambda item: item["pdf_url"] != paper_url),
                "figures": sorted(pool["figures"], key=lambda item: item["pdf_url"] != paper_url),
            }
            return super().section_with_evidence(state, context, selected)

        def verify_section(self, state, context, draft, pool):
            verdict = super().verify_section(state, context, draft, pool)
            if self.writes == 1:
                assert not searches
                verdict.evidence_gaps = [EvidenceGap(kind=kind, detail=detail)]
            return verdict

    model = RequiredGap()
    pipeline = AutomaticWriting(db, model, fetcher_factory=lambda: mock_fetcher(respond))
    ResearchCollector(pipeline.store, fetcher=factory()).collect("run", run, ["seed cache"])
    calls.clear()
    _, bundle = pipeline.write("run", run, {}, 1)
    assert model.writes == 2 and len(model.planned) == len(searches) == 1
    assert SearchBudget(pipeline.store, "run", 1).usage()["total_used"] == 1
    assert any(item["pdf_url"] == paper_url for item in bundle["pool"]["sources"])
    if kind == "figure":
        assert any(
            item["id"] in bundle["figure_ids"] and item["pdf_url"] == paper_url
            for item in bundle["pool"]["figures"]
        )
    assert not any(request.url.path == "/search" for request in calls)


@pytest.mark.parametrize("route", ["resolved_cache", "after_cache", "last_slot"])
def test_semantic_retry_checks_other_passages_in_cached_full_text_first(
    tmp_path, research_network, route
):
    _, _, factory = research_network
    run = automatic_state()
    term = "microkernel isolation boundary"
    missing_term = "preemption latency benchmark"

    class CachedPassage(FakeModel):
        writes = 0
        plans = 0

        def research_plan(self, state, targets=None):
            assert route != "resolved_cache" and self.writes == 2
            assert targets["evidence_gaps"] == [{"kind": "source", "detail": missing_term}]
            self.plans += 1
            return ResearchPlan(queries=[missing_term])

        def section_with_evidence(self, state, context, pool):
            self.writes += 1
            pages = {p["page"] for item in pool["sources"] for p in item["passages"]}
            assert (9 in pages) == (self.writes == 2 and route != "last_slot")
            return super().section_with_evidence(state, context, pool)

        def verify_section(self, state, context, draft, pool):
            verdict = super().verify_section(state, context, draft, pool)
            if self.writes == 1:
                if route == "last_slot":
                    verdict.issues = ["需要校正论断范围"]
                else:
                    verdict.evidence_gaps = [EvidenceGap(kind="source", detail=term)]
            elif route != "resolved_cache":
                verdict.evidence_gaps = [EvidenceGap(kind="source", detail=missing_term)]
            return verdict

    model = CachedPassage()
    pipeline = AutomaticWriting(tmp_path / "agent.sqlite", model, fetcher_factory=factory)
    ResearchCollector(pipeline.store, fetcher=factory()).collect("run", run, ["seed cache"])
    digest = context_hash(run)
    source = pipeline.store.entries("run", digest, "source")[0]
    pages = [
        {"page": index, "text": "Kernel architecture mechanisms are documented on this page. "}
        for index in range(1, 6)
    ]
    pages.append({"page": 9, "text": "The microkernel isolation boundary separates services."})
    pages.append({"page": 10, "text": "Preemption latency benchmark is outside the stated scope."})
    source["text_asset"] = save_asset(pipeline.store.assets, json.dumps(pages).encode(), "json")
    pipeline.store.save("run", digest, "source", source)
    budget = SearchBudget(pipeline.store, "run", 1)
    if route != "resolved_cache":
        with pytest.raises(EvidenceFailure, match=missing_term):
            pipeline.write("run", run, {}, 1)
        assert model.writes == 2 and model.plans == 1
        assert 0 < budget.usage()["total_used"] <= 6
    else:
        _, bundle = pipeline.write("run", run, {}, 1)
        assert model.writes == 2 and not bundle["verdict"]["evidence_gaps"]
        assert budget.usage()["total_used"] == 0


def test_resume_reuses_named_subject_sections_missing_from_previous_review(
    tmp_path, research_network
):
    _, _, factory = research_network
    run = automatic_state()
    db = tmp_path / "agent.sqlite"

    class CompareBoth(FakeModel):
        def research_plan(self, state, targets=None):
            pytest.fail("恢复时应先核对旧稿没有送审的对象小节")

        def section_with_evidence(self, state, context, pool):
            passage_pages = {p["page"] for s in pool["sources"] for p in s["passages"]}
            assert {2, 3} <= passage_pages
            return super().section_with_evidence(state, context, pool)

    pipeline = AutomaticWriting(db, CompareBoth(), fetcher_factory=factory)
    ResearchCollector(pipeline.store, fetcher=factory()).collect("run", run, ["seed cache"])
    digest = context_hash(run)
    source = pipeline.store.entries("run", digest, "source")[0]
    pages = [
        {"page": index, "text": "Current server architecture kernel industry deployment notes."}
        for index in (1, 4, 5, 24)
    ]
    pages += [
        {
            "page": 2,
            "text": "8.1.1\tWindows Server\r\nNamed clients deploy this product on their servers.",
        },
        {"page": 3, "text": "(1) RHEL\nEnterprise tenants maintain this release in production."},
    ]
    source["text_asset"] = save_asset(pipeline.store.assets, json.dumps(pages).encode(), "json")
    pipeline.store.save("run", digest, "source", source)
    previous = pipeline.pool("run", run)
    old_pages = {p["page"] for s in previous["sources"] for p in s["passages"]}
    assert not ({2, 3} & old_pages)
    detail = "请补 Windows Server 和 RHEL 的 current server architecture industry deployment"
    pipeline.store.writing_event("run", digest, "section-1", 1, status="drafted", pool=previous)
    pipeline.store.writing_event(
        "run",
        digest,
        "section-1",
        1,
        status="failed",
        next_action="rewrite",
        issues=[detail],
        evidence_gaps=[{"kind": "source", "detail": detail}],
    )

    _, bundle = pipeline.write("run", run, {}, 1)
    assert not bundle["verdict"]["issues"]
    assert SearchBudget(pipeline.store, "run", 1).usage()["total_used"] == 0


def test_local_repair_resumes_same_draft_and_rejects_prose_rewrite(tmp_path, research_network):
    _, _, factory = research_network
    db = tmp_path / "agent.sqlite"
    run = automatic_state()

    class BrokenBinding(FakeModel):
        writes = 0

        def section_with_evidence(self, state, context, pool):
            self.writes += 1
            draft = super().section_with_evidence(state, context, pool)
            self.correct = draft
            changed = draft.model_copy(deep=True)
            changed.content = changed.content.replace(
                f"[[cite:{changed.citations[0].source_id}]]", "[[cite:unknown]]", 1
            )
            return changed

        def repair_bindings(self, state, draft, pool, issues):
            return self.correct.model_copy(
                update={"content": self.correct.content + "\n\n新增论点。"}
            )

    model = BrokenBinding()
    pipeline = AutomaticWriting(db, model, fetcher_factory=factory)
    with pytest.raises(EvidenceFailure, match="绑定修复修改了正文措辞"):
        pipeline.write("run", run, {}, 1)
    assert model.writes == 1
    original_usage = SearchBudget(pipeline.store, "run", 1).usage()
    assert original_usage["chapter_used"] == 1

    class RepairOnly(FakeModel):
        def research_plan(self, state, targets=None):
            pytest.fail("绑定修复无需检索")

        def section_with_evidence(self, state, context, pool):
            pytest.fail("恢复绑定修复应继续使用原稿")

        def repair_bindings(self, state, draft, pool, issues):
            assert "[[cite:unknown]]" in draft.content
            return model.correct

    recovered = AutomaticWriting(db, RepairOnly(), fetcher_factory=factory)
    _, bundle = recovered.write("run", run, {}, 1)
    assert bundle["draft"]["content"] == model.correct.content
    assert bundle["verdict"]["citations"][0]["supported"]
    assert SearchBudget(recovered.store, "run", 1).usage() == original_usage


def test_new_evidence_policy_rechecks_cached_sources_once_at_the_cap(tmp_path, research_network):
    _, _, factory = research_network
    run = automatic_state()
    db = tmp_path / "agent.sqlite"
    detail = "缺少可以证明实际部署的来源"

    class CachedReview(FakeModel):
        checks = 0

        def research_plan(self, state, targets=None):
            pytest.fail("达到上限后，规则更新也不能触发搜索")

        def verify_section(self, state, context, draft, pool):
            self.checks += 1
            verdict = super().verify_section(state, context, draft, pool)
            verdict.evidence_gaps = [EvidenceGap(kind="source", detail=detail)]
            return verdict

    model = CachedReview()
    pipeline = AutomaticWriting(db, model, fetcher_factory=factory)
    ResearchCollector(pipeline.store, fetcher=factory()).collect("run", run, ["seed cache"])
    budget = SearchBudget(pipeline.store, "run", 1)
    for _ in range(6):
        budget.reserve("previous.example")
    for _ in range(18):
        SearchBudget(pipeline.store, "run").reserve("previous.example")
    usage = budget.usage()
    digest = context_hash(run)
    previous = pipeline.pool("run", run)
    missing = previous["sources"][0]["id"]
    pipeline.store.writing_event("run", digest, "section-1", 1, status="drafted", pool=previous)
    pipeline.store.save_run(
        "run",
        digest,
        pending_pool={"source": [s["id"] for s in previous["sources"][1:]], "figure": []},
    )
    pipeline.store.cleanup_candidates("run", digest, preserve_pending=True)
    assert missing not in {s["id"] for s in pipeline.store.entries("run", digest, "source")}
    pipeline.store.writing_event(
        "run",
        context_hash(run),
        "section-1",
        1,
        status="failed",
        next_action="rewrite",
        evidence_policy=1,
        issues=[detail],
        evidence_gaps=[{"kind": "source", "detail": detail}],
    )
    with pytest.raises(EvidenceFailure, match=detail):
        pipeline.write("run", run, {}, 1)
    assert model.checks == 1 and budget.usage() == usage
    assert any(
        s["id"] == missing and s["verified"]
        for s in pipeline.store.entries("run", digest, "source")
    )

    recovered = AutomaticWriting(db, model, fetcher_factory=factory)
    with pytest.raises(EvidenceFailure, match=detail):
        recovered.write("run", run, {}, 1)
    assert model.checks == 1 and budget.usage() == usage


def test_cached_rewrite_keeps_current_chapter_discovery_focus(
    tmp_path, research_network, monkeypatch
):
    _, _, factory = research_network
    run = automatic_state()
    pipeline = AutomaticWriting(tmp_path / "agent.sqlite", FakeModel(), fetcher_factory=factory)
    ResearchCollector(pipeline.store, fetcher=factory()).collect("run", run, ["seed cache"])
    terms = ["enterprise deployments", "current server use"]
    pipeline.store.save_run(
        "run", context_hash(run), discovery_chapter=1, queries=terms[:1], web_queries=terms[1:]
    )
    real_pool = pipeline.pool
    used_focus = []

    def capture(run_id, state, focus=None, **options):
        used_focus.append(focus)
        return real_pool(run_id, state, focus, **options)

    monkeypatch.setattr(pipeline, "pool", capture)
    budget = SearchBudget(pipeline.store, "run", 1)
    pipeline.prepare_evidence("run", run, budget, [], 3)
    assert used_focus == [terms]
    assert budget.usage()["total_used"] == 0


def test_web_fallback_stops_when_first_channel_provides_enough():
    calls = []

    def respond(request):
        calls.append(request.headers["host"])
        return httpx.Response(
            200,
            text='<html><a class="result__a" href="https://example.org/paper">'
            "Kernel architecture</a></html>",
        )

    search = FreeWebSearch(mock_fetcher(respond))
    seen = []

    def enough(batch):
        seen.extend(batch)
        return len(seen) == 1

    result = search.search("Kernel architecture", on_results=enough)
    assert [item.url for item in result] == ["https://example.org/paper"]
    assert result == seen
    assert calls == ["html.duckduckgo.com"]
