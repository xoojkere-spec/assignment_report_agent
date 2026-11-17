import json

import pytest
from conftest import legacy_agent as open_agent

from assignment_agent.model_adapter import APISettings, ModelError, OpenAICompatibleModel
from assignment_agent.schemas import Decision

KEY = "byok-test-secret-value"


def adapter(url, key=KEY, model="test-model"):
    return OpenAICompatibleModel(APISettings(base_url=url, model=model, api_key=key))


def choice(result, action="approve", feedback=""):
    pending = result["state"]["pending"]
    return Decision(
        interrupt_id=result["interrupt_id"],
        artifact_id=pending["artifact_id"],
        version=pending["version"],
        action=action,
        feedback=feedback,
    )


def context_paths(request):
    text = request["body"]["messages"][0]["content"]
    # 入口文档本身会提及其他模块；校验加载键，不能仅搜索文档中的链接名称。
    context, _ = json.JSONDecoder().raw_decode(text[text.index('{"SKILL.md"') :])
    return set(context)


def test_http_workflow_revisions_resume_and_key_rotation(compatible_server, tmp_path):
    url, requests, _ = compatible_server
    brief = tmp_path / "brief.md"
    brief.write_text(
        "题目：真实适配\n部分：分析；结论\n字数上限：500\n引用格式：APA 7\n网络图片最少：2",
        encoding="utf-8",
    )
    db = tmp_path / "state.sqlite"
    with open_agent(db, adapter(url)) as agent:
        result = agent.start_materials(brief, run_id="live")
        assert len(requests) == 2
        assert requests[0]["path"] == "/v1/chat/completions"
        assert "references/brainstorming-research/guide.md" in context_paths(requests[0])
        assert "references/paper-orchestration/guide.md" in context_paths(requests[1])
        assert "references/writing-chapters/guide.md" not in context_paths(requests[1])
        assert all(request["authorization"] == "Bearer " + KEY for request in requests)
        assert KEY not in json.dumps(result)
        result = agent.decide("live", choice(result, "revise", "增加例子"))
        assert "增加例子" in result["state"]["outline"]["content"]
        assert result["state"]["outline"]["version"] == 2
    with open_agent(db, adapter(url, key="rotated-test-key")) as agent:
        assert agent.recover("live") == result
        assert len(requests) == 3
        old = choice(result)
        result = agent.decide("live", old)
        payload = json.loads(requests[-1]["body"]["messages"][1]["content"])
        assert payload["current_section"] == "分析"
        assert payload["approved_outline"]
        assert payload["materials"][0]["units"][0]["text"] == "题目：真实适配"
        assert "path" not in payload["materials"][0]
        assert "references/writing-chapters/guide.md" in context_paths(requests[-1])
        assert "references/paper-orchestration/guide.md" not in context_paths(requests[-1])
        with pytest.raises(ValueError, match="过期"):
            agent.decide("live", old)
        result = agent.decide("live", choice(result, "revise", "解释因果关系"))
        assert "解释因果关系" in result["state"]["drafts"][0]["content"]
        result = agent.decide("live", choice(result))
        payload = json.loads(requests[-1]["body"]["messages"][1]["content"])
        assert len(payload["confirmed_sections"]) == 1
        assert payload["current_section"] == "结论"
        result = agent.decide("live", choice(result))
        assert result["state"]["status"] == "completed"
    assert KEY.encode() not in db.read_bytes()
    assert b"rotated-test-key" not in db.read_bytes()


def test_api_failure_checkpoint_and_model_switch(compatible_server, tmp_path):
    url, requests, replies = compatible_server
    brief = tmp_path / "brief.txt"
    brief.write_text(
        "题目：故障恢复\n部分：分析\n字数上限：500\n引用格式：APA 7\n网络图片最少：2",
        encoding="utf-8",
    )
    db = tmp_path / "state.sqlite"
    replies.append((401, KEY))
    with open_agent(db, adapter(url)) as agent:
        with pytest.raises(ModelError, match="任务 ID：failed") as exc:
            agent.start_materials(brief, run_id="failed")
        assert KEY not in str(exc.value)
    with open_agent(db, adapter(url, model="switched-model")) as agent:
        result = agent.recover("failed")
        assert result["state"]["outline"]
        assert result["state"]["model_profile"]["model"] == "switched-model"
        assert requests[-1]["body"]["model"] == "switched-model"
        replies.append((503, KEY))
        with pytest.raises(ModelError, match="503"):
            agent.decide("failed", choice(result))
    with open_agent(db, adapter(url, model="switched-model")) as agent:
        result = agent.recover("failed")
        assert result["state"]["drafts"]
        assert len(result["state"]["approvals"]) == 1
    with open_agent(db) as agent:
        with pytest.raises(ModelError, match="模型类型与任务记录不兼容"):
            agent.decide("failed", choice(result))
    assert KEY.encode() not in db.read_bytes()


def test_status_and_waiting_resume_do_not_require_credentials(compatible_server, tmp_path):
    url, requests, _ = compatible_server
    from assignment_agent.schemas import AssignmentSpec

    db = tmp_path / "state.sqlite"
    with open_agent(db, adapter(url)) as agent:
        result = agent.start(AssignmentSpec(title="测试", sections=["分析"], word_limit=500), "a")
    with open_agent(db, OpenAICompatibleModel(env_file=tmp_path / "missing.env")) as agent:
        assert agent.status("a") == result
        assert agent.recover("a") == result
        assert len(requests) == 1
