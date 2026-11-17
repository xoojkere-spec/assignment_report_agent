from shutil import copytree, rmtree

import pytest
from conftest import legacy_agent as open_agent

from assignment_agent.fake_model import FakeModel
from assignment_agent.schemas import AssignmentSpec, Clarification, Decision
from assignment_agent.skills import DEFAULT_SKILL_ROOT


class InspectingModel(FakeModel):
    def __init__(self):
        self.calls = []

    def extract(self, state, context):
        self.calls.append(("parse", context, len(state.materials)))
        return super().extract(state, context)

    def outline(self, state, context):
        self.calls.append(("outline", context, len(state.materials)))
        return super().outline(state, context)

    def section(self, state, context):
        self.calls.append(("section", context, len(state.materials)))
        return super().section(state, context)


def approve(agent, result):
    pending = result["state"]["pending"]
    return agent.decide(
        result["run_id"],
        Decision(
            interrupt_id=result["interrupt_id"],
            artifact_id=pending["artifact_id"],
            version=pending["version"],
            action="approve",
        ),
    )


def test_materials_and_stage_context_survive_source_deletion(tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_text(
        "题目：演示\n部分：分析；结论\n字数上限：500\n引用格式：APA 7\n网络图片最少：2",
        encoding="utf-8",
    )
    course = tmp_path / "course.txt"
    course.write_text("题目：此处不应覆盖作业题目\n课程案例", encoding="utf-8")
    source = tmp_path / "skill"
    copytree(DEFAULT_SKILL_ROOT, source)
    model = InspectingModel()
    db = tmp_path / "workspace" / "state.sqlite"
    with open_agent(db, model) as agent:
        result = agent.start_materials(brief, courses=[course], run_id="docs", skill_root=source)
        assert result["state"]["spec"]["title"] == "演示"
        assert result["state"]["analysis"]["facts"][2]["location"] == "line:3"
        assert model.calls[0][0] == "parse"
        assert any("brainstorming-research" in name for name in model.calls[0][1])
        assert not any("writing-chapters" in name for name in model.calls[0][1])
        assert model.calls[1][0] == "outline"
        assert model.calls[1][2] == 2
        assert any("paper-orchestration" in name for name in model.calls[1][1])
        assert not any("brainstorming-research" in name for name in model.calls[1][1])
    brief.unlink()
    course.unlink()
    rmtree(source)
    with open_agent(db, model) as agent:
        assert agent.recover("docs") == result
        result = approve(agent, result)
        assert any("literature-review" in name for name in model.calls[-1][1])
        assert any("writing-chapters" in name for name in model.calls[-1][1])
        result = approve(agent, result)
        result = approve(agent, result)
        assert result["state"]["status"] == "completed"
        assert any("verification" in name for name in result["state"]["active_skill_paths"])


@pytest.mark.parametrize("rubric_text", ["评分：比较分析", "字数上限：600"])
def test_missing_or_conflicting_requirements_pause_until_clarified(tmp_path, rubric_text):
    brief = tmp_path / "brief.txt"
    brief.write_text(
        "题目：演示\n部分：分析" + ("\n字数上限：500" if "600" in rubric_text else ""),
        encoding="utf-8",
    )
    rubric = tmp_path / "rubric.md"
    rubric.write_text(rubric_text, encoding="utf-8")
    model = InspectingModel()
    db = tmp_path / "state.sqlite"
    with open_agent(db, model) as agent:
        result = agent.start_materials(brief, rubrics=[rubric], run_id="clarify")
        assert result["state"]["spec"] is None
        assert result["state"]["outline"] is None
        assert result["interrupt_payload"]["kind"] == "requirements"
        assert result["state"]["analysis"]["issues"]
        assert len(model.calls) == 1
    with open_agent(db, model) as agent:
        assert agent.recover("clarify") == result
        answer = Clarification(
            interrupt_id=result["interrupt_id"],
            spec=AssignmentSpec(title="演示", sections=["分析"], word_limit=600),
            note="用户确认按 600 上限执行",
        )
        result = agent.clarify("clarify", answer)
        assert result["state"]["outline"] is not None
        assert result["state"]["drafts"] == []
        assert len(result["state"]["clarifications"]) == 1
        with pytest.raises(ValueError, match="过期"):
            agent.clarify("clarify", answer)


def test_ambiguous_limit_is_not_silently_normalized(tmp_path):
    brief = tmp_path / "brief.txt"
    brief.write_text("Title: Demo\nSections: Analysis\nWord limit: about 500", encoding="utf-8")
    with open_agent(tmp_path / "state.sqlite") as agent:
        result = agent.start_materials(brief)
        assert result["interrupt_payload"]["kind"] == "requirements"
        assert result["state"]["analysis"]["facts"][2]["quote"] == "Word limit: about 500"
