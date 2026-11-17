import json
import sqlite3
from contextlib import closing

import pytest
from test_research import research_network as research_network

from assignment_agent.migrations.catalog import BUSINESS_SQL
from assignment_agent.migrations.inspection import MigrationError, inspect
from assignment_agent.migrations.maintenance import migrate


def test_empty_database_initialization_and_repeat(tmp_path):
    db = tmp_path / "agent.sqlite"
    assert migrate(db)["applied"] == [1]
    assert inspect(db)["status"] == "current"
    with closing(sqlite3.connect(db)) as connection:
        before = connection.execute("SELECT * FROM schema_migrations").fetchall()
    assert migrate(db)["applied"] == []
    with closing(sqlite3.connect(db)) as connection:
        assert connection.execute("SELECT * FROM schema_migrations").fetchall() == before


def test_future_business_archive_changes_do_not_rewrite_historical_migration(tmp_path, monkeypatch):
    from assignment_agent import research_library

    db = tmp_path / "agent.sqlite"
    migrate(db)

    def future_archive(*args, **kwargs):
        raise RuntimeError("未来业务实现不能改变已经发布的迁移")

    monkeypatch.setattr(research_library, "archive", future_archive)
    assert inspect(db)["status"] == "current"
    assert migrate(db)["applied"] == []


def test_partial_database_upgrade_preserves_budget_diagnostics_and_delivery(tmp_path):
    db = tmp_path / "agent.sqlite"
    with sqlite3.connect(db) as connection:
        for name in ("research_search_attempts", "run_diagnostics", "completion_records"):
            connection.execute(BUSINESS_SQL[name])
        connection.execute(
            "INSERT INTO research_search_attempts VALUES (7,'run',2,'crossref','stamp')"
        )
        connection.execute("INSERT INTO run_diagnostics VALUES (9,'run','stamp','event','{}')")
        connection.execute("INSERT INTO completion_records VALUES ('run','digest','stamp')")
    with closing(sqlite3.connect(db)) as connection:
        original = list(connection.iterdump())
    result = migrate(db, tmp_path / "backup")
    with closing(sqlite3.connect(tmp_path / "backup/database.sqlite")) as connection:
        assert list(connection.iterdump()) == original
    assert result["status"] == "completed"
    with closing(sqlite3.connect(db)) as connection:
        assert connection.execute("SELECT * FROM research_search_attempts").fetchall() == [
            (7, "run", 2, "crossref", "stamp")
        ]
        assert connection.execute("SELECT * FROM run_diagnostics").fetchall() == [
            (9, "run", "stamp", "event", "{}")
        ]
        assert connection.execute("SELECT * FROM completion_records").fetchall() == [
            ("run", "digest", "stamp")
        ]


def test_legacy_source_candidates_preserved_and_conflicts_roll_back(tmp_path):
    db = tmp_path / "agent.sqlite"
    with sqlite3.connect(db) as connection:
        connection.execute(BUSINESS_SQL["research_candidates"])
        connection.execute(
            "CREATE TABLE research_entries AS SELECT * FROM research_candidates WHERE 0"
        )
        connection.execute("INSERT INTO research_entries VALUES ('r','c','source','s','{}')")
    migrate(db, tmp_path / "backup")
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT * FROM research_candidates").fetchall() == [
            ("r", "c", "source", "s", "{}")
        ]
        connection.execute("DROP TABLE schema_migrations")
        connection.execute(
            "CREATE TABLE research_entries AS SELECT * FROM research_candidates WHERE 0"
        )
        connection.execute(
            "INSERT INTO research_entries VALUES ('r','c','source','s','{\"changed\":true}')"
        )
    with pytest.raises(MigrationError, match="冲突"):
        migrate(db, tmp_path / "conflict-backup")
    with closing(sqlite3.connect(db)) as connection:
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='schema_migrations'"
        ).fetchone()
        assert connection.execute("SELECT COUNT(*) FROM research_entries").fetchone()[0] == 1
    records = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in (tmp_path / "migration-reports").glob("*.json")
    ]
    assert any(record["status"] == "failed" and "backup" in record for record in records)


def test_tampered_migration_history_refuses_writes(tmp_path):
    db = tmp_path / "agent.sqlite"
    migrate(db)
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE schema_migrations SET checksum='wrong'")
    before = db.read_bytes()
    assert inspect(db)["status"] == "blocked"
    with pytest.raises(MigrationError, match="校验值"):
        migrate(db)
    assert db.read_bytes() == before


def test_formal_history_migration_preserves_evidence_and_source_assets(tmp_path, research_network):
    from test_grounded_writing import automatic_state
    from test_research_library import pipeline, set_draft

    from assignment_agent.research_store import ResearchStore, context_hash

    p = pipeline(tmp_path, research_network)
    run = automatic_state()
    content, bundle = p.write("run", run, {}, 1)
    set_draft(run, content, bundle)
    records = [
        (kind, item)
        for kind in ("source", "figure")
        for item in p.store.entries("run", context_hash(run), kind)
    ]
    with p.store.connect() as connection:
        connection.execute(
            "CREATE TABLE research_entries AS SELECT * FROM research_candidates WHERE 0"
        )
        for kind, item in records:
            connection.execute(
                "INSERT INTO research_entries VALUES (?,?,?,?,?)",
                ("run", context_hash(run), kind, item["id"], json.dumps(item)),
            )
        connection.execute("DELETE FROM research_library_versions")
        # 模拟真正的无版本历史库，不把已版本化库的结构损坏当成可迁移旧库。
        connection.execute("DROP TABLE schema_migrations")
        evidence = connection.execute("SELECT * FROM writing_evidence").fetchall()
    migrate(p.store.database, tmp_path / "backup")
    restored = ResearchStore(p.store.database)
    library = restored.library_summary("run", run)
    assert {s["id"] for s in library["sources"]} == set(bundle["sources"])
    assert all(restored.asset_valid(s["pdf"]) for s in library["sources"])
    with restored.connect() as connection:
        assert connection.execute("SELECT * FROM writing_evidence").fetchall() == evidence
