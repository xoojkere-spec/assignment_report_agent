"""记录允许公开的运行指标，不保存请求正文、模型原文或密钥。"""

import json
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path

from assignment_agent.migrations.runtime import business_connection

_trace: ContextVar[tuple[str, str] | None] = ContextVar("assignment_trace", default=None)


@contextmanager
def _connect(database):
    with business_connection(database) as connection:
        yield connection


@contextmanager
def trace_run(database, run_id):
    token = _trace.set((str(database), run_id))
    try:
        yield
    finally:
        _trace.reset(token)


def emit(event: str, **data):
    target = _trace.get()
    if target is None:
        return
    database, run_id = target
    with _connect(database) as connection:
        connection.execute(
            "INSERT INTO run_diagnostics(run_id, recorded_at, event, data) VALUES (?, ?, ?, ?)",
            (run_id, datetime.now(UTC).isoformat(), event, json.dumps(data, ensure_ascii=False)),
        )
    connection.close()


def read_diagnostics(database, run_id):
    if not Path(database).exists():
        return {"events": [], "usage": {}}
    with _connect(database) as connection:
        rows = connection.execute(
            "SELECT recorded_at, event, data FROM run_diagnostics WHERE run_id=? ORDER BY id",
            (run_id,),
        ).fetchall()
    connection.close()
    events = [
        {"recorded_at": timestamp, "event": event, **json.loads(data)}
        for timestamp, event, data in rows
    ]
    usage = {}
    calls = 0
    reported = 0
    for item in events:
        if item["event"] == "api.response":
            calls += 1
            if item.get("usage"):
                reported += 1
            for key, value in item.get("usage", {}).items():
                usage[key] = usage.get(key, 0) + value
    return {
        "events": events,
        "usage": usage,
        "response_count": calls,
        "usage_reported_count": reported,
    }
