"""从保留的来源、证据和任务状态中读取文件引用，不重新认领已损坏文件的哈希。"""

import json
import re

from assignment_agent.migrations.inspection import MigrationError

ASSET_TABLES = (
    "research_library_current",
    "research_library_versions",
    "research_candidates",
    "research_entries",
    "writing_evidence",
    "research_runs",
)


def asset_references(value):
    if isinstance(value, dict):
        if "filename" in value:
            name, sha = value["filename"], value.get("sha256")
            if (
                not isinstance(name, str)
                or not isinstance(sha, str)
                or not re.fullmatch(r"[0-9a-f]{64}", sha)
                or name not in {sha + "." + suffix for suffix in ("pdf", "png", "html", "json")}
            ):
                raise MigrationError("来源文件引用或哈希格式无效")
            yield name, sha
        for child in value.values():
            yield from asset_references(child)
    elif isinstance(value, list):
        for child in value:
            yield from asset_references(child)


def required_assets(connection, states):
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    result = {}
    for state in states:
        result.update(asset_references(state))
    for table in ASSET_TABLES:
        if table not in tables:
            continue
        for row in connection.execute(f"SELECT data FROM {table}"):
            try:
                value = json.loads(row[0])
            except (TypeError, ValueError) as exc:
                raise MigrationError("来源记录 JSON 无效，不能核对来源文件") from exc
            result.update(asset_references(value))
    return result
