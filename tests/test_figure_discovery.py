import json

import httpx
import pytest
from test_model_adapter import response, settings
from test_research import candidate
from test_research import research_network as research_network

from assignment_agent.diagnostics import read_diagnostics, trace_run
from assignment_agent.grounded_writing import AutomaticWriting, auto_license
from assignment_agent.model_adapter import ModelError, OpenAICompatibleModel
from assignment_agent.research import ResearchCollector
from assignment_agent.research_search import openalex_paper
from assignment_agent.research_store import ResearchStore, context_hash
from assignment_agent.research_types import ResearchPlan
from assignment_agent.schemas import AssignmentSpec, RunState


def state():
    return RunState(
        spec=AssignmentSpec(title="Kernel architecture", sections=["Analysis"], word_limit=900)
    )


def test_image_target_searches_licensed_candidates_and_keeps_original_pdf_checks(
    tmp_path, research_network
):
    calls, _, factory = research_network
    store = ResearchStore(tmp_path / "agent.sqlite")
    collector = ResearchCollector(store, fetcher=factory())
    collector.collect("figures", state(), ["kernel architecture"], figure_target=2)
    filters = [
        request.url.params["filter"]
        for request in calls
        if request.headers["host"] == "api.openalex.org" and request.url.path == "/works"
    ]
    assert filters == [
        "is_oa:true,is_retracted:false,locations.license:cc-by|cc-by-sa|cc0",
        "is_oa:true,is_retracted:false",
    ]
    seen = []
    collector.collect(
        "figures",
        state(),
        ["kernel architecture"],
        refresh=True,
        figure_target=2,
        ranker=lambda candidates: seen.extend(candidates) or [s["id"] for s in candidates],
    )
    assert any(s["figure_inventory"][0]["license"]["status"] == "unknown" for s in seen)
    events = store.summary("figures", state())["events"]
    assert any(e.get("provider") == "openalex-figures" for e in events)
    assert len(store.entries("figures", context_hash(state()), "figure")) == 3


def test_index_license_is_only_a_hint_and_does_not_authorize_an_unknown_figure():
    paper = openalex_paper(
        {
            "id": "https://openalex.org/W1",
            "title": "Kernel architecture",
            "locations": [
                {
                    "license": "cc-by",
                    "pdf_url": "https://paper.example/a.pdf",
                    "landing_page_url": "https://paper.example/a",
                }
            ],
        }
    )
    assert paper.license_hints[0]["license"] == "cc-by"
    figure = {"license": {"status": "unknown", "url": None, "evidence": "No PDF permission"}}
    assert not auto_license(figure)


def test_source_selector_receives_real_image_limitations_and_unverified_license_hints():
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return response('{"source_ids":["src-test"]}')

    model = OpenAICompatibleModel(settings(), transport=httpx.MockTransport(handle))
    paper = openalex_paper(
        {
            "id": "https://openalex.org/W1",
            "title": "Kernel architecture",
            "locations": [{"license": "cc-by", "pdf_url": "https://paper.example/a.pdf"}],
        }
    )
    model.select_sources(
        state(),
        [
            {
                "id": "src-test",
                "paper": paper.model_dump(),
                "figure_inventory": [
                    {"caption": "Kernel", "copyright_check": {"status": "blocked"}}
                ],
                "license": {"status": "unknown"},
                "figure_warnings": ["Figure 1 未找到可靠裁剪区域"],
            }
        ],
        {"new_figures": 2},
    )
    data = json.loads(requests[0]["messages"][-1]["content"])
    item = data["candidates"][0]
    assert item["license_hints"][0]["license"] == "cc-by"
    assert item["checked_license"]["status"] == "unknown"
    assert item["figure_inventory"][0]["copyright_check"]["status"] == "blocked"
    assert item["figure_warnings"]
    assert "只是索引线索" in requests[0]["messages"][0]["content"]


def test_retry_feedback_explains_source_or_relevance_gap_without_blocking_permission():
    library = {
        "sources": [{"id": "s1", "title": "Kernel design"}],
        "figures": [
            {
                "id": "f1",
                "source_id": "s1",
                "license": {"status": "unknown", "evidence": "No license"},
            },
            {
                "id": "f2",
                "source_id": "s1",
                "image_review": {"status": "blocked"},
            },
        ],
    }
    pool = {"targets": {"new_figures": 2, "used_figures": []}, "figures": []}
    feedback = AutomaticWriting.figure_gaps(library, pool)
    assert "需新增 2 张相关图，来源与文件初筛候选 0 张" in feedback[0]
    assert any("许可状态只作提示" in issue for issue in feedback)
    assert not any("须另查许可明确" in issue for issue in feedback)
    pool["targets"]["new_figures"] = 0
    assert AutomaticWriting.figure_gaps(library, pool) == []


def test_cached_full_text_does_not_consume_selection_slots_for_new_downloads(
    tmp_path, research_network
):
    calls, _, factory = research_network
    store = ResearchStore(tmp_path / "agent.sqlite")
    ResearchCollector(store, fetcher=factory()).collect("selection", state(), ["kernel"])
    captured = []

    def handle(request):
        data = json.loads(json.loads(request.content)["messages"][-1]["content"])
        captured.append(data)
        assert len(data["retained_sources"]) == 3
        assert [p["title"] for p in data["candidates"]] == [candidate(3).title]
        return response(
            json.dumps(
                {
                    "source_ids": [
                        *[p["id"] for p in data["retained_sources"]],
                        *[p["id"] for p in data["candidates"]],
                    ]
                }
            )
        )

    class Search:
        providers = ("openalex",)

        def search(self, provider, query):
            return [candidate(i) for i in range(4)]

    model = OpenAICompatibleModel(settings(), transport=httpx.MockTransport(handle))
    with trace_run(store.database, "selection"):
        result = ResearchCollector(store, fetcher=factory(), search=Search()).collect(
            "selection",
            state(),
            ["new kernel"],
            ranker=lambda sources: (
                model.select_sources(state(), sources, {"new_figures": 2}).source_ids
            ),
        )
    assert len(captured) == 1
    assert sum(bool(s.get("verified")) for s in result["sources"]) == 4
    selection = next(e for e in result["events"] if e["code"] == "source.selection")
    assert len(selection["selected_ids"]) == 1
    assert len(selection["retained_ids"]) == 3
    omitted = next(
        e
        for e in read_diagnostics(store.database, "selection")["events"]
        if e["event"] == "source_selection.cached_omitted"
    )
    assert set(omitted["retained_ids"]) == set(selection["retained_ids"])
    assert omitted["selected_ids"] == selection["selected_ids"]
    for index in range(4):
        assert sum(request.url.path == f"/paper{index}.pdf" for request in calls) == 1


def test_cached_only_selection_skips_api_and_cached_ids_cannot_be_reselected():
    retained = {"id": "cached", "title": "Cached kernel", "retrieval_needed": False}
    model = OpenAICompatibleModel(
        settings(), transport=httpx.MockTransport(lambda request: pytest.fail("不应调用 API"))
    )
    assert model.select_sources(state(), [retained], {"new_figures": 2}).source_ids == []
    pending = {"id": "new", "paper": candidate().model_dump()}
    model = OpenAICompatibleModel(
        settings(),
        transport=httpx.MockTransport(lambda request: response('{"source_ids":["cached"]}')),
    )
    assert model.select_sources(state(), [retained, pending], {"new_figures": 2}).source_ids == []


@pytest.mark.parametrize("selected", [["unknown"], ["cached", "unknown"], ["new", "unknown"]])
def test_unknown_selection_is_not_hidden_by_cached_id_normalization(selected):
    retained = {"id": "cached", "title": "Cached kernel", "retrieval_needed": False}
    pending = {"id": "new", "paper": candidate().model_dump()}
    model = OpenAICompatibleModel(
        settings(),
        transport=httpx.MockTransport(
            lambda request: response(json.dumps({"source_ids": selected}))
        ),
    )
    with pytest.raises(ModelError, match="不存在的候选编号"):
        model.select_sources(state(), [retained, pending], {"new_figures": 2})


def test_figure_query_uses_licensed_index_without_leaking_into_normal_search(
    tmp_path, research_network
):
    calls, _, factory = research_network
    store = ResearchStore(tmp_path / "agent.sqlite")
    ResearchCollector(store, fetcher=factory()).collect(
        "queries",
        state(),
        ["kernel security isolation tradeoffs"],
        web_queries=["official kernel documentation"],
        figure_queries=["Linux kernel architecture"],
        figure_target=2,
    )
    indexed = [
        (request.url.params["search"], request.url.params["filter"])
        for request in calls
        if request.headers["host"] == "api.openalex.org" and request.url.path == "/works"
    ]
    assert indexed == [
        (
            "Linux kernel architecture",
            "is_oa:true,is_retracted:false,locations.license:cc-by|cc-by-sa|cc0",
        ),
        ("kernel security isolation tradeoffs", "is_oa:true,is_retracted:false"),
    ]
    assert store.run("queries")["figure_queries"] == ["Linux kernel architecture"]
    assert ResearchPlan(queries=["kernel"], figure_queries=[" Linux kernel "]).figure_queries == [
        "Linux kernel"
    ]
    assert ResearchPlan(queries=["kernel"]).figure_queries == []
