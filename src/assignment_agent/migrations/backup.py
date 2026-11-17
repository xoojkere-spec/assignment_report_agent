"""在维护窗口备份数据库与文件依赖，恢复始终生成独立副本。"""

import hashlib
import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from assignment_agent.migrations.assets import required_assets
from assignment_agent.migrations.inspection import MigrationError, plan, readonly
from assignment_agent.migrations.locking import database_lock
from assignment_agent.migrations.manifest import validate_layout, validate_required_files
from assignment_agent.state_versions import path_mappings, relocate_state


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_child(root, relative):
    path = (root / relative).resolve()
    if (
        Path(relative).is_absolute()
        or not path.is_relative_to(root.resolve())
        or path == root.resolve()
    ):
        raise MigrationError("备份清单包含越界路径")
    return path


def run_values(connection):
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    serde = JsonPlusSerializer()
    if "checkpoints" in tables:
        for kind, raw in connection.execute("SELECT type,checkpoint FROM checkpoints"):
            checkpoint = serde.loads_typed((kind, raw))
            if not isinstance(checkpoint, dict):
                raise MigrationError("检查点任务状态格式无效")
            channels = checkpoint.get("channel_values", {})
            if not isinstance(channels, dict):
                raise MigrationError("检查点任务状态通道无效")
            # 初始检查点允许尚无 run；已存在的损坏值不能被当成没有任务。
            if "run" not in channels:
                continue
            value = channels["run"]
            if not isinstance(value, dict):
                raise MigrationError("检查点任务状态必须是字典")
            yield value
    if "writes" in tables:
        for kind, raw in connection.execute("SELECT type,value FROM writes WHERE channel='run'"):
            value = serde.loads_typed((kind, raw))
            if not isinstance(value, dict):
                raise MigrationError("待提交任务状态必须是字典")
            yield value


def backup(database, destination):
    with database_lock(database):
        return _backup(database, destination)


def _backup(database, destination):
    database, destination = Path(database).resolve(), Path(destination).resolve()
    plan(database)
    if not database.is_file():
        raise MigrationError("数据库不存在，不能生成备份")
    roots = {database.parent}
    files = set()
    for folder in ("research-assets", "skill-snapshots"):
        root = database.parent / folder
        if root.exists():
            files.update(path for path in root.rglob("*") if path.is_file())
    with readonly(database) as source:
        values = [relocate_state(value, database) for value in run_values(source)]
        references = required_assets(source, values)
    expected_assets = {}
    for filename, expected in references.items():
        path = safe_child(database.parent / "research-assets", filename)
        if not path.is_file() or digest(path) != expected:
            raise MigrationError("来源文件缺失或哈希不符，不能生成完整备份")
        expected_assets[path] = expected
        files.add(path)
    for value in values:
        for item in value.get("materials", []):
            path = Path(item["path"]).resolve()
            if not path.is_file() or digest(path) != item["sha256"]:
                raise MigrationError("材料原文件缺失或哈希不符，不能生成完整备份")
            files.add(path)
        if value.get("skill_root"):
            root = Path(value["skill_root"]).resolve()
            if not root.is_dir():
                raise MigrationError("Skill 快照目录缺失")
            roots.add(root)
            for name, expected in value.get("skill_manifest", {}).items():
                path = safe_child(root, name)
                if not path.is_file() or digest(path) != expected:
                    raise MigrationError("Skill 快照缺失或哈希不符")
                files.add(path)
    for root in roots:
        if (
            destination == root
            or destination.is_relative_to(root / "research-assets")
            or destination.is_relative_to(root / "skill-snapshots")
        ):
            raise MigrationError("备份目标不能覆盖数据库或依赖目录")
    if destination.exists():
        raise MigrationError("备份目标已存在")
    destination.mkdir(parents=True)
    db_copy = destination / "database.sqlite"
    with readonly(database) as source, closing(sqlite3.connect(db_copy)) as target:
        source.backup(target)
        if target.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise MigrationError("数据库备份完整性检查失败")
    root_map = [{"original": str(database.parent), "relative": "."}]
    for index, root in enumerate(sorted(roots - {database.parent})):
        if not root.is_relative_to(database.parent):
            root_map.append({"original": str(root), "relative": f"external/skill-{index}"})
    entries = []
    for path in sorted(files):
        if path.is_symlink() or not path.is_file():
            raise MigrationError("文件依赖缺失或为符号链接")
        mappings = [m for m in root_map if path.is_relative_to(Path(m["original"]))]
        if mappings:
            mapping = max(mappings, key=lambda m: len(m["original"]))
            relative = (
                Path(mapping["relative"]) / path.relative_to(mapping["original"])
            ).as_posix()
        else:
            relative = (
                "external/material-"
                + hashlib.sha256(str(path).encode()).hexdigest()
                + "/"
                + path.name
            )
            root_map.append({"original": str(path), "relative": relative})
        before = digest(path)
        if path in expected_assets and before != expected_assets[path]:
            raise MigrationError("备份期间来源文件哈希发生变化")
        # 平铺内容寻址对象，避免备份根目录叠加 Skill 深层路径触发 Windows 长路径限制。
        stored = "objects/" + before
        target = safe_child(destination, stored)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        if digest(path) != before or digest(target) != before:
            raise MigrationError("备份期间文件发生变化，备份不完整")
        entries.append(
            {"original": str(path), "relative": relative, "sha256": before, "stored": stored}
        )
    # 再次备份恢复副本时，原始历史检查点仍可能引用第一代目录。
    for previous in path_mappings(database):
        current = Path(previous["restored"]).resolve()
        candidates = [m for m in root_map if current.is_relative_to(Path(m["original"]))]
        if candidates:
            match = max(candidates, key=lambda m: len(m["original"]))
            relative = (Path(match["relative"]) / current.relative_to(match["original"])).as_posix()
            root_map.append({"original": previous["original"], "relative": relative})
    manifest = {
        "format": 1,
        "database_name": database.name,
        "database_sha256": digest(db_copy),
        "roots": root_map,
        "files": entries,
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    verify(destination)
    return {"backup": str(destination), "files": len(entries)}


def verify(directory):
    directory = Path(directory).resolve()
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        roots, files = validate_layout(manifest)
        if (
            manifest["format"] != 1
            or digest(directory / "database.sqlite") != manifest["database_sha256"]
        ):
            raise MigrationError("备份数据库哈希不符")
        if Path(manifest["database_name"]).name != manifest["database_name"]:
            raise MigrationError("备份数据库名称无效")
        with readonly(directory / "database.sqlite") as connection:
            if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise MigrationError("备份数据库损坏")
            states = list(run_values(connection))
            validate_required_files(connection, states, roots, files)
        for item in manifest["files"]:
            safe_child(directory / "files", item["relative"])
            stored = item.get("stored", "files/" + item["relative"])
            if digest(safe_child(directory, stored)) != item["sha256"]:
                raise MigrationError("备份文件哈希不符")
        return manifest
    except (OSError, KeyError, TypeError, json.JSONDecodeError, sqlite3.Error) as exc:
        raise MigrationError("备份不完整或清单无效") from exc


def restore(directory, destination):
    directory, destination = Path(directory).resolve(), Path(destination).resolve()
    manifest = verify(directory)
    if destination.exists():
        raise MigrationError("恢复目标必须是不存在的新目录")
    destination.mkdir(parents=True)
    database = destination / manifest["database_name"]
    shutil.copyfile(directory / "database.sqlite", database)
    for item in manifest["files"]:
        target = safe_child(destination, item["relative"])
        target.parent.mkdir(parents=True, exist_ok=True)
        stored = item.get("stored", "files/" + item["relative"])
        shutil.copyfile(safe_child(directory, stored), target)
        if digest(target) != item["sha256"]:
            raise MigrationError("恢复文件哈希不符")
    # 历史检查点保持原字节；F02-6 在读取状态时应用恢复目录映射。
    mapping = [
        {
            "original": r["original"],
            "restored": str(
                destination if r["relative"] == "." else safe_child(destination, r["relative"])
            ),
        }
        for r in manifest["roots"]
    ]
    database.with_suffix(database.suffix + ".paths.json").write_text(
        json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if digest(database) != manifest["database_sha256"]:
        raise MigrationError("恢复数据库哈希不符")
    return {"database": str(database), "files": len(manifest["files"])}
