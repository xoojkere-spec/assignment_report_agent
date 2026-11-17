"""正式来源的不可变版本与临时材料回收；正式记录只能由已通过的正文提交。"""

import hashlib
import json
import re
from pathlib import Path

from assignment_agent.research_types import ResearchError


def asset_names(value):
    if isinstance(value, dict):
        name = value.get("filename", "")
        if isinstance(name, str) and re.fullmatch(r"[0-9a-f]{64}\.(?:pdf|png|html|json)", name):
            yield name
        for child in value.values():
            yield from asset_names(child)
    elif isinstance(value, list):
        for child in value:
            yield from asset_names(child)


def working_entries(connection, run_id, digest, kind):
    records = {}
    for row in connection.execute(
        "SELECT id,data FROM research_library_versions WHERE run_id=? AND context_hash=? "
        "AND kind=? ORDER BY rowid",
        (run_id, digest, kind),
    ):
        records[row[0]] = json.loads(row[1])
    for row in connection.execute(
        "SELECT id,data FROM research_library_current WHERE run_id=? AND context_hash=? "
        "AND kind=? ORDER BY rowid",
        (run_id, digest, kind),
    ):
        records[row[0]] = json.loads(row[1])
    for row in connection.execute(
        "SELECT id,data FROM research_candidates WHERE run_id=? AND context_hash=? AND kind=? "
        "ORDER BY rowid",
        (run_id, digest, kind),
    ):
        records[row[0]] = json.loads(row[1])
    return list(records.values())


def archive(
    connection, run_id, digest, artifact_id, version, stamp, data, *, legacy=False, asset_valid=None
):
    bundle = data.get("evidence", {})
    # 旧的测试记录、单独的来源审阅和 References 列表都不能冒充正文引用通过记录。
    if not bundle.get("draft") or not bundle.get("verdict"):
        return
    draft, verdict = bundle["draft"], bundle["verdict"]
    cited = {item["source_id"] for item in draft.get("citations", [])}
    figures = {item["figure_id"] for item in draft.get("figures", [])}
    valid = (
        cited == set(bundle.get("citation_ids", []))
        and cited == set(re.findall(r"\[\[cite:(src-[0-9a-f]{24})\]\]", draft.get("content", "")))
        and figures == set(bundle.get("figure_ids", []))
        and figures
        == set(re.findall(r"\[\[figure:(fig-[0-9a-f]{24})\]\]", draft.get("content", "")))
        and not verdict.get("issues")
        and len(verdict.get("citations", [])) == len(draft.get("citations", []))
        and {item.get("index") for item in verdict.get("citations", [])}
        == set(range(len(draft.get("citations", []))))
        and all(item.get("supported") for item in verdict.get("citations", []))
        and len(verdict.get("figures", [])) == len(figures)
        and {item.get("figure_id") for item in verdict.get("figures", [])} == figures
        and all(
            item.get("relevant") and item.get("caption_accurate")
            for item in verdict.get("figures", [])
        )
    )
    if not valid:
        if legacy:
            return
        raise ResearchError("未通过正文引用或插图核验，不能提交正式来源库")
    source_records = {s["id"]: s for s in working_entries(connection, run_id, digest, "source")}
    figure_records = {f["id"]: f for f in working_entries(connection, run_id, digest, "figure")}
    if not figures.issubset(figure_records):
        if legacy:
            figures &= figure_records.keys()
        else:
            raise ResearchError("实际插图的取证记录缺失，不能提交正式来源库")
    used = cited | {figure_records[key]["source_id"] for key in figures}
    pool = {s["id"]: s for s in bundle.get("pool", {}).get("sources", [])}
    for kind, ids, records in (
        ("source", used, source_records),
        ("figure", figures, figure_records),
    ):
        for key in sorted(ids):
            if key not in records:
                if legacy:
                    continue
                raise ResearchError("实际引用的来源记录缺失，不能提交正式来源库")
            item = dict(records[key])
            if asset_valid and (
                (
                    kind == "source"
                    and not (
                        asset_valid(item.get("pdf") or item.get("page_asset"))
                        and asset_valid(item.get("text_asset"))
                    )
                )
                or (kind == "figure" and not asset_valid(item))
                or (
                    kind == "figure"
                    and item.get("method") in {"licensed_web_image", "web_page_image"}
                    and not asset_valid(item.get("provenance_asset"))
                )
            ):
                raise ResearchError("正式提交时来源或插图缓存已缺失，不能入库")
            if kind == "source":
                snapshot = bundle.get("sources", {}).get(key)
                if not snapshot:
                    if legacy:
                        continue
                    raise ResearchError("来源快照缺失，不能提交正式来源库")
                if not legacy and (
                    not item.get("verified")
                    or (item.get("pdf") or item.get("page_asset")) != snapshot["asset"]
                    or item.get("text_asset") != snapshot["text_asset"]
                ):
                    raise ResearchError("来源快照与核验稿不一致，不能提交正式来源库")
                if legacy:
                    # 按旧稿原始快照恢复证据，不用后来下载的文件替换历史版本。
                    public = pool.get(key, {})
                    item.update(
                        {k: public[k] for k in ("title", "authors", "year", "doi") if k in public}
                    )
                    asset = snapshot.get("asset") or {}
                    is_pdf = asset.get("filename", "").endswith(".pdf")
                    item.update(
                        pdf=asset if is_pdf else {},
                        page_asset={} if is_pdf else asset,
                        text_asset=snapshot.get("text_asset"),
                        verified=True,
                    )
                    item["pdf_url" if is_pdf else "url"] = snapshot["url"]
                    item["doi"] = snapshot.get("doi")
            item["retired"] = False
            encoded = json.dumps(item, ensure_ascii=False, sort_keys=True)
            identity = (run_id, digest, artifact_id, version, bundle["content_hash"], kind, key)
            old = connection.execute(
                "SELECT data FROM research_library_versions WHERE run_id=? AND context_hash=? "
                "AND artifact_id=? AND version=? AND content_hash=? AND kind=? AND id=?",
                identity,
            ).fetchone()
            if old:
                previous = json.loads(old[0])
                if not legacy and (
                    set(asset_names(previous)) != set(asset_names(item))
                    or any(
                        previous.get(k) != item.get(k)
                        for k in ("url", "pdf_url", "doi", "source_id")
                    )
                ):
                    raise ResearchError("已有正文来源版本不能被覆盖")
                continue
            connection.execute(
                "INSERT OR IGNORE INTO research_library_versions VALUES (?,?,?,?,?,?,?,?,?)",
                (*identity, stamp, encoded),
            )
            connection.execute(
                "INSERT OR REPLACE INTO research_library_current VALUES (?,?,?,?,?)",
                (run_id, digest, kind, key, encoded),
            )


def collect_garbage(connection, assets):
    # 有下载进行时只排队；文件可能尚未登记，不能误删其他任务刚写出的共享文件。
    if any(
        json.loads(row[0]).get("status") == "running"
        for row in connection.execute("SELECT data FROM research_runs")
    ):
        return 0
    protected = set()
    for table in (
        "research_candidates",
        "research_library_versions",
        "research_library_current",
        "writing_evidence",
    ):
        for row in connection.execute(f"SELECT data FROM {table}"):
            protected.update(asset_names(json.loads(row[0])))
    deleted = 0
    root = Path(assets).resolve()
    if root.is_dir():
        for path in root.iterdir():
            if (
                re.fullmatch(r"[0-9a-f]{64}\.(?:pdf|png|html|json)", path.name)
                and path.name not in protected
            ):
                connection.execute(
                    "INSERT OR IGNORE INTO research_garbage VALUES (?)", (path.name,)
                )
    for row in connection.execute("SELECT filename FROM research_garbage").fetchall():
        name = row[0]
        if name in protected:
            connection.execute("DELETE FROM research_garbage WHERE filename=?", (name,))
            continue
        path = (root / name).resolve()
        if path.parent != root or not re.fullmatch(r"[0-9a-f]{64}\.(pdf|png|html|json)", name):
            continue
        try:
            if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == name[:64]:
                path.unlink()
                deleted += 1
            elif path.exists():
                continue
        except OSError:
            continue
        connection.execute("DELETE FROM research_garbage WHERE filename=?", (name,))
    return deleted
