import pytest
from conftest import legacy_agent as open_agent
from pydantic import ValidationError

from assignment_agent.schemas import AssignmentSpec, Decision, OutlinePlan


def approve(agent, run_id, result, action="approve", feedback=""):
    pending = result["state"]["pending"]
    return agent.decide(
        run_id,
        Decision(
            interrupt_id=result["interrupt_id"],
            artifact_id=pending["artifact_id"],
            version=pending["version"],
            action=action,
            feedback=feedback,
        ),
    )


def test_recommendation_is_confirmed_before_writing_each_chapter(tmp_path):
    db = tmp_path / "state.sqlite"
    spec = AssignmentSpec(title="自动规划", section_mode="recommend", word_limit=900)
    with open_agent(db) as agent:
        result = agent.start(spec, "recommended")
        assert result["state"]["spec"]["sections"] == []
        assert len(result["state"]["outline_plan"]["parts"]) == 3
        assert result["state"]["drafts"] == []
        result = approve(agent, "recommended", result, "revise", "强调案例分析")
        assert result["state"]["outline"]["version"] == 2
        assert "强调案例分析" in result["state"]["outline"]["content"]
        assert result["state"]["drafts"] == []
    with open_agent(db) as agent:
        assert agent.recover("recommended") == result
        result = approve(agent, "recommended", result)
        assert result["state"]["spec"]["sections"] == ["引言", "主体分析", "总结"]
        assert len(result["state"]["drafts"]) == 1
        assert result["state"]["pending"]["artifact_id"] == "section-1"
        for index in range(3):
            assert len(result["state"]["drafts"]) == index + 1
            assert result["state"]["status"] == "awaiting_user"
            result = approve(agent, "recommended", result)
        assert result["state"]["status"] == "completed"
        assert len(result["state"]["drafts"]) == 3


def test_replan_preserves_completed_legacy_task(tmp_path):
    with open_agent(tmp_path / "state.sqlite") as agent:
        legacy = agent.start(
            AssignmentSpec(title="旧任务", sections=["按照你的方式帮我推荐划分"], word_limit=900),
            "legacy",
        )
        legacy = approve(agent, "legacy", legacy)
        legacy = approve(agent, "legacy", legacy)
        assert legacy["state"]["status"] == "completed"
        result = agent.replan("legacy", "new-plan")
        assert result["state"]["parent_run_id"] == "legacy"
        assert result["state"]["drafts"] == []
        assert result["state"]["spec"]["sections"] == []
        assert len(result["state"]["outline_plan"]["parts"]) == 3
        assert agent.status("legacy") == legacy


def test_manual_empty_sections_and_duplicate_plan_are_rejected():
    with pytest.raises(ValidationError):
        AssignmentSpec(title="测试", word_limit=500)
    with pytest.raises(ValidationError):
        OutlinePlan.model_validate(
            {
                "parts": [
                    {"title": "相同标题", "word_budget": 100, "purpose": "分析"},
                    {"title": "相同标题", "word_budget": 100, "purpose": "比较"},
                ]
            }
        )
