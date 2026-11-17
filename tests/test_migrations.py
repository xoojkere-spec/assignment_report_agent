import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from assignment_agent.migrations.catalog import BUSINESS_SQL
from assignment_agent.migrations.inspection import MigrationError, inspect, plan
from assignment_agent.migrations.maintenance import migrate


def legacy_database(path, names=None):
    with sqlite3.connect(path) as connection:
        for name in names or BUSINESS_SQL:
            connection.execute(BUSINESS_SQL[name])
    return path


def test_inspection_missing_does_not_create_file(tmp_path):
    db = tmp_path / "missing" / "db.sqlite"
    assert plan(db)["pending"] == [1]
    assert not db.parent.exists()


def test_known_partial_legacy_inspection_is_readonly(tmp_path):
    db = legacy_database(tmp_path / "db", ["run_diagnostics"])
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    assert inspect(db)["status"] == "unversioned"
    assert plan(db)["requires_backup"]
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE unrelated (id TEXT)",
        "CREATE TABLE research_runs (run_id TEXT)",
        "CREATE VIEW secret AS SELECT 1",
        BUSINESS_SQL["research_runs"] + "; CREATE INDEX custom ON research_runs(run_id)",
    ],
)
def test_unknown_structure_blocked(tmp_path, sql):
    db = tmp_path / "db"
    with sqlite3.connect(db) as connection:
        connection.executescript(sql)
    before = db.read_bytes()
    assert inspect(db)["status"] == "blocked"
    with pytest.raises(MigrationError):
        plan(db)
    assert db.read_bytes() == before


def test_corrupt_database_blocked(tmp_path):
    db = tmp_path / "db"
    db.write_bytes(b"not sqlite")
    assert inspect(db)["status"] == "blocked"


@pytest.mark.parametrize("operation", ["status", "plan"])
def test_management_cli_needs_no_model_and_is_readonly(tmp_path, operation):
    db = tmp_path / "missing"
    result = subprocess.run(
        [sys.executable, "-m", "assignment_agent", "--db", str(db), "db", operation],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    assert json.loads(result.stdout)["status"] == "missing"
    assert not db.exists()


def test_catalog_matches_frozen_inventory():
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/database-schema-v0.json").read_text(encoding="utf-8")
    )
    assert BUSINESS_SQL == {name: data["sql"] for name, data in fixture["business_tables"].items()}


@pytest.mark.parametrize("prefix", ["sqliteX", "SQLITEX"])
@pytest.mark.parametrize("kind", ["table", "view", "index", "trigger"])
def test_sqlite_lookalike_user_objects_are_not_hidden(tmp_path, prefix, kind):
    from assignment_agent.research_store import ResearchStore

    db = tmp_path / "db"
    migrate(db)
    name = prefix + "_foreign"
    statements = {
        "table": f"CREATE TABLE {name} (id TEXT)",
        "view": f"CREATE VIEW {name} AS SELECT run_id FROM research_runs",
        "index": f"CREATE INDEX {name} ON research_runs(run_id)",
        "trigger": f"CREATE TRIGGER {name} AFTER INSERT ON research_runs BEGIN SELECT 1; END",
    }
    with sqlite3.connect(db) as connection:
        connection.execute(statements[kind])
    before = db.read_bytes()
    assert inspect(db)["status"] == "blocked"
    for operation in (lambda: plan(db), lambda: migrate(db), lambda: ResearchStore(db).run("r")):
        with pytest.raises(MigrationError, match="未知"):
            operation()
    assert db.read_bytes() == before


def test_real_sqlite_internal_objects_remain_compatible(tmp_path):
    db = tmp_path / "db"
    migrate(db)
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE scratch (id INTEGER PRIMARY KEY AUTOINCREMENT)")
        connection.execute("DROP TABLE scratch")
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'"
        ).fetchone()
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name LIKE 'sqlite_autoindex_%'"
        ).fetchone()
    assert inspect(db)["status"] == "current"


def test_versioned_database_missing_required_index_is_blocked(tmp_path):
    from assignment_agent.research_store import ResearchStore

    db = tmp_path / "db"
    migrate(db)
    with sqlite3.connect(db) as connection:
        connection.execute("DROP INDEX research_search_attempts_run_chapter")
    before = db.read_bytes()
    assert inspect(db)["status"] == "blocked"
    for operation in (lambda: plan(db), lambda: migrate(db), lambda: ResearchStore(db).run("r")):
        with pytest.raises(MigrationError, match="索引"):
            operation()
    assert db.read_bytes() == before


def test_unversioned_missing_index_is_created_without_resetting_budget(tmp_path):
    db = legacy_database(tmp_path / "db", ["research_search_attempts"])
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO research_search_attempts VALUES (1,'r',2,'channel','stamp')"
        )
    assert inspect(db)["status"] == "unversioned"
    assert migrate(db, tmp_path / "backup")["applied"] == [1]
    assert inspect(db)["status"] == "current"
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT name FROM pragma_index_info('research_search_attempts_run_chapter')"
        ).fetchall() == [("run_id",), ("chapter",)]
        assert connection.execute("SELECT * FROM research_search_attempts").fetchall() == [
            (1, "r", 2, "channel", "stamp")
        ]
