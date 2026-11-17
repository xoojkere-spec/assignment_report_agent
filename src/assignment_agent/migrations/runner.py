"""每步迁移与成功记录原子提交；失败步骤回滚，已完成步骤不重做。"""

import hashlib
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from assignment_agent.migrations.inspection import LEDGER_SQL, MigrationError


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    source: str
    apply: Callable

    @property
    def checksum(self):
        return hashlib.sha256(self.source.encode("utf-8")).hexdigest()


def history(connection):
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name='schema_migrations'"
    ).fetchone():
        return []
    return connection.execute(
        "SELECT version,name,checksum FROM schema_migrations ORDER BY version"
    ).fetchall()


def validate_history(connection, migrations):
    if [step.version for step in migrations] != list(range(1, len(migrations) + 1)):
        raise MigrationError("程序迁移定义不连续")
    completed = history(connection)
    if [row[0] for row in completed] != list(range(1, len(completed) + 1)):
        raise MigrationError("迁移历史不连续")
    if len(completed) > len(migrations):
        raise MigrationError("数据库来自更新的程序版本")
    for row, step in zip(completed, migrations, strict=False):
        if tuple(row) != (step.version, step.name, step.checksum):
            raise MigrationError("已执行迁移的名称或脚本校验值发生变化")
    return len(completed)


def apply_steps(connection, migrations):
    if connection.in_transaction:
        raise MigrationError("迁移不能在已有事务中执行")
    completed = validate_history(connection, migrations)
    applied = []
    for step in migrations[completed:]:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(LEDGER_SQL.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS "))

            def authorize(action, arg1, arg2, database, source):
                # 防止脚本内部 commit 或 executescript 提前提交，破坏失败回滚边界。
                return (
                    sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_TRANSACTION
                    else sqlite3.SQLITE_OK
                )

            connection.set_authorizer(authorize)
            try:
                step.apply(connection)
            finally:
                connection.set_authorizer(None)
            if not connection.in_transaction:
                raise MigrationError("迁移步骤擅自提交了事务")
            connection.execute(
                "INSERT INTO schema_migrations VALUES (?,?,?,?)",
                (
                    step.version,
                    step.name,
                    step.checksum,
                    datetime.now(UTC).isoformat(),
                ),
            )
            connection.commit()
            applied.append(step.version)
        except BaseException:
            connection.rollback()
            raise
    return applied
