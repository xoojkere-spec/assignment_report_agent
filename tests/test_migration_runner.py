import sqlite3
import subprocess
import sys
from contextlib import closing

import pytest

from assignment_agent.migrations.inspection import MigrationError
from assignment_agent.migrations.locking import database_lock
from assignment_agent.migrations.runner import Migration, apply_steps


def step(version, name="create", source="create", fail=False):
    def apply(connection):
        connection.execute(f"CREATE TABLE data_{version} (value TEXT)")
        connection.execute(f"INSERT INTO data_{version} VALUES ('preserved')")
        if fail:
            raise RuntimeError("故障注入")

    return Migration(version, name, source, apply)


def test_migration_repeated_execution_and_checksum(tmp_path):
    with closing(sqlite3.connect(tmp_path / "db", isolation_level=None)) as connection:
        assert apply_steps(connection, [step(1)]) == [1]
        assert apply_steps(connection, [step(1)]) == []
        with pytest.raises(MigrationError, match="校验值"):
            apply_steps(connection, [step(1, source="changed")])
        assert connection.execute("SELECT * FROM data_1").fetchall() == [("preserved",)]


def test_failed_step_rolls_back_data_and_record_then_can_resume(tmp_path):
    with closing(sqlite3.connect(tmp_path / "db", isolation_level=None)) as connection:
        with pytest.raises(RuntimeError):
            apply_steps(connection, [step(1), step(2, fail=True)])
        assert connection.execute("SELECT version FROM schema_migrations").fetchall() == [(1,)]
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name='data_2'").fetchone()
        assert apply_steps(connection, [step(1), step(2)]) == [2]


@pytest.mark.parametrize("versions", [[2], [1, 3]])
def test_migration_definition_gaps_rejected(tmp_path, versions):
    with closing(sqlite3.connect(tmp_path / "db", isolation_level=None)) as connection:
        with pytest.raises(MigrationError, match="不连续"):
            apply_steps(connection, [step(v) for v in versions])
        assert connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0


def test_failed_initial_migration_leaves_no_ledger(tmp_path):
    with closing(sqlite3.connect(tmp_path / "db", isolation_level=None)) as connection:
        with pytest.raises(RuntimeError):
            apply_steps(connection, [step(1, fail=True)])
        assert connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0


def test_migration_lock_is_reentrant_and_blocks_another_process(tmp_path):
    db = tmp_path / "db"
    code = (
        "from assignment_agent.migrations.locking import database_lock; import sys\n"
        "with database_lock(sys.argv[1]): pass"
    )
    with database_lock(db), database_lock(db):
        result = subprocess.run([sys.executable, "-c", code, str(db)], capture_output=True)
        assert result.returncode != 0
    result = subprocess.run([sys.executable, "-c", code, str(db)], capture_output=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("operation", ["commit", "executescript"])
def test_migration_cannot_commit_its_partial_changes(tmp_path, operation):
    def apply(connection):
        connection.execute("CREATE TABLE partial(value TEXT)")
        if operation == "commit":
            connection.commit()
        else:
            connection.executescript("INSERT INTO partial VALUES ('invalid commit')")

    with closing(sqlite3.connect(tmp_path / "db", isolation_level=None)) as connection:
        with pytest.raises(sqlite3.DatabaseError):
            apply_steps(connection, [Migration(1, "bad", "bad", apply)])
        assert connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0
