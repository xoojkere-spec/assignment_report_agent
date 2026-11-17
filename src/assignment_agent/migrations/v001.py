"""将已知的无版本业务结构纳入首个版本，保留旧混合库的原有语义。"""

import json

from assignment_agent.migrations.catalog import BUSINESS_SQL, SEARCH_INDEX_SQL
from assignment_agent.migrations.inspection import MigrationError, validate_structure
from assignment_agent.migrations.legacy_v0 import archive


def apply(connection):
    for sql in BUSINESS_SQL.values():
        connection.execute(sql)
    connection.execute(SEARCH_INDEX_SQL)
    if connection.execute("SELECT 1 FROM sqlite_master WHERE name='research_entries'").fetchone():
        conflicts = connection.execute(
            "SELECT old.data,new.data FROM research_entries old JOIN research_candidates new "
            "USING (run_id,context_hash,kind,id)"
        ).fetchall()
        if any(json.loads(old) != json.loads(new) for old, new in conflicts):
            raise MigrationError("旧混合库与候选库有同编号冲突，不能自动覆盖")
        connection.execute(
            "INSERT OR IGNORE INTO research_candidates SELECT * FROM research_entries"
        )
        for row in connection.execute(
            "SELECT run_id,context_hash,artifact_id,version,recorded_at,data "
            "FROM writing_evidence ORDER BY id"
        ).fetchall():
            value = json.loads(row[5])
            if value.get("status") == "passed":
                archive(connection, *row[:5], value, legacy=True)
        connection.execute("DROP TABLE research_entries")
    validate_structure(connection)
