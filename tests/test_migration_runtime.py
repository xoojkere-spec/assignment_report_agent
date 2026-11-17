import sqlite3

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from assignment_agent import advisor, delivery, diagnostics
from assignment_agent.migrations.backup import backup
from assignment_agent.migrations.catalog import BUSINESS_SQL
from assignment_agent.migrations.inspection import CHECKPOINT_SQL, MigrationError, inspect, plan
from assignment_agent.migrations.maintenance import migrate
from assignment_agent.migrations.runtime import access
from assignment_agent.research_store import ResearchStore
from assignment_agent.schemas import RunState
from assignment_agent.service import open_agent
from assignment_agent.web import create_app


def old_database(path):
    with sqlite3.connect(path) as connection:
        connection.execute(BUSINESS_SQL["advisor_exchanges"])
        connection.execute(
            "INSERT INTO advisor_exchanges VALUES "
            "(1,'r','i','question',NULL,'pending',NULL,'stamp',NULL)"
        )
    return path


@pytest.mark.parametrize(
    "operation",
    [
        lambda db: ResearchStore(db).run("r"),
        lambda db: advisor.history(db, "r"),
        lambda db: diagnostics.read_diagnostics(db, "r"),
        lambda db: delivery.delivery_status(db, "r", RunState()),
    ],
)
def test_direct_stores_cannot_bypass_legacy_gate(tmp_path, operation):
    db = old_database(tmp_path / "db")
    before = db.read_bytes()
    with pytest.raises(MigrationError, match="db migrate"):
        operation(db)
    assert db.read_bytes() == before


def test_web_checks_before_startup_changes_or_cleanup(tmp_path):
    db = old_database(tmp_path / "db")
    before = db.read_bytes()
    with pytest.raises(MigrationError, match="db migrate"), TestClient(create_app(db)):
        pass
    assert db.read_bytes() == before
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT status FROM advisor_exchanges").fetchone()[0] == "pending"


def test_future_version_is_rejected_without_model_calls(tmp_path):
    db = tmp_path / "db"
    migrate(db)
    with sqlite3.connect(db) as connection:
        connection.execute("INSERT INTO schema_migrations VALUES (2,'future','unknown','stamp')")
    before = db.read_bytes()
    with pytest.raises(MigrationError, match="更新"), open_agent(db):
        pytest.fail("不应进入任务服务")
    assert db.read_bytes() == before


def test_new_database_initializes_and_nested_access_is_safe(tmp_path):
    db = tmp_path / "new/db"
    with access(db), access(db):
        assert ResearchStore(db).run("missing") is None
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT version FROM schema_migrations").fetchall() == [(1,)]


def test_business_sessions_can_query_while_maintenance_is_blocked(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    db = tmp_path / "db"
    migrate(db)
    with access(db), ThreadPoolExecutor() as pool:
        assert pool.submit(lambda: ResearchStore(db).run("missing")).result(timeout=5) is None
        with pytest.raises(MigrationError, match="占用"):
            pool.submit(lambda: migrate(db)).result(timeout=5)


def add_run_record(database, location, value, *, include_run=True):
    serde = JsonPlusSerializer()
    with sqlite3.connect(database) as connection:
        for sql in CHECKPOINT_SQL.values():
            connection.execute(sql.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS "))
        if location == "checkpoint":
            channels = {"run": value} if include_run else {"other_channel": "initial"}
            kind, raw = serde.dumps_typed({"channel_values": channels})
            connection.execute(
                "INSERT INTO checkpoints VALUES ('r','','c',NULL,?,?,NULL)", (kind, raw)
            )
        else:
            kind, raw = serde.dumps_typed(value)
            channel = "run" if include_run else "other_channel"
            connection.execute(
                "INSERT INTO writes VALUES ('r','','c','task','',0,?,?,?)", (channel, kind, raw)
            )


@pytest.mark.parametrize("location", ["checkpoint", "pending_write"])
@pytest.mark.parametrize("value", [None, "damaged", [], 1])
def test_non_dict_run_blocks_every_entry_before_startup_cleanup(tmp_path, location, value):
    db = tmp_path / "db"
    migrate(db)
    add_run_record(db, location, value)
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO advisor_exchanges VALUES "
            "(1,'r','i','question',NULL,'pending',NULL,'stamp',NULL)"
        )
    before = db.read_bytes()
    assert inspect(db)["status"] == "blocked"
    for operation in (lambda: plan(db), lambda: backup(db, tmp_path / "backup")):
        with pytest.raises(MigrationError, match="任务状态"):
            operation()
    with pytest.raises(MigrationError, match="任务状态"), TestClient(create_app(db)):
        pytest.fail("损坏状态不能进入启动清理")
    assert db.read_bytes() == before
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT status FROM advisor_exchanges").fetchone()[0] == "pending"


@pytest.mark.parametrize("location", ["checkpoint", "pending_write"])
def test_non_dict_run_prevents_legacy_migration(tmp_path, location):
    db = tmp_path / "db"
    migrate(db)
    add_run_record(db, location, "damaged")
    with sqlite3.connect(db) as connection:
        connection.execute("DROP TABLE schema_migrations")
    before = db.read_bytes()
    with pytest.raises(MigrationError, match="任务状态"):
        migrate(db, tmp_path / "backup")
    assert db.read_bytes() == before
    assert not (tmp_path / "backup").exists()


@pytest.mark.parametrize("location", ["checkpoint", "pending_write"])
def test_absent_run_in_other_channels_remains_compatible(tmp_path, location):
    db = tmp_path / "db"
    migrate(db)
    add_run_record(db, location, None, include_run=False)
    assert inspect(db)["status"] == "current"
