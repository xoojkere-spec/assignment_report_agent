"""只读识别已知结构；未知结构不能被默认为当前版本。"""

import re
import sqlite3
from contextlib import closing
from pathlib import Path

from assignment_agent.migrations.catalog import BUSINESS_SQL, SEARCH_INDEX_SQL

CURRENT_VERSION = 1
LEDGER_SQL = (
    "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
    "checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
)
CHECKPOINT_SQL = {
    "checkpoints": "CREATE TABLE checkpoints (thread_id TEXT NOT NULL, checkpoint_ns TEXT "
    "NOT NULL DEFAULT '', checkpoint_id TEXT NOT NULL, parent_checkpoint_id TEXT, type TEXT, "
    "checkpoint BLOB, metadata BLOB, PRIMARY KEY(thread_id,checkpoint_ns,checkpoint_id))",
    "writes": "CREATE TABLE writes (thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL "
    "DEFAULT '', checkpoint_id TEXT NOT NULL, task_id TEXT NOT NULL, task_path TEXT NOT NULL "
    "DEFAULT '', idx INTEGER NOT NULL, channel TEXT NOT NULL, type TEXT, value BLOB, "
    "PRIMARY KEY(thread_id,checkpoint_ns,checkpoint_id,task_id,idx))",
}


class MigrationError(ValueError):
    """需要用户处理后才能继续的数据维护错误。"""


def readonly(database):
    return closing(sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True))


def columns(connection, name):
    return [tuple(row[1:]) for row in connection.execute(f'PRAGMA table_info("{name}")')]


def normalize(sql):
    return re.sub(r"\s+|IFNOTEXISTS|[\"`]", "", sql.upper().replace("IF NOT EXISTS", ""))


def validate_structure(connection):
    objects = connection.execute(
        # GLOB 将下划线视为普通字符，不能误把 sqliteX 等用户对象当成内部对象。
        "SELECT type,name,tbl_name FROM sqlite_master WHERE lower(name) NOT GLOB 'sqlite_*'"
    ).fetchall()
    tables = {name for kind, name, _ in objects if kind == "table"}
    expected = {**BUSINESS_SQL, **CHECKPOINT_SQL, "schema_migrations": LEDGER_SQL}
    unexpected = tables - expected.keys() - {"research_entries"}
    if unexpected:
        raise MigrationError("未知数据表：" + ", ".join(sorted(unexpected)))
    if bool("checkpoints" in tables) != bool("writes" in tables):
        raise MigrationError("检查点表不完整")
    with closing(sqlite3.connect(":memory:")) as reference:
        for sql in expected.values():
            reference.execute(sql)
        reference.execute(SEARCH_INDEX_SQL)
        legacy_sql = BUSINESS_SQL["research_candidates"].replace(
            "research_candidates", "research_entries"
        )
        reference.execute("CREATE TABLE legacy_copy AS SELECT * FROM research_candidates WHERE 0")
        copied_sql = (
            reference.execute("SELECT sql FROM sqlite_master WHERE name='legacy_copy'")
            .fetchone()[0]
            .replace("legacy_copy", "research_entries")
        )
        old_writes = CHECKPOINT_SQL["writes"].replace("task_path TEXT NOT NULL DEFAULT '', ", "")
        reference.execute(old_writes.replace("CREATE TABLE writes", "CREATE TABLE writes_old"))
        reference.execute("ALTER TABLE writes_old ADD COLUMN task_path TEXT NOT NULL DEFAULT ''")
        altered_sql = (
            reference.execute("SELECT sql FROM sqlite_master WHERE name='writes_old'")
            .fetchone()[0]
            .replace("writes_old", "writes")
        )
        for name in sorted(tables):
            sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()[0]
            if name not in {"research_entries", "writes"} and normalize(sql) != normalize(
                expected[name]
            ):
                raise MigrationError(f"数据表定义不兼容：{name}")
            if name == "research_entries":
                if normalize(sql) not in {normalize(legacy_sql), normalize(copied_sql)}:
                    raise MigrationError("旧混合来源表定义未知")
                # 旧回归通过 CREATE TABLE AS 构造混合表，无主键和 NOT NULL 约束。
                actual = [row[:2] for row in columns(connection, name)]
                known = [row[:2] for row in columns(reference, "research_candidates")]
                if actual != known:
                    raise MigrationError("旧混合来源表结构未知")
                continue
            actual, known = columns(connection, name), columns(reference, name)
            if name == "writes":
                if normalize(sql) not in {
                    normalize(CHECKPOINT_SQL["writes"]),
                    normalize(old_writes),
                    normalize(altered_sql),
                }:
                    raise MigrationError("检查点写入表定义未知")
                known = [
                    row
                    for row in known
                    if row[0] != "task_path" or any(item[0] == "task_path" for item in actual)
                ]
                actual, known = sorted(actual), sorted(known)
            if actual != known:
                raise MigrationError(f"数据表结构不兼容：{name}")
        for kind, name, table in objects:
            if kind == "table":
                continue
            if kind != "index" or name != "research_search_attempts_run_chapter":
                raise MigrationError(f"未知数据库对象：{name}")
            actual = connection.execute(f'PRAGMA index_info("{name}")').fetchall()
            known = reference.execute(f'PRAGMA index_info("{name}")').fetchall()
            indexes = connection.execute(f'PRAGMA index_list("{table}")').fetchall()
            if actual != known or not any(
                row[1] == name and row[2] == 0 and row[4] == 0 for row in indexes
            ):
                raise MigrationError(f"索引结构不兼容：{name}")
    return tables


def inspect(database):
    result = {"database": str(Path(database).resolve()), "target_version": CURRENT_VERSION}
    if not Path(database).exists():
        return {**result, "status": "missing", "version": 0, "pending": [1]}
    try:
        with readonly(database) as connection:
            tables = validate_structure(connection)
            history = (
                connection.execute(
                    "SELECT version,name,checksum,applied_at FROM schema_migrations "
                    "ORDER BY version"
                ).fetchall()
                if "schema_migrations" in tables
                else []
            )
            version = history[-1][0] if history else 0
            if any(type(row[0]) is not int or row[0] <= 0 for row in history):
                raise MigrationError("迁移版本记录无效")
            if version > CURRENT_VERSION:
                return {
                    **result,
                    "status": "future",
                    "version": version,
                    "pending": [],
                    "error": "数据库来自更新的程序版本",
                }
            if [row[0] for row in history] != list(range(1, version + 1)):
                raise MigrationError("迁移历史不连续")
            if history:
                from assignment_agent.migrations.registry import migrations
                from assignment_agent.migrations.runner import validate_history

                validate_history(connection, migrations())
            if version and (set(BUSINESS_SQL) - tables or "research_entries" in tables):
                raise MigrationError("已版本化数据库缺少必要结构或残留旧表")
            # 无版本旧库允许按需建表；声明完成迁移的当前库必须具备目标版本索引。
            if (
                version
                and not connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='index' AND name=? AND tbl_name=?",
                    ("research_search_attempts_run_chapter", "research_search_attempts"),
                ).fetchone()
            ):
                raise MigrationError("已版本化数据库缺少必要索引")
            status = "current" if version else "unversioned" if tables else "empty"
            if "checkpoints" in tables:
                from assignment_agent.migrations.backup import run_values
                from assignment_agent.state_versions import load_run

                try:
                    for value in run_values(connection):
                        load_run(value, database)
                except Exception:
                    raise MigrationError("检查点任务状态或恢复路径不兼容，未执行写入") from None
            return {
                **result,
                "status": status,
                "version": version,
                "pending": list(range(version + 1, CURRENT_VERSION + 1)),
                "tables": sorted(tables),
            }
    except (sqlite3.Error, MigrationError) as exc:
        return {**result, "status": "blocked", "version": None, "pending": [], "error": str(exc)}


def plan(database):
    result = inspect(database)
    if result["status"] in {"blocked", "future"}:
        raise MigrationError(result["error"])
    return {
        **result,
        "requires_backup": result["status"] not in {"missing", "empty"} and bool(result["pending"]),
    }
