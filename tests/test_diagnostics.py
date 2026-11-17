import json

import httpx
import pytest
from conftest import legacy_agent as open_agent

from assignment_agent.model_adapter import APISettings, ModelError, OpenAICompatibleModel

SECRET = "private-diagnostic-test-key"


def test_failed_status_and_safe_usage_survive_restart(tmp_path):
    brief = tmp_path / "brief.txt"
    brief.write_text("题目：测试\n字数上限：500", encoding="utf-8")
    db = tmp_path / "state.sqlite"

    def handle(request):
        return httpx.Response(
            200,
            headers={"x-request-id": "req-123"},
            json={
                "id": "resp-456",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps({SECRET: "private raw output"})},
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "untrusted_extra": SECRET,
                },
            },
        )

    model = OpenAICompatibleModel(
        APISettings(base_url="http://localhost/v1", model="test", api_key=SECRET),
        transport=httpx.MockTransport(handle),
    )
    with open_agent(db, model) as agent:
        with pytest.raises(ModelError, match="两次"):
            agent.start_materials(brief, run_id="failed")
        result = agent.status("failed")
        assert result["state"]["status"] == "failed"
        assert result["failed_nodes"] == ["parse"]
        diagnostics = result["diagnostics"]
        assert diagnostics["usage"] == {
            "prompt_tokens": 20,
            "completion_tokens": 10,
            "total_tokens": 30,
        }
        assert diagnostics["response_count"] == 2
        assert diagnostics["usage_reported_count"] == 2
        failure = [item for item in diagnostics["events"] if item["event"] == "validation.failed"]
        assert len(failure) == 2
        assert failure[0]["details"][0]["code"] == "extra_forbidden"
        assert failure[0]["details"][0]["path"] == "<field>"
        assert SECRET not in json.dumps(result)
    with open_agent(db) as agent:
        assert agent.status("failed") == result
    assert SECRET.encode() not in db.read_bytes()
    assert b"private raw output" not in db.read_bytes()


def test_quote_validation_reports_location_without_raw_text(tmp_path):
    brief = tmp_path / "brief.txt"
    brief.write_text("题目：测试\n字数上限：500", encoding="utf-8")
    model = OpenAICompatibleModel(
        APISettings(base_url="http://localhost/v1", model="test", api_key=SECRET)
    )
    from assignment_agent.materials import read_material

    material = read_material(brief, "assignment")
    body = {
        "facts": [
            {
                "field": "title",
                "value": "测试",
                "source_id": material.source_id,
                "location": "line:1",
                "quote": "题目：其它",
            }
        ],
        "issues": [],
        "spec": {"title": "测试", "sections": ["分析"], "word_limit": 500},
    }
    model._transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(body)}, "finish_reason": "stop"}]},
        )
    )
    with open_agent(tmp_path / "state.sqlite", model) as agent:
        with pytest.raises(ModelError, match="quote_mismatch"):
            agent.start_materials(brief, run_id="quote")
        events = agent.status("quote")["diagnostics"]["events"]
        detail = next(event for event in events if event["event"] == "validation.failed")[
            "details"
        ][0]
        assert detail == {"code": "quote_mismatch", "fact_index": 0, "whitespace_match": False}
        assert "quote" not in detail
