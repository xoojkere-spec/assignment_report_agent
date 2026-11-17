"""显式维护入口：获取锁、先备份、再迁移，独立保存成功和失败报告。"""

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from assignment_agent.migrations.backup import backup
from assignment_agent.migrations.inspection import inspect, plan
from assignment_agent.migrations.locking import database_lock
from assignment_agent.migrations.registry import migrations
from assignment_agent.migrations.runner import apply_steps


def migrate(database, backup_directory=None):
    database = Path(database).resolve()
    with database_lock(database):
        proposed = plan(database)
        if not proposed["pending"]:
            return {"status": "current", "applied": []}
        token = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:8]
        reports = database.parent / "migration-reports"
        reports.mkdir(exist_ok=True)
        record = {"status": "started", "database": str(database), "applied": []}
        try:
            if proposed["requires_backup"]:
                target = backup_directory or database.parent / "backups" / token
                record["backup"] = backup(database, target)["backup"]
            with closing(sqlite3.connect(database, timeout=10, isolation_level=None)) as connection:
                record["applied"] = apply_steps(connection, migrations())
            record["status"] = "completed"
            record["version"] = inspect(database)["version"]
            return record
        except BaseException as exc:
            record.update(status="failed", error=str(exc))
            raise
        finally:
            (reports / (token + ".json")).write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
            )
