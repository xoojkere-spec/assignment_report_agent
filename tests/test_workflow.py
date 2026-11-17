import pytest
from conftest import legacy_agent as open_agent

from assignment_agent.fake_model import FakeModel
from assignment_agent.schemas import AssignmentSpec, Decision


def spec():
    return AssignmentSpec(title="演示作业", sections=["分析", "结论"], word_limit=500)


def decision(result, action="approve", feedback=""):
    pending = result["state"]["pending"]
    return Decision(
        interrupt_id=result["interrupt_id"],
        artifact_id=pending["artifact_id"],
        version=pending["version"],
        action=action,
        feedback=feedback,
    )


def test_pause_revise_restart_and_complete(tmp_path):
    db = tmp_path / "state.sqlite"
    with open_agent(db) as agent:
        result = agent.start(spec(), "demo")
        assert result["state"]["drafts"] == []
        assert result["state"]["status"] == "awaiting_user"
        old = decision(result)
        result = agent.decide("demo", decision(result, "revise", "增加对比"))
        assert result["state"]["outline"]["version"] == 2
        assert "增加对比" in result["state"]["outline"]["content"]
        with pytest.raises(ValueError, match="过期"):
            agent.decide("demo", old)
    with open_agent(db) as agent:
        assert agent.recover("demo") == result
        result = agent.decide("demo", decision(result))
        assert len(result["state"]["drafts"]) == 1
        result = agent.decide("demo", decision(result, "revise", "解释具体原因"))
        assert result["state"]["current_index"] == 0
        assert len(result["state"]["drafts"]) == 1
        assert result["state"]["drafts"][0]["version"] == 2
        assert "解释具体原因" in result["state"]["drafts"][0]["content"]
        assert len(result["state"]["history"]) == 4
    with open_agent(db) as agent:
        assert agent.status("demo") == result
        approval = decision(result)
        result = agent.decide("demo", approval)
        assert len(result["state"]["drafts"]) == 2
        assert result["state"]["status"] == "awaiting_user"
        with pytest.raises(ValueError, match="过期"):
            agent.decide("demo", approval)
        result = agent.decide("demo", decision(result))
        assert result["state"]["status"] == "completed"
        assert result["state"]["pending"] is None
        assert len(result["state"]["approvals"]) == 5
    with open_agent(db) as agent:
        assert agent.recover("demo") == result


def test_invalid_requests_do_not_mutate_state(tmp_path):
    with open_agent(tmp_path / "state.sqlite") as agent:
        with pytest.raises(ValueError, match="不存在"):
            agent.status("missing")
        result = agent.start(spec(), "a")
        with pytest.raises(ValueError, match="已存在"):
            agent.start(spec(), "a")
        bad = decision(result).model_copy(update={"version": 99})
        with pytest.raises(ValueError, match="版本"):
            agent.decide("a", bad)
        assert agent.status("a") == result


def test_runs_are_isolated(tmp_path):
    with open_agent(tmp_path / "state.sqlite") as agent:
        a = agent.start(spec(), "a")
        b = agent.start(spec(), "b")
        agent.decide("a", decision(a))
        assert agent.status("b") == b


def test_generation_failure_can_resume_without_regenerating_outline(tmp_path):
    class FailingModel(FakeModel):
        def section(self, state, context):
            raise RuntimeError("模拟模型故障")

    db = tmp_path / "state.sqlite"
    with open_agent(db, FailingModel()) as agent:
        result = agent.start(spec(), "demo")
        with pytest.raises(RuntimeError, match="模拟模型故障"):
            agent.decide("demo", decision(result))
    with open_agent(db) as agent:
        result = agent.recover("demo")
        assert result["state"]["outline"]["version"] == 1
        assert len(result["state"]["approvals"]) == 1
        assert len(result["state"]["drafts"]) == 1
        assert result["interrupt_id"] is not None
