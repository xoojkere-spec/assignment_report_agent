import json
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from io import BytesIO

import pytest
from conftest import legacy_agent
from docx import Document
from test_workflow import decision, spec

from assignment_agent.migrations import maintenance
from assignment_agent.migrations.catalog import BUSINESS_SQL
from assignment_agent.migrations.inspection import MigrationError
from assignment_agent.migrations.runner import Migration, apply_steps


@pytest.mark.parametrize("after_commit", [False, True])
def test_process_termination_and_restart_preserves_atomic_migration(tmp_path, after_commit):
    db, ready = tmp_path / "db", tmp_path / "ready"
    script = """
import sqlite3,sys,time
from pathlib import Path
from assignment_agent.migrations.runner import Migration,apply_steps
db,ready,after=sys.argv[1:]
c=sqlite3.connect(db,isolation_level=None)
def first(c):
    c.execute("CREATE TABLE preserved(value TEXT)")
    c.execute("INSERT INTO preserved VALUES ('original')")
def second(c):
    c.execute("CREATE TABLE interrupted(value TEXT)")
    if after=='False':
        Path(ready).write_text('ready')
        time.sleep(30)
    c.execute("INSERT INTO interrupted VALUES ('finished')")
apply_steps(c,[Migration(1,'first','first',first),Migration(2,'second','second',second)])
Path(ready).write_text('ready')
time.sleep(30)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(db), str(ready), str(after_commit)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "故障注入进程未到达目标提交位置"
    finally:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=5)

    def first(connection):
        pytest.fail("已提交的第一步不能重做")

    def second(connection):
        connection.execute("CREATE TABLE interrupted(value TEXT)")
        connection.execute("INSERT INTO interrupted VALUES ('finished')")

    with closing(sqlite3.connect(db, isolation_level=None)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT * FROM preserved").fetchall() == [("original",)]
        expected = [1, 2] if after_commit else [1]
        assert [
            r[0] for r in connection.execute("SELECT version FROM schema_migrations")
        ] == expected
        applied = apply_steps(
            connection,
            [Migration(1, "first", "first", first), Migration(2, "second", "second", second)],
        )
        assert applied == ([] if after_commit else [2])
        assert connection.execute("SELECT * FROM interrupted").fetchall() == [("finished",)]


def test_backup_failure_prevents_any_schema_or_data_upgrade(tmp_path, monkeypatch):
    db = tmp_path / "db"
    with sqlite3.connect(db) as connection:
        connection.execute(BUSINESS_SQL["research_runs"])
        connection.execute("INSERT INTO research_runs VALUES ('r','c','{}')")
    before = db.read_bytes()

    def fail(*args):
        raise OSError("模拟磁盘写入失败")

    monkeypatch.setattr(maintenance, "backup", fail)
    with pytest.raises(OSError, match="磁盘"):
        maintenance.migrate(db)
    assert db.read_bytes() == before
    reports = list((tmp_path / "migration-reports").glob("*.json"))
    assert len(reports) == 1
    assert json.loads(reports[0].read_text(encoding="utf-8"))["status"] == "failed"


def test_old_checkpoint_upgrades_then_resumes_and_exports(tmp_path):
    db = tmp_path / "agent.sqlite"
    with legacy_agent(db) as agent:
        original = agent.start(spec(), "run")
    with sqlite3.connect(db) as connection:
        connection.execute("DROP TABLE schema_migrations")
        before = connection.execute("SELECT * FROM checkpoints ORDER BY checkpoint_id").fetchall()
    with pytest.raises(MigrationError, match="db migrate"), legacy_agent(db):
        pytest.fail("旧库不能隐式迁移")
    result = subprocess.run(
        [sys.executable, "-m", "assignment_agent", "--db", str(db), "db", "migrate", "--offline"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    assert json.loads(result.stdout)["applied"] == [1]
    with closing(sqlite3.connect(db)) as connection:
        assert (
            connection.execute("SELECT * FROM checkpoints ORDER BY checkpoint_id").fetchall()
            == before
        )
    with legacy_agent(db) as agent:
        current = agent.status("run")
        assert current["interrupt_id"] == original["interrupt_id"]
        assert current["state"] == original["state"]
        while current["state"]["pending"]:
            current = agent.decide("run", decision(current))
        assert current["state"]["status"] == "completed"
        agent.confirm_completion("run", current["delivery"]["content_hash"])
        raw, title = agent.export_docx("run")
    assert title == spec().title
    document = Document(BytesIO(raw))
    assert any("分析" in p.text for p in document.paragraphs)


def test_legacy_checkpoint_writes_column_is_preserved_by_dependency_upgrade(tmp_path):
    from assignment_agent.migrations.inspection import CHECKPOINT_SQL

    db = tmp_path / "db"
    with sqlite3.connect(db) as connection:
        connection.execute(CHECKPOINT_SQL["checkpoints"])
        connection.execute(
            CHECKPOINT_SQL["writes"].replace("task_path TEXT NOT NULL DEFAULT '', ", "")
        )
        connection.execute(
            "INSERT INTO writes VALUES ('r','','checkpoint','task',0,'pending','json',?)", (b"{}",)
        )
    maintenance.migrate(db)
    with legacy_agent(db) as agent:
        agent.graph.get_state(agent._config("missing"))
    with closing(sqlite3.connect(db)) as connection:
        assert connection.execute(
            "SELECT task_id,idx,channel,type,value,task_path FROM writes"
        ).fetchall() == [("task", 0, "pending", "json", b"{}", "")]


def test_future_run_state_blocks_migration_before_any_database_changes(tmp_path):
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    db = tmp_path / "db"
    with legacy_agent(db) as agent:
        agent.start(spec(), "run")
    serde = JsonPlusSerializer()
    with sqlite3.connect(db) as connection:
        connection.execute("DROP TABLE schema_migrations")
        rows = connection.execute(
            "SELECT checkpoint_id,type,checkpoint FROM checkpoints"
        ).fetchall()
        for checkpoint_id, kind, raw in rows:
            value = serde.loads_typed((kind, raw))
            if isinstance(value.get("channel_values", {}).get("run"), dict):
                value["channel_values"]["run"]["schema_version"] = 2
                kind, raw = serde.dumps_typed(value)
                connection.execute(
                    "UPDATE checkpoints SET type=?,checkpoint=? WHERE checkpoint_id=?",
                    (kind, raw, checkpoint_id),
                )
    before = db.read_bytes()
    with pytest.raises(MigrationError, match="任务状态"):
        maintenance.migrate(db)
    assert db.read_bytes() == before
