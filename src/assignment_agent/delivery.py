"""将全文确认绑定到正文版本，保持已有写作检查点兼容。"""

import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, datetime

from assignment_agent.migrations.runtime import business_connection
from assignment_agent.schemas import RunState


def content_hash(run: RunState) -> str:
    spec = run.spec.model_dump() if run.spec else None
    if spec:
        # 新增的默认目标不使旧正文的全文确认失效；更高的任务要求仍参与版本校验。
        for name, default in (("min_sources", 3), ("min_web_images", 2)):
            if spec[name] == default:
                spec.pop(name)
    content = {
        "spec": spec,
        "drafts": [item.model_dump() for item in run.drafts],
    }
    if run.automatic_research:
        content["grounded_drafts"] = run.grounded_drafts
    return hashlib.sha256(
        json.dumps(content, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def ready_for_delivery(run: RunState) -> bool:
    if (
        run.status != "completed"
        or not run.spec
        or not run.spec.sections
        or run.pending
        or run.current_index != len(run.spec.sections)
        or len(run.drafts) != len(run.spec.sections)
    ):
        return False
    approved = {
        (item.artifact_id, item.version) for item in run.approvals if item.action == "approve"
    }
    if run.outline is None or (run.outline.artifact_id, run.outline.version) not in approved:
        return False
    return all(
        item.artifact_id == f"section-{index + 1}" and (item.artifact_id, item.version) in approved
        for index, item in enumerate(run.drafts)
    )


@contextmanager
def _connect(database):
    with business_connection(database) as connection:
        yield connection


def delivery_status(database, run_id, run: RunState) -> dict:
    ready = ready_for_delivery(run)
    digest = content_hash(run) if ready else None
    with _connect(database) as connection:
        row = connection.execute(
            "SELECT confirmed_at FROM completion_records WHERE run_id=? AND content_hash=?",
            (run_id, digest),
        ).fetchone()
    connection.close()
    return {
        "ready": ready,
        "confirmed": bool(row) and ready,
        "content_hash": digest,
        "confirmed_at": row[0] if row else None,
    }


def confirm_delivery(database, run_id, run: RunState, expected_hash: str):
    if not ready_for_delivery(run):
        raise ValueError("尚有未确认章节，请先完成逐部分写作与确认")
    digest = content_hash(run)
    if expected_hash != digest:
        raise ValueError("正文版本已变化，请刷新全文后重新确认完成")
    with _connect(database) as connection:
        # 相同版本重复确认不新增记录，也不改变原确认时间。
        connection.execute(
            "INSERT OR IGNORE INTO completion_records VALUES (?, ?, ?)",
            (run_id, digest, datetime.now(UTC).isoformat()),
        )
    connection.close()
