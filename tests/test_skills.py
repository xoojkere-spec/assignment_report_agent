from pathlib import Path
from shutil import copytree

import pytest

from assignment_agent.schemas import RunState
from assignment_agent.skills import DEFAULT_SKILL_ROOT, active_context, load_stage, snapshot_skill


def test_lazy_context_and_immutable_snapshot(tmp_path):
    source = tmp_path / "source"
    copytree(DEFAULT_SKILL_ROOT, source)
    root, manifest = snapshot_skill(source, tmp_path / "snapshots")
    run = RunState(skill_root=root, skill_manifest=manifest)
    load_stage(run, "parse")
    assert len(active_context(run)) == 3
    assert not any("writing-chapters" in name for name in run.skill_documents)
    path = "references/writing-chapters/guide.md"
    (source / path).write_text("源文件已经修改", encoding="utf-8")
    load_stage(run, "section")
    assert "源文件已经修改" not in active_context(run)[path]
    assert not any("brainstorming-research" in name for name in active_context(run))
    assert not any("literature-review" in name for name in active_context(run))


def test_tampered_or_incomplete_skill_is_rejected(tmp_path):
    root, manifest = snapshot_skill(DEFAULT_SKILL_ROOT, tmp_path / "snapshots")
    (Path(root) / "SKILL.md").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="校验失败"):
        load_stage(RunState(skill_root=root, skill_manifest=manifest), "parse")
    source = tmp_path / "bad"
    source.mkdir()
    (source / "SKILL.md").write_text("name: undergraduate-assignment", encoding="utf-8")
    with pytest.raises(ValueError, match="缺少"):
        snapshot_skill(source, tmp_path / "other")
