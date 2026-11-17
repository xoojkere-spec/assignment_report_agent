"""独立保存答疑记录，不推进或改写作业确认检查点。"""

from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from assignment_agent.migrations.runtime import business_connection


def available(current: dict) -> bool:
    return bool(
        current["interrupt_id"]
        and current["state"]["status"] == "awaiting_user"
        and (
            (current["interrupt_payload"] or {}).get("kind") == "requirements"
            or (current["state"]["pending"] or {}).get("artifact_id") == "outline"
        )
    )


@contextmanager
def connect(database):
    with business_connection(database, rows=True) as connection:
        yield connection


def history(database, run_id):
    with connect(database) as connection:
        rows = connection.execute(
            "SELECT id, interrupt_id, question, answer, status, error, created_at, finished_at "
            "FROM advisor_exchanges WHERE run_id=? ORDER BY id",
            (run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def begin(database, run_id, interrupt_id, question):
    with connect(database) as connection:
        return connection.execute(
            "INSERT INTO advisor_exchanges(run_id, interrupt_id, question, status, created_at) "
            "VALUES (?, ?, ?, 'pending', ?)",
            (run_id, interrupt_id, question, datetime.now(UTC).isoformat()),
        ).lastrowid


def finish(database, exchange_id, *, answer=None, error=None):
    with connect(database) as connection:
        connection.execute(
            "UPDATE advisor_exchanges SET answer=?, error=?, status=?, finished_at=? WHERE id=?",
            (
                answer,
                error,
                "failed" if error else "completed",
                datetime.now(UTC).isoformat(),
                exchange_id,
            ),
        )


def interrupt_pending(database):
    # 后台队列只在内存中；重启后将遗留提问标为中断，避免无限显示正在回答。
    if not Path(database).exists():
        return
    with connect(database) as connection:
        connection.execute(
            "UPDATE advisor_exchanges SET status='failed', error=?, finished_at=? "
            "WHERE status='pending'",
            ("服务已重启，回答已中断，请重新提问", datetime.now(UTC).isoformat()),
        )
