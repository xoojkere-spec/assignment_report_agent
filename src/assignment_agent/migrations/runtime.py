"""运行入口在维护锁内检查版本，旧库只能通过显式维护命令升级。"""

import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from assignment_agent.migrations.backup import run_values
from assignment_agent.migrations.inspection import MigrationError, inspect, readonly
from assignment_agent.migrations.locking import database_lock
from assignment_agent.migrations.maintenance import migrate
from assignment_agent.state_versions import load_run

_active = ContextVar("assignment_database_access", default=frozenset())


@contextmanager
def access(database):
    path = Path(database).resolve()
    key = str(path)
    # LangGraph 将上下文传到节点线程；外层仍持有维护锁，节点不能再次竞争同一锁。
    if key in _active.get():
        yield
        return
    # 空库初始化需要独占锁；不能在共享业务锁中升级锁级别。
    if inspect(path)["status"] in {"missing", "empty"}:
        migrate(path)
    with database_lock(path, shared=True):
        result = inspect(path)
        if result["status"] != "current":
            raise MigrationError(
                result.get("error") or "旧数据库需要停服后执行 db migrate --offline"
            )
        with readonly(path) as connection:
            for value in run_values(connection):
                load_run(value, path)
        token = _active.set(_active.get() | {key})
        try:
            yield
        finally:
            _active.reset(token)


@contextmanager
def business_connection(database, *, rows=False):
    with access(database):
        connection = sqlite3.connect(str(database), timeout=10)
        if rows:
            connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()
