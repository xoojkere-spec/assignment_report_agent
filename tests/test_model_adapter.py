import json

import httpx
import pytest

from assignment_agent.materials import read_material
from assignment_agent.model_adapter import (
    APISettings,
    ModelError,
    OpenAICompatibleModel,
    read_settings,
)
from assignment_agent.schemas import Analysis, AssignmentSpec, Material, RunState, SourceUnit

KEY = "test-private-api-key"


def settings(**kwargs):
    return APISettings(
        base_url="http://localhost:1234/v1", model="custom-model", api_key=KEY, **kwargs
    )


def response(text, finish_reason="stop"):
    return httpx.Response(
        200, json={"choices": [{"message": {"content": text}, "finish_reason": finish_reason}]}
    )


def test_request_context_and_secret_boundary():
    requests = []

    def handle(request):
        requests.append(request)
        return response("草稿 " + KEY)

    model = OpenAICompatibleModel(settings(), transport=httpx.MockTransport(handle))
    state = RunState(spec=AssignmentSpec(title="测试", sections=["分析"], word_limit=500))
    assert model.outline(state, {"current.md": "阶段规则"}) == "草稿 [REDACTED]"
    request = requests[0]
    assert request.url.path == "/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer " + KEY
    body = json.loads(request.content)
    assert KEY not in request.content.decode()
    assert body["model"] == "custom-model"
    assert "阶段规则" in body["messages"][0]["content"]
    assert "skill_documents" not in body["messages"][1]["content"]
    assert "api_key" not in settings().model_dump()
    assert KEY not in repr(settings())


def test_model_can_plan_unverified_url_candidates_without_server_search_tool():
    captured = []

    def handle(request):
        captured.append(json.loads(request.content))
        return response(
            json.dumps(
                {
                    "queries": ["kernel architecture"],
                    "source_candidates": [
                        {"title": "Original article", "url": "https://publisher.example/article"}
                    ],
                }
            )
        )

    model = OpenAICompatibleModel(settings(), transport=httpx.MockTransport(handle))
    run = RunState(spec=AssignmentSpec(title="Kernel", sections=["Introduction"], word_limit=500))
    plan = model.research_plan(run)
    assert plan.source_candidates[0].url == "https://publisher.example/article"
    assert "tools" not in captured[0]
    assert "未经联网核验的候选" in captured[0]["messages"][0]["content"]


def test_evidence_json_repairs_locally_and_records_only_safe_validation_details(tmp_path):
    from assignment_agent.diagnostics import read_diagnostics, trace_run

    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return response(json.dumps({"content": "draft", "citations": [{"secret": KEY}]}))
        return response(json.dumps({"content": "corrected", "citations": [], "figures": []}))

    model = OpenAICompatibleModel(settings(), transport=httpx.MockTransport(handle))
    state = RunState(spec=AssignmentSpec(title="Test", sections=["Introduction"], word_limit=500))
    db = tmp_path / "agent.sqlite"
    with trace_run(db, "repair"):
        draft = model.section_with_evidence(state, {}, {"sources": [], "figures": []})
    assert draft.content == "corrected"
    assert len(requests) == 2
    assert KEY not in json.dumps(requests)
    diagnostics = read_diagnostics(db, "repair")
    encoded = json.dumps(diagnostics)
    assert KEY not in encoded and "secret" not in encoded
    assert "citations.0.source_id" in encoded
    assert "validation.failed" in encoded


def test_verification_prompt_observes_section_scope_without_weakening_support_checks():
    from assignment_agent.evidence_types import EvidenceDraft

    captured = []

    def handle(request):
        captured.append(json.loads(request.content))
        return response(json.dumps({"citations": [], "figures": [], "issues": []}))

    model = OpenAICompatibleModel(settings(), transport=httpx.MockTransport(handle))
    run = RunState(spec=AssignmentSpec(title="Test", sections=["Introduction"], word_limit=500))
    scope = {"stage": "section", "require_bibliography": False}
    model.verify_section(run, {}, EvidenceDraft(content="Plan"), {"verification_scope": scope})
    rules = captured[0]["messages"][0]["content"]
    payload = json.loads(captured[0]["messages"][1]["content"])
    assert payload["evidence_pool"]["verification_scope"] == scope
    assert "缺少列表不是错误" in rules
    assert "不能为回避证据不足而改变已批准的选型" in rules
    assert "原文是否支持 claim" in rules and "不得因为数量目标而放宽核验" in rules


@pytest.mark.parametrize("status", [301, 400, 401, 429, 500])
def test_http_errors_are_safe(status):
    model = OpenAICompatibleModel(
        settings(), transport=httpx.MockTransport(lambda request: httpx.Response(status, text=KEY))
    )
    with pytest.raises(ModelError, match=f"HTTP {status}") as exc:
        model.outline(RunState(), {})
    assert KEY not in str(exc.value)


def test_timeout_and_input_limit():
    assert settings().timeout == 300
    assert settings(timeout=600).timeout == 600

    def timeout(request):
        raise httpx.ReadTimeout(KEY)

    model = OpenAICompatibleModel(settings(), transport=httpx.MockTransport(timeout))
    with pytest.raises(ModelError, match="超时"):
        model.outline(RunState(), {})
    model = OpenAICompatibleModel(settings(max_input_chars=10))
    with pytest.raises(ModelError, match="未截断"):
        model.outline(RunState(), {})


def test_default_input_limit_accepts_request_above_old_limit():
    requests = []

    def handle(request):
        requests.append(request)
        return response("已接收")

    model = OpenAICompatibleModel(settings(), transport=httpx.MockTransport(handle))
    assert model.settings.max_input_chars == 300_000
    result = model._request([{"role": "user", "content": "材" * 181_788}])
    assert result == "已接收" and len(requests) == 1


@pytest.mark.parametrize(
    "result",
    [
        response("", "stop"),
        response("未完成", "length"),
        httpx.Response(200, json={}),
        httpx.Response(200, text="bad"),
        httpx.Response(200, json={"choices": [None]}),
        response("错误类型的结束标记", []),
    ],
)
def test_invalid_responses_are_not_drafts(result):
    model = OpenAICompatibleModel(settings(), transport=httpx.MockTransport(lambda request: result))
    with pytest.raises(ModelError):
        model.outline(RunState(), {})


def test_extraction_repairs_json_and_checks_provenance(tmp_path):
    path = tmp_path / "brief.txt"
    path.write_text("题目：演示\n字数上限：500", encoding="utf-8")
    material = read_material(path, "assignment")
    valid = {
        "facts": [
            {
                "field": "title",
                "value": "演示",
                "source_id": material.source_id,
                "location": "line:1",
                "quote": "题目：演示",
            },
            {
                "field": "word_limit",
                "value": "500",
                "source_id": material.source_id,
                "location": "line:2",
                "quote": "字数上限：500",
            },
        ],
        "issues": [],
        "spec": {"title": "演示", "sections": ["分析"], "word_limit": 500},
    }
    replies = iter(["not json", json.dumps(valid)])
    model = OpenAICompatibleModel(
        settings(), transport=httpx.MockTransport(lambda request: response(next(replies)))
    )
    state = RunState(materials=[material])
    assert model.extract(state, {}).spec.title == "演示"
    valid["facts"][0]["quote"] = "不存在的引用"
    model = OpenAICompatibleModel(
        settings(), transport=httpx.MockTransport(lambda request: response(json.dumps(valid)))
    )
    with pytest.raises(ModelError, match="两次"):
        model.extract(state, {})


def test_extraction_aligns_pdf_whitespace_to_original_source():
    material = Material(
        source_id="pdf-source",
        path="assignment.pdf",
        role="assignment",
        sha256="0" * 64,
        units=[
            SourceUnit(location="page:1", text="Title: Demo\nWord limit: 500\nWeig\nht \n(%)"),
            SourceUnit(location="page:1:ocr-line:14", text="AssessmentTitle", method="ocr"),
        ],
    )
    facts = [
        {
            "field": "title",
            "value": "Demo",
            "source_id": material.source_id,
            "location": "page:1",
            "quote": "Title: Demo",
        },
        {
            "field": "word_limit",
            "value": "500",
            "source_id": material.source_id,
            "location": "page:1",
            "quote": "Word limit: 500",
        },
        {
            "field": "rubric",
            "value": "Weight",
            "source_id": material.source_id,
            "location": "page:1",
            "quote": "Weight (%)",
        },
        {
            "field": "assessment",
            "value": "Title",
            "source_id": material.source_id,
            "location": "page:1:ocr-line:14",
            "quote": "Assessment Title",
        },
    ]
    payload = {
        "facts": facts,
        "issues": [],
        "spec": {"title": "Demo", "sections": ["Analysis"], "word_limit": 500},
    }
    model = OpenAICompatibleModel(
        settings(), transport=httpx.MockTransport(lambda request: response(json.dumps(payload)))
    )
    analysis = model.extract(RunState(materials=[material]), {})
    assert analysis.facts[2].quote == "Weig\nht \n(%)"
    assert analysis.facts[3].quote == "AssessmentTitle"


@pytest.mark.parametrize("bad_quote", ["Weight (%%)", "Weight (9%)"])
def test_extraction_still_rejects_non_whitespace_quote_changes(bad_quote):
    material = Material(
        source_id="pdf-source",
        path="assignment.pdf",
        role="assignment",
        sha256="0" * 64,
        units=[SourceUnit(location="page:1", text="Title: Demo\nWord limit: 500\nWeig\nht \n(%)")],
    )
    payload = {
        "facts": [
            {
                "field": "title",
                "value": "Demo",
                "source_id": material.source_id,
                "location": "page:1",
                "quote": "Title: Demo",
            },
            {
                "field": "word_limit",
                "value": "500",
                "source_id": material.source_id,
                "location": "page:1",
                "quote": "Word limit: 500",
            },
            {
                "field": "rubric",
                "value": "Weight",
                "source_id": material.source_id,
                "location": "page:1",
                "quote": bad_quote,
            },
        ],
        "issues": [],
        "spec": {"title": "Demo", "sections": ["Analysis"], "word_limit": 500},
    }
    model = OpenAICompatibleModel(
        settings(), transport=httpx.MockTransport(lambda request: response(json.dumps(payload)))
    )
    with pytest.raises(ModelError, match="quote_mismatch"):
        model.extract(RunState(materials=[material]), {})


def test_model_assesses_unstated_requirements_after_user_answer():
    captured = []
    payload = {
        "spec": {
            "title": "SRS",
            "sections": [],
            "section_mode": "recommend",
            "word_limit": 7500,
            "page_limit": 30,
            "page_count_rules": "封面、参考文献和附录不计",
            "citation_style": "APA 7",
            "min_sources": 3,
            "min_web_images": 0,
        },
        "reasons": {
            "word_limit": "内部写作预算",
            "citation_style": "选择 APA 7",
            "min_web_images": "本作业无需网络图片",
        },
    }

    def handle(request):
        captured.append(json.loads(request.content))
        return response(json.dumps(payload, ensure_ascii=False))

    model = OpenAICompatibleModel(settings(), transport=httpx.MockTransport(handle))
    state = RunState(
        analysis=Analysis(
            requirement_details={"page_limit": 30, "page_count_rules": "封面、参考文献和附录不计"}
        )
    )
    assessed = model.assess_requirements(state, "没有额外要求", {})
    assert assessed.spec.min_sources == 3 and assessed.spec.min_web_images == 0
    assert assessed.spec.page_limit == 30
    assert json.loads(captured[0]["messages"][1]["content"])["user_answer"] == "没有额外要求"
    assert "内部写作预算" in assessed.reasons["word_limit"]


def test_environment_config_and_no_defaults(monkeypatch, tmp_path):
    for name in ("OPENAI_BASE_URL", "OPENAI_API_KEY", "MODEL_NAME"):
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / ".env"
    with pytest.raises(ModelError, match="请配置"):
        read_settings(env)
    env.write_text(
        f"OPENAI_BASE_URL=http://localhost:1234/v1\nMODEL_NAME=custom\nOPENAI_API_KEY={KEY}",
        encoding="utf-8",
    )
    monkeypatch.setenv("MODEL_NAME", "override")
    assert read_settings(env).model == "override"
    assert KEY not in repr(read_settings(env))


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@host/v1",
        "https://host/v1?key=x",
        "file:///tmp/api",
        "https://host/v1/chat/completions",
    ],
)
def test_unsafe_or_incorrect_base_urls_are_rejected(url):
    with pytest.raises(ModelError):
        APISettings(base_url=url, model="custom", api_key=KEY).checked()


def test_structured_plan_budget_and_purpose():
    state = RunState(
        spec=AssignmentSpec(title="规划测试", section_mode="recommend", word_limit=500)
    )
    body = {
        "parts": [
            {"title": "分析", "word_budget": 350, "purpose": "比较两种观点"},
            {"title": "结论", "word_budget": 150, "purpose": "回应作业问题"},
        ]
    }
    model = OpenAICompatibleModel(
        settings(), transport=httpx.MockTransport(lambda request: response(json.dumps(body)))
    )
    plan = model.plan(state, {})
    assert [part.title for part in plan.parts] == ["分析", "结论"]
    assert "比较两种观点" in plan.to_markdown("规划测试")
    body["parts"][0]["word_budget"] = 501
    with pytest.raises(ModelError, match="预算"):
        model.plan(state, {})
