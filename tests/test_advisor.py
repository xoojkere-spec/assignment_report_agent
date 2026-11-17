import json
import threading

import httpx
import pytest
from conftest import legacy_agent as open_agent
from conftest import legacy_app as create_app
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_web import HEADERS, config, wait
from test_workflow import decision

from assignment_agent import advisor
from assignment_agent.fake_model import FakeModel
from assignment_agent.model_adapter import APISettings, ModelError, OpenAICompatibleModel
from assignment_agent.schemas import AdvisorQuestion, AssignmentSpec, Clarification, RunState


def question(result, text="rubric 中的批判性分析是什么意思？"):
    return AdvisorQuestion(interrupt_id=result["interrupt_id"], question=text)


def test_material_context_history_and_checkpoint_unchanged(tmp_path, compatible_server):
    url, requests, _ = compatible_server
    brief = tmp_path / "brief.txt"
    brief.write_text(
        "题目：作业答疑\n部分：分析；结论\n字数上限：800\n引用格式：APA 7\n网络图片最少：2",
        encoding="utf-8",
    )
    rubric = tmp_path / "rubric.txt"
    rubric.write_text("批判性分析占比 40%，需要比较不同观点。", encoding="utf-8")
    db = tmp_path / "agent.sqlite"
    model = OpenAICompatibleModel(APISettings(base_url=url, model="test", api_key="private-key"))
    with open_agent(db, model) as agent:
        original = agent.start_materials(brief, [rubric], run_id="questions")
        checkpoint = agent.graph.get_state(agent._config("questions")).config
        first = agent.ask("questions", question(original))
        second = agent.ask("questions", question(first, "能用一个简单例子解释吗？"))
        assert first["state"] == second["state"] == original["state"]
        assert second["interrupt_id"] == original["interrupt_id"]
        assert second["next_nodes"] == original["next_nodes"]
        assert agent.graph.get_state(agent._config("questions")).config == checkpoint
        assert len(second["advisor"]["exchanges"]) == 2
        messages = requests[-1]["body"]["messages"]
        context = json.loads(messages[1]["content"])
        assert {item["role"] for item in context["materials"]} == {"assignment", "rubric"}
        assert context["materials"][1]["units"][0]["text"].startswith("批判性分析占比")
        assert context["current_outline"] == original["state"]["outline"]["content"]
        assert messages[-3] == {"role": "user", "content": question(first).question}
        assert messages[-2]["role"] == "assistant"
        assert "private-key" not in json.dumps(requests[-1]["body"])
        assert str(tmp_path) not in json.dumps(context, ensure_ascii=False)
    with open_agent(db) as agent:
        restored = agent.status("questions")
        assert restored["advisor"] == second["advisor"]
        assert restored["state"] == original["state"]


def test_requirements_outline_and_stale_questions(tmp_path):
    brief = tmp_path / "brief.txt"
    brief.write_text("题目：需澄清\n部分：分析", encoding="utf-8")
    with open_agent(tmp_path / "agent.sqlite") as agent:
        result = agent.start_materials(brief, run_id="clarify")
        assert result["advisor"]["available"] is True
        result = agent.ask("clarify", question(result, "为什么要我填写字数上限？"))
        assert "假模型答疑" in result["advisor"]["exchanges"][0]["answer"]
        old_question = question(result)
        result = agent.clarify(
            "clarify",
            Clarification(
                interrupt_id=result["interrupt_id"],
                spec=AssignmentSpec(title="需澄清", sections=["分析"], word_limit=500),
                note="已核对教师要求",
            ),
        )
        assert result["advisor"]["available"] is True
        with pytest.raises(ValueError, match="刷新任务状态"):
            agent.ask("clarify", old_question)
        result = agent.ask("clarify", question(result, "这个大纲怎样对应评分标准？"))
        result = agent.decide("clarify", decision(result))
        assert result["advisor"]["available"] is False
        assert len(result["advisor"]["exchanges"]) == 2
        with pytest.raises(ValueError, match="仅可"):
            agent.ask("clarify", question(result))


def test_safe_api_failure_and_retry_without_failing_writing(tmp_path, compatible_server):
    url, requests, replies = compatible_server
    model = OpenAICompatibleModel(APISettings(base_url=url, model="test", api_key="private-key"))
    with open_agent(tmp_path / "agent.sqlite", model) as agent:
        result = agent.start(AssignmentSpec(title="测试", sections=["分析"], word_limit=500), "qa")
        replies.append((401, "private-key provider error"))
        with pytest.raises(ModelError, match="HTTP 401"):
            agent.ask("qa", question(result, "解释 private-key 以及评分标准"))
        failed = agent.status("qa")
        assert failed["state"] == result["state"]
        assert failed["failed_nodes"] == []
        exchange = failed["advisor"]["exchanges"][0]
        assert exchange["status"] == "failed"
        assert exchange["answer"] is None
        assert "private-key" not in json.dumps(failed)
        assert exchange["question"] == "解释 [REDACTED] 以及评分标准"
        replies.append((200, "解释答案 private-key"))
        recovered = agent.ask("qa", question(failed))
        assert recovered["advisor"]["exchanges"][-1]["answer"] == "解释答案 [REDACTED]"
        assert len(requests[-1]["body"]["messages"]) == 3
        assert any(event.get("stage") == "advisor" for event in recovered["diagnostics"]["events"])


def test_advisor_usage_and_input_limit(tmp_path):
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "解释"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
            },
        )

    settings = APISettings(base_url="http://localhost:1234/v1", model="test", api_key="private")
    model = OpenAICompatibleModel(settings, transport=httpx.MockTransport(handle))
    with open_agent(tmp_path / "agent.sqlite", model) as agent:
        result = agent.start(AssignmentSpec(title="测试", sections=["分析"], word_limit=500), "qa")
        result = agent.ask("qa", question(result))
        assert result["diagnostics"]["usage"]["total_tokens"] == 120
        assert result["diagnostics"]["events"][-1]["stage"] == "advisor"
        system = requests[-1]["messages"][0]["content"]
        assert "不代替用户确认" in system and "不能声称查证" in system
    limited = OpenAICompatibleModel(
        settings.model_copy(update={"max_input_chars": 20}), transport=httpx.MockTransport(handle)
    )
    count = len(requests)
    with pytest.raises(ModelError, match="未截断"):
        limited.advise(RunState(), "问题", [])
    assert len(requests) == count


def test_web_advisor_jobs_restart_recovery_and_validation(tmp_path, compatible_server):
    url, requests, _ = compatible_server
    db = tmp_path / "agent.sqlite"
    model = OpenAICompatibleModel(
        APISettings(base_url=url, model="web-test", api_key="web-private-key")
    )
    with open_agent(db, model) as agent:
        original = agent.start(
            AssignmentSpec(title="网页答疑", sections=["分析"], word_limit=500), "qa"
        )
    app = create_app(db, tmp_path / "missing.env")
    body = question(original).model_dump()
    with TestClient(app) as client:
        assert client.post("/api/runs/qa/advisor", json=body).status_code == 403
        assert client.post("/api/runs/qa/advisor", headers=HEADERS, json=body).status_code == 400
        config(client, url)
        for text in ("   ", "x" * 4001):
            assert (
                client.post(
                    "/api/runs/qa/advisor", headers=HEADERS, json={**body, "question": text}
                ).status_code
                == 422
            )
        stale = client.post(
            "/api/runs/qa/advisor", headers=HEADERS, json={**body, "interrupt_id": "stale"}
        )
        assert stale.status_code == 400
        response = client.post("/api/runs/qa/advisor", headers=HEADERS, json=body)
        assert response.status_code == 202
        assert response.json()["kind"] == "advisor"
        assert wait(client, response.json())["status"] == "completed"
        current = client.get("/api/runs/qa").json()
        assert current["state"]["pending"] == original["state"]["pending"]
        assert current["advisor"]["exchanges"][0]["status"] == "completed"
    advisor.begin(db, "qa", original["interrupt_id"], "服务中断的提问")
    with TestClient(create_app(db, tmp_path / "missing.env")) as client:
        count = len(requests)
        restored = client.get("/api/runs/qa").json()
        assert restored["advisor"]["exchanges"][0]["answer"]
        assert restored["advisor"]["exchanges"][1]["status"] == "failed"
        assert "已中断" in restored["advisor"]["exchanges"][1]["error"]
        assert len(requests) == count
        assert client.get("/api/config").json()["configured"] is False


def test_model_profile_guard_prevents_question_call(tmp_path, compatible_server):
    url, requests, _ = compatible_server
    db = tmp_path / "agent.sqlite"
    with open_agent(db, FakeModel()) as agent:
        result = agent.start(AssignmentSpec(title="测试", sections=["分析"], word_limit=500), "qa")
    model = OpenAICompatibleModel(APISettings(base_url=url, model="different", api_key="private"))
    with open_agent(db, model) as agent:
        with pytest.raises(ModelError, match="模型类型与任务记录不兼容"):
            agent.ask("qa", question(result))
        assert agent.status("qa")["advisor"]["exchanges"] == []
    assert requests == []
    with pytest.raises(ValidationError):
        AdvisorQuestion(interrupt_id="x", question=" ")


def test_advisor_blocks_parallel_writing_and_repeated_calls(
    tmp_path, compatible_server, monkeypatch
):
    url, requests, _ = compatible_server
    db = tmp_path / "agent.sqlite"
    model = OpenAICompatibleModel(
        APISettings(base_url=url, model="web-test", api_key="web-private-key")
    )
    with open_agent(db, model) as agent:
        original = agent.start(
            AssignmentSpec(title="串行答疑", sections=["分析"], word_limit=500), "qa"
        )
    entered = threading.Event()
    release = threading.Event()
    advise = OpenAICompatibleModel.advise

    def delayed(self, *args):
        entered.set()
        assert release.wait(5), "测试未释放模拟模型请求"
        return advise(self, *args)

    monkeypatch.setattr(OpenAICompatibleModel, "advise", delayed)
    with TestClient(create_app(db, tmp_path / "missing.env")) as client:
        config(client, url)
        response = client.post(
            "/api/runs/qa/advisor", headers=HEADERS, json=question(original).model_dump()
        )
        try:
            assert entered.wait(3)
            repeated = client.post(
                "/api/runs/qa/advisor", headers=HEADERS, json=question(original).model_dump()
            )
            assert repeated.status_code == 409
            confirm = client.post(
                "/api/runs/qa/decision", headers=HEADERS, json=decision(original).model_dump()
            )
            assert confirm.status_code == 409
            assert len(requests) == 1
        finally:
            release.set()
        assert wait(client, response.json())["status"] == "completed"
        current = client.get("/api/runs/qa").json()
        assert current["state"]["drafts"] == []
        assert current["interrupt_id"] == original["interrupt_id"]
        assert len(current["advisor"]["exchanges"]) == 1
        assert len(requests) == 2
