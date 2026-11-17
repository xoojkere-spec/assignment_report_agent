import copy
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from conftest import legacy_agent
from pydantic import ValidationError
from test_workflow import decision, spec

from assignment_agent.migrations.backup import backup, restore
from assignment_agent.schemas import RunState


def test_unversioned_state_is_upgraded_without_mutation():
    state = RunState(spec=spec()).model_dump(mode="json")
    state.pop("schema_version")
    before = copy.deepcopy(state)
    restored = RunState.model_validate(state)
    assert restored.schema_version == 1
    assert restored.spec == spec()
    assert state == before


@pytest.mark.parametrize("version", [0, 2, -1, True, "1", None])
def test_unknown_or_invalid_state_version_is_rejected(version):
    with pytest.raises(ValidationError, match="版本"):
        RunState.model_validate({"schema_version": version})


def test_unknown_old_fields_remain_rejected():
    with pytest.raises(ValidationError):
        RunState.model_validate({"invented_legacy_field": "do not drop"})


def checkpoints(db):
    with closing(sqlite3.connect(db)) as connection:
        return connection.execute("SELECT * FROM checkpoints ORDER BY checkpoint_id").fetchall()


def test_restored_task_reads_new_files_and_resumes_without_rewriting_history(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    material = source / "brief.md"
    material.write_text(
        "题目：恢复测试\n部分：分析；结论\n字数上限：500\n引用格式：APA 7\n网络图片最少：2",
        encoding="utf-8",
    )
    db = source / "agent.sqlite"
    with legacy_agent(db) as agent:
        original = agent.start_materials(material, run_id="run")
    before = checkpoints(db)
    backup(db, tmp_path / "backup")
    restored = Path(restore(tmp_path / "backup", tmp_path / "restored")["database"])
    source.rename(tmp_path / "unavailable")
    with legacy_agent(restored) as agent:
        current = agent.status("run")
        assert current["interrupt_id"] == original["interrupt_id"]
        assert current["next_nodes"] == original["next_nodes"]
        assert checkpoints(restored) == before
        assert Path(current["state"]["materials"][0]["path"]).is_file()
        assert Path(current["state"]["skill_root"]).is_dir()
        resumed = agent.decide("run", decision(current))
        assert len(resumed["state"]["drafts"]) == 1
        assert (
            resumed["state"]["history"][: len(original["state"]["history"])]
            == original["state"]["history"]
        )
    backup(restored, tmp_path / "backup-again")
    again = Path(restore(tmp_path / "backup-again", tmp_path / "again")["database"])
    with legacy_agent(again) as agent:
        assert Path(agent.status("run")["state"]["skill_root"]).is_dir()


def test_bad_restore_mapping_is_rejected(tmp_path):
    from assignment_agent.state_versions import load_run

    db = tmp_path / "agent.sqlite"
    db.with_suffix(".sqlite.paths.json").write_text(
        json.dumps([{"original": str(tmp_path), "restored": str(tmp_path.parent)}]),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="越出"):
        load_run({}, db)
