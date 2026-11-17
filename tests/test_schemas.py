import pytest
from pydantic import ValidationError

from assignment_agent.schemas import AssignmentSpec, Decision, RunState


def test_state_round_trip():
    state = RunState(spec=AssignmentSpec(title="演示", sections=["分析", "结论"], word_limit=500))
    assert RunState.model_validate_json(state.model_dump_json()) == state


@pytest.mark.parametrize(
    "overrides",
    [
        {"word_limit": 0},
        {"sections": []},
        {"sections": ["重复", "重复"]},
        {"title": " "},
        {"sections": [" "]},
    ],
)
def test_invalid_spec(overrides):
    data = {"title": "演示", "sections": ["分析"], "word_limit": 500} | overrides
    with pytest.raises(ValidationError):
        AssignmentSpec.model_validate(data)


def test_revision_requires_feedback():
    with pytest.raises(ValidationError):
        Decision(interrupt_id="i", artifact_id="outline", version=1, action="revise")
