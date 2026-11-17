import hashlib
import json
import sqlite3
from contextlib import closing

import pytest

from assignment_agent.migrations.backup import backup, restore, verify
from assignment_agent.migrations.catalog import BUSINESS_SQL
from assignment_agent.migrations.inspection import MigrationError
from assignment_agent.migrations.maintenance import migrate


def database(path):
    with sqlite3.connect(path) as connection:
        connection.execute(BUSINESS_SQL["research_runs"])
        connection.execute("INSERT INTO research_runs VALUES ('run','ctx','{}')")
    return path


def test_backup_round_trip_including_committed_wal_and_assets(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    db = database(source / "agent.sqlite")
    assets = source / "research-assets"
    assets.mkdir()
    (assets / "image.png").write_bytes(b"image")
    with closing(sqlite3.connect(db)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("INSERT INTO research_runs VALUES ('wal','ctx','{}')")
        connection.commit()
        result = backup(db, tmp_path / "backup")
        assert result["files"] == 1
    restored = restore(tmp_path / "backup", tmp_path / "restored")
    with closing(sqlite3.connect(restored["database"])) as connection:
        assert connection.execute(
            "SELECT run_id FROM research_runs ORDER BY run_id"
        ).fetchall() == [("run",), ("wal",)]
    assert (tmp_path / "restored/research-assets/image.png").read_bytes() == b"image"
    assert db.exists()


@pytest.mark.parametrize("target", ["database.sqlite", "files/research-assets/image.png"])
def test_corrupt_backup_rejected_before_restore(tmp_path, target):
    db = database(tmp_path / "agent.sqlite")
    (tmp_path / "research-assets").mkdir()
    (tmp_path / "research-assets/image.png").write_bytes(b"image")
    backup(db, tmp_path / "backup")
    if target.startswith("files/"):
        target = verify(tmp_path / "backup")["files"][0]["stored"]
    (tmp_path / "backup" / target).write_bytes(b"corrupt")
    with pytest.raises(MigrationError):
        restore(tmp_path / "backup", tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_backup_and_restore_never_overwrite_existing_destination(tmp_path):
    db = database(tmp_path / "agent.sqlite")
    before = db.read_bytes()
    backup(db, tmp_path / "backup")
    with pytest.raises(MigrationError, match="已存在"):
        backup(db, tmp_path / "backup")
    with pytest.raises(MigrationError, match="新目录"):
        restore(tmp_path / "backup", tmp_path)
    assert db.read_bytes() == before


def test_manifest_traversal_is_rejected(tmp_path):
    db = database(tmp_path / "agent.sqlite")
    backup(db, tmp_path / "backup")
    manifest_path = tmp_path / "backup/manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"] = [
        {"relative": "../../agent.sqlite", "sha256": hashlib.sha256(db.read_bytes()).hexdigest()}
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(MigrationError, match="越界"):
        verify(tmp_path / "backup")


def test_backup_reads_material_and_skill_references_without_changing_checkpoint(tmp_path):
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    from assignment_agent.migrations.inspection import CHECKPOINT_SQL

    db = database(tmp_path / "agent.sqlite")
    external = tmp_path / "elsewhere"
    external.mkdir()
    material = external / "brief.md"
    material.write_bytes(b"brief")
    skill = external / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_bytes(b"skill")
    from assignment_agent.schemas import Material, RunState, SourceUnit

    run = RunState(
        materials=[
            Material(
                source_id="material",
                role="assignment",
                path=str(material),
                sha256=hashlib.sha256(b"brief").hexdigest(),
                units=[SourceUnit(location="line:1", text="brief")],
            )
        ],
        skill_root=str(skill),
        skill_manifest={"SKILL.md": hashlib.sha256(b"skill").hexdigest()},
    ).model_dump(mode="json")
    kind, raw = JsonPlusSerializer().dumps_typed({"channel_values": {"run": run}})
    with sqlite3.connect(db) as connection:
        for sql in CHECKPOINT_SQL.values():
            connection.execute(sql)
        connection.execute("INSERT INTO checkpoints VALUES ('r','','c',NULL,?,?,NULL)", (kind, raw))
    backup(db, tmp_path / "backup")
    manifest = verify(tmp_path / "backup")
    assert len(manifest["files"]) == 2
    material.unlink()
    with pytest.raises(MigrationError, match="材料"):
        backup(db, tmp_path / "incomplete")
    assert not (tmp_path / "incomplete").exists()


def add_asset_reference(database, origin, asset):
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    from assignment_agent.migrations.inspection import CHECKPOINT_SQL
    from assignment_agent.schemas import RunState

    value = {"id": "source", "pdf": asset}
    raw = json.dumps(value)
    with sqlite3.connect(database) as connection:
        if origin == "current":
            connection.execute(
                "INSERT INTO research_library_current VALUES ('r','c','source','s',?)", (raw,)
            )
        elif origin == "history":
            connection.execute(
                "INSERT INTO research_library_versions VALUES "
                "('r','c','section-1',1,'hash','source','s','stamp',?)",
                (raw,),
            )
        elif origin in {"legacy", "candidate"}:
            table = "research_entries" if origin == "legacy" else "research_candidates"
            if origin == "legacy":
                connection.execute("DROP TABLE schema_migrations")
                connection.execute(
                    "CREATE TABLE research_entries AS SELECT * FROM research_candidates WHERE 0"
                )
            connection.execute(f"INSERT INTO {table} VALUES ('r','c','source','s',?)", (raw,))
        elif origin == "evidence":
            connection.execute(
                "INSERT INTO writing_evidence VALUES (1,'r','c','section-1',1,'stamp',?)",
                (
                    json.dumps(
                        {"status": "passed", "evidence": {"sources": {"s": {"asset": asset}}}}
                    ),
                ),
            )
        elif origin == "research_run":
            connection.execute(
                "INSERT INTO research_runs VALUES ('r','c',?)",
                (json.dumps({"pool": {"sources": [value]}}),),
            )
        else:
            for sql in CHECKPOINT_SQL.values():
                connection.execute(sql.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS "))
            run = RunState(
                grounded_drafts={"section-1": {"sources": {"s": {"asset": asset}}}}
            ).model_dump(mode="json")
            serde = JsonPlusSerializer()
            if origin == "checkpoint":
                kind, encoded = serde.dumps_typed({"channel_values": {"run": run}})
                connection.execute(
                    "INSERT INTO checkpoints VALUES ('r','','c',NULL,?,?,NULL)", (kind, encoded)
                )
            else:
                kind, encoded = serde.dumps_typed(run)
                connection.execute(
                    "INSERT INTO writes VALUES ('r','','c','task','',0,'run',?,?)", (kind, encoded)
                )


@pytest.mark.parametrize(
    "origin",
    [
        "current",
        "history",
        "legacy",
        "candidate",
        "evidence",
        "research_run",
        "checkpoint",
        "pending_write",
    ],
)
@pytest.mark.parametrize("damage", ["missing", "tampered"])
def test_referenced_asset_damage_blocks_backup_and_migration(tmp_path, origin, damage):
    db = tmp_path / "agent.sqlite"
    migrate(db)
    raw = b"original asset"
    sha = hashlib.sha256(raw).hexdigest()
    assets = tmp_path / "research-assets"
    assets.mkdir()
    path = assets / (sha + ".pdf")
    path.write_bytes(raw)
    add_asset_reference(db, origin, {"filename": path.name, "sha256": sha})
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"changed asset")
    with sqlite3.connect(db) as connection:
        connection.execute("DROP TABLE IF EXISTS schema_migrations")
    before = db.read_bytes()
    with pytest.raises(MigrationError, match="来源文件"):
        backup(db, tmp_path / "backup")
    assert not (tmp_path / "backup").exists()
    with pytest.raises(MigrationError, match="来源文件"):
        migrate(db, tmp_path / "migration-backup")
    assert db.read_bytes() == before
    assert not (tmp_path / "migration-backup").exists()


@pytest.mark.parametrize("extension", ["pdf", "png", "html", "json"])
def test_shared_historical_asset_keeps_recorded_hash_on_restore(tmp_path, extension):
    db = tmp_path / "agent.sqlite"
    migrate(db)
    raw = b"shared history"
    sha = hashlib.sha256(raw).hexdigest()
    assets = tmp_path / "research-assets"
    assets.mkdir()
    path = assets / (sha + "." + extension)
    path.write_bytes(raw)
    asset = {"filename": path.name, "sha256": sha}
    add_asset_reference(db, "current", asset)
    add_asset_reference(db, "history", asset)
    backup(db, tmp_path / "backup")
    result = restore(tmp_path / "backup", tmp_path / "restored")
    assert result["files"] == 1
    assert (tmp_path / "restored/research-assets" / path.name).read_bytes() == raw


def complete_backup(tmp_path):
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    from assignment_agent.migrations.inspection import CHECKPOINT_SQL
    from assignment_agent.schemas import Material, RunState, SourceUnit

    source = tmp_path / "source"
    source.mkdir()
    db = source / "agent.sqlite"
    migrate(db)
    material = tmp_path / "external.md"
    material.write_bytes(b"brief")
    skill = tmp_path / "external-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_bytes(b"skill")
    assets = source / "research-assets"
    assets.mkdir()
    sha = hashlib.sha256(b"asset").hexdigest()
    asset = {"filename": sha + ".pdf", "sha256": sha}
    (assets / asset["filename"]).write_bytes(b"asset")
    state = RunState(
        materials=[
            Material(
                source_id="m",
                role="assignment",
                path=str(material),
                sha256=hashlib.sha256(b"brief").hexdigest(),
                units=[SourceUnit(location="line:1", text="brief")],
            )
        ],
        skill_root=str(skill),
        skill_manifest={"SKILL.md": hashlib.sha256(b"skill").hexdigest()},
        grounded_drafts={"section-1": {"sources": {"s": {"asset": asset}}}},
    )
    kind, raw = JsonPlusSerializer().dumps_typed(
        {"channel_values": {"run": state.model_dump(mode="json")}}
    )
    with sqlite3.connect(db) as connection:
        for sql in CHECKPOINT_SQL.values():
            connection.execute(sql)
        connection.execute("INSERT INTO checkpoints VALUES ('r','','c',NULL,?,?,NULL)", (kind, raw))
    destination = tmp_path / "backup"
    backup(db, destination)
    return destination


@pytest.mark.parametrize("omission", ["all", "material", "skill", "asset"])
def test_incomplete_manifest_is_rejected_before_restore(tmp_path, omission):
    directory = complete_backup(tmp_path)
    path = directory / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["files"] = [
        item
        for item in manifest["files"]
        if not (
            omission == "all"
            or (omission == "material" and item["original"].endswith("external.md"))
            or (omission == "skill" and item["original"].endswith("SKILL.md"))
            or (omission == "asset" and item["relative"].startswith("research-assets/"))
        )
    ]
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(MigrationError):
        verify(directory)
    with pytest.raises(MigrationError):
        restore(directory, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize(
    "damage",
    [
        "roots_null",
        "roots_missing",
        "roots_empty",
        "roots_object",
        "root_original_relative",
        "root_escape",
        "root_conflict",
        "files_null",
        "duplicate_file",
        "missing_original",
        "wrong_mapping",
        "reserved_target",
        "stored_escape",
        "boolean_format",
        "empty_database_name",
    ],
)
def test_invalid_manifest_is_rejected_before_creating_destination(tmp_path, damage):
    directory = complete_backup(tmp_path)
    path = directory / "manifest.json"
    m = json.loads(path.read_text(encoding="utf-8"))
    if damage == "roots_null":
        m["roots"] = None
    elif damage == "roots_missing":
        del m["roots"]
    elif damage == "roots_empty":
        m["roots"] = []
    elif damage == "roots_object":
        m["roots"] = {}
    elif damage == "root_original_relative":
        m["roots"][0]["original"] = "relative"
    elif damage == "root_escape":
        m["roots"][0]["relative"] = "../outside"
    elif damage == "root_conflict":
        m["roots"].append({**m["roots"][0], "relative": "other"})
    elif damage == "files_null":
        m["files"] = None
    elif damage == "duplicate_file":
        m["files"].append(m["files"][0].copy())
    elif damage == "missing_original":
        del m["files"][0]["original"]
    elif damage == "wrong_mapping":
        m["roots"][0]["relative"] = "wrong"
    elif damage == "reserved_target":
        m["files"][0]["relative"] = m["database_name"]
    elif damage == "stored_escape":
        m["files"][0]["stored"] = "../outside"
    elif damage == "boolean_format":
        m["format"] = True
    elif damage == "empty_database_name":
        m["database_name"] = ""
    path.write_text(json.dumps(m), encoding="utf-8")
    with pytest.raises(MigrationError):
        verify(directory)
    with pytest.raises(MigrationError):
        restore(directory, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize("legacy_layout", [False, True])
def test_complete_external_dependencies_restore_without_original_files(tmp_path, legacy_layout):
    import shutil

    from assignment_agent.migrations.backup import run_values
    from assignment_agent.state_versions import load_run

    directory = complete_backup(tmp_path)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if legacy_layout:
        for item in manifest["files"]:
            target = directory / "files" / item["relative"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(directory / item.pop("stored"), target)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / "external.md").unlink()
    (tmp_path / "external-skill/SKILL.md").unlink()
    result = restore(directory, tmp_path / "restored")
    with closing(sqlite3.connect(result["database"])) as connection:
        state = load_run(next(run_values(connection)), result["database"])
    assert state.materials[0].path != str(tmp_path / "external.md")
    from pathlib import Path

    assert Path(state.materials[0].path).read_bytes() == b"brief"
    assert (Path(state.skill_root) / "SKILL.md").read_bytes() == b"skill"
