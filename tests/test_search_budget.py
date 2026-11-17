from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from test_grounded_writing import AutomaticFake, automatic_state
from test_research import research_network as research_network

from assignment_agent import research
from assignment_agent.free_search import FreeWebSearch
from assignment_agent.grounded_writing import EvidenceFailure
from assignment_agent.model_adapter import ModelError
from assignment_agent.public_fetch import PublicFetcher
from assignment_agent.research_budget import SearchBudget, SearchBudgetReached
from assignment_agent.research_store import ResearchStore, interrupt_research
from assignment_agent.research_types import ResearchPlan
from assignment_agent.schemas import AssignmentSpec
from assignment_agent.service import open_agent


def mock_fetcher(handler, **kwargs):
    return PublicFetcher(
        transport=httpx.MockTransport(handler),
        resolver=lambda *args, **options: [(2, 1, 6, "", ("93.184.216.34", 443))],
        **kwargs,
    )


def test_parallel_search_reservations_enforce_both_limits(tmp_path):
    db = tmp_path / "agent.sqlite"
    SearchBudget(ResearchStore(db), "run").usage()

    def reserve(chapter):
        budget = SearchBudget(ResearchStore(db), "run", chapter)
        try:
            budget.reserve("mock.example")
            return True
        except SearchBudgetReached:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, [1] * 16)) == 6
        assert sum(pool.map(reserve, [2, 3, 4, 5] * 8)) == 18

    with ResearchStore(db).connect() as connection:
        totals = connection.execute(
            "SELECT chapter,COUNT(*) FROM research_search_attempts GROUP BY chapter"
        ).fetchall()
    assert sum(row[1] for row in totals) == 24
    assert all(row[1] <= 6 for row in totals)
    assert not reserve(6)
    assert SearchBudget(ResearchStore(db), "other", 1).usage()["total_used"] == 0


def test_final_body_topup_can_exceed_chapter_cap_but_not_total_cap(tmp_path):
    store = ResearchStore(tmp_path / "topup.sqlite")
    regular = SearchBudget(store, "run", 3)
    for _ in range(6):
        regular.reserve("early.example")
    with pytest.raises(SearchBudgetReached):
        regular.reserve("early.example")
    topup = SearchBudget(store, "run", 3, allow_chapter_overflow=True)
    for _ in range(18):
        topup.reserve("topup.example")
    assert topup.usage()["chapter_used"] == 24
    assert topup.usage()["total_used"] == 24
    with pytest.raises(SearchBudgetReached):
        topup.reserve("topup.example")


def test_last_body_research_continues_after_six_chapter_searches(
    tmp_path, monkeypatch, research_network
):
    _, _, factory = research_network
    monkeypatch.setattr(research, "PublicFetcher", factory)
    db = tmp_path / "last-body.sqlite"
    budget = SearchBudget(ResearchStore(db), "topup", 1)
    for _ in range(6):
        budget.reserve("previous.example")
    spec = automatic_state().spec.model_copy(update={"min_web_images": 0})
    with open_agent(db, AutomaticFake()) as agent:
        completed = agent.start(spec, "topup")
    assert completed["state"]["status"] == "completed"
    assert completed["research"]["used_sources"] >= 3
    usage = SearchBudget(ResearchStore(db), "topup", 1, allow_chapter_overflow=True).usage()
    assert 6 < usage["chapter_used"] <= usage["total_used"] <= 24


def test_total_cap_does_not_allow_uncited_final_body_to_pass(tmp_path):
    db = tmp_path / "total-cap.sqlite"
    store = ResearchStore(db)
    budget = SearchBudget(store, "no-sources")
    for _ in range(24):
        budget.reserve("previous.example")
    spec = automatic_state().spec.model_copy(update={"min_web_images": 0})
    with open_agent(db, AutomaticFake()) as agent:
        with pytest.raises(EvidenceFailure, match="搜索预算已用尽"):
            agent.start(spec, "no-sources")
        status = agent.status("no-sources")
    assert status["state"]["drafts"] == []
    assert status["research"]["search_budget"]["total_used"] == 24
    assert status["research"]["used_sources"] == 0


def test_mock_search_pauses_at_chapter_cap_and_restart_keeps_exact_usage(tmp_path, monkeypatch):
    db = tmp_path / "agent.sqlite"
    requests = []
    planned = []

    class NoEvidence(AutomaticFake):
        def research_plan(self, state, targets=None):
            planned.append(state.current_index)
            return ResearchPlan(
                queries=["Kernel architecture"],
                web_queries=["Windows kernel architecture"],
                figure_queries=["Kernel diagrams"],
            )

    def respond(request):
        host = request.headers["host"]
        requests.append((host, request.url.path, dict(request.url.params)))
        if host == "api.openalex.org":
            return httpx.Response(200, json={"results": []})
        if host == "api.crossref.org":
            return httpx.Response(200, json={"message": {"items": []}})
        if host == "api.semanticscholar.org":
            return httpx.Response(200, json={"data": []})
        if host == "html.duckduckgo.com":
            return httpx.Response(429)
        if host == "www.bing.com":
            return httpx.Response(302, headers={"location": "/redirected"})
        pytest.fail(f"意外的网络请求 {host}")

    monkeypatch.setattr(research, "PublicFetcher", lambda: mock_fetcher(respond))
    spec = automatic_state().spec.model_copy(
        update={"sections": ["Analysis", "Methods", "Conclusion"]}
    )
    with open_agent(db, NoEvidence()) as agent:
        with pytest.raises(EvidenceFailure, match="搜索预算已用尽"):
            agent.start(spec, "run")
        first = agent.status("run")
    assert first["state"]["current_index"] == 0
    assert first["state"]["drafts"] == [] and first["next_nodes"] == ["section"]
    assert first["research"]["status"] == "search_budget_exhausted"
    assert not any("3 个独立全文来源，当前有 0 个" in i for i in first["research"]["issues"])
    assert any("1 张相关图" in i and "候选 0 张" in i for i in first["research"]["issues"])
    assert first["research"]["search_budget"] == {
        "chapter": 1,
        "chapter_used": 6,
        "chapter_limit": 6,
        "total_used": 6,
        "total_limit": 24,
        "exhausted": True,
    }
    assert [host for host, _, _ in requests] == [
        "api.openalex.org",
        "html.duckduckgo.com",
        "www.bing.com",
        "www.bing.com",
        "www.bing.com",
        "www.bing.com",
    ]
    assert requests[0][2]["search"] == "Kernel diagrams"
    assert "locations.license" in requests[0][2]["filter"]
    assert requests[-1][1] == "/redirected"

    with open_agent(db, NoEvidence()) as agent:
        with pytest.raises(EvidenceFailure, match="搜索预算已用尽"):
            agent.recover("run")
        recovered = agent.status("run")
    assert recovered["state"] == first["state"]
    assert recovered["research"]["search_budget"] == first["research"]["search_budget"]
    assert len(requests) == 6 and planned == [0]


def test_redirects_are_charged_and_cached_engine_results_survive_exhaustion(tmp_path):
    budget = SearchBudget(ResearchStore(tmp_path / "agent.sqlite"), "run", 1)
    for _ in range(4):
        budget.reserve("previous.example")
    calls = []

    def respond(request):
        calls.append(request.url.path)
        if request.url.path == "/html/":
            return httpx.Response(302, headers={"location": "/results"})
        assert request.url.path == "/results"
        return httpx.Response(
            200,
            text='<html><a class="result__a" href="https://example.org/paper">'
            "Kernel architecture</a></html>",
        )

    web = FreeWebSearch(mock_fetcher(respond, before_request=budget.reserve))
    assert [item.url for item in web.search("Kernel architecture")] == ["https://example.org/paper"]
    assert calls == ["/html/", "/results"]
    assert budget.usage()["chapter_used"] == 6


def test_interrupted_candidate_selection_recovers_at_cap(tmp_path, monkeypatch, research_network):
    calls, _, factory = research_network
    monkeypatch.setattr(research, "PublicFetcher", factory)
    db = tmp_path / "agent.sqlite"

    class Interrupted(AutomaticFake):
        def select_sources(self, state, candidates, targets):
            assert candidates
            raise ModelError("模型连接中断")

    class CacheOnly(AutomaticFake):
        def research_plan(self, state, targets=None):
            pytest.fail("额度耗尽后应直接复用已取得的候选")

    def searches():
        return [
            request
            for request in calls
            if request.url.params.get("search")
            or request.url.params.get("query.bibliographic")
            or request.url.params.get("query")
            or request.url.params.get("q")
        ]

    spec = automatic_state().spec.model_copy(update={"sections": ["Analysis", "References"]})
    budget = SearchBudget(ResearchStore(db), "run", 1)
    for _ in range(5):
        budget.reserve("previous.example")
    with open_agent(db, Interrupted()) as agent:
        with pytest.raises(ModelError, match="模型连接中断"):
            agent.start(spec, "run")
        assert agent.status("run")["research"]["candidate_count"] == 3
    interrupt_research(db)
    ResearchStore(db).cleanup_interrupted()
    assert len(searches()) == 1 and budget.usage()["chapter_used"] == 6
    with open_agent(db, CacheOnly()) as agent:
        completed = agent.recover("run")
    assert completed["state"]["status"] == "completed"
    assert completed["research"]["used_sources"] == 3
    assert completed["research"]["used_images"] == 2
    assert completed["research"]["candidate_count"] == 0
    assert len(searches()) == 1 and budget.usage()["chapter_used"] == 6


def test_whole_article_cap_still_allows_later_chapter_with_cached_evidence(
    tmp_path, monkeypatch, research_network
):
    _, _, factory = research_network
    monkeypatch.setattr(research, "PublicFetcher", factory)
    db = tmp_path / "agent.sqlite"
    plan_calls = []

    class MultiChapter(AutomaticFake):
        def research_plan(self, state, targets=None):
            plan_calls.append(state.current_index)
            return ResearchPlan(queries=["kernel architecture", "kernel research"])

    spec = AssignmentSpec(
        title="Kernel architecture",
        sections=["Overview", "Protection", "Structure", "Results", "Trade-offs", "References"],
        word_limit=1800,
    )
    for chapter in range(1, 5):
        budget = SearchBudget(ResearchStore(db), "run", chapter)
        for _ in range(5 if chapter < 4 else 6):
            budget.reserve("previous.example")
    with open_agent(db, MultiChapter()) as agent:
        completed = agent.start(spec, "run")
    assert completed["state"]["status"] == "completed"
    assert completed["research"]["search_budget"]["total_used"] == 24
    assert plan_calls == [0, 1, 4]
    assert [
        SearchBudget(ResearchStore(db), "run", chapter).usage()["chapter_used"]
        for chapter in range(1, 6)
    ] == [6, 6, 5, 6, 1]
