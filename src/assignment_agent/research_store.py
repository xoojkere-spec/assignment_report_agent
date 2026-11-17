"""按任务与要求版本持久保存检索进度、来源、图源及人工决定。"""

import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from assignment_agent import research_library
from assignment_agent.image_policy import IMAGE_POLICY, WEB_IMAGE_METHODS, image_warnings
from assignment_agent.migrations.runtime import business_connection
from assignment_agent.research_types import ResearchError
from assignment_agent.schemas import required_web_images


def timestamp():
    return datetime.now(UTC).isoformat()


def context_hash(state):
    spec = state.spec.model_dump() if state.spec else None
    if spec and state.outline_plan:
        # 接受推荐章节只将同一规划写入 sections，不应使已有来源库失效。
        spec["sections"] = [part.title for part in state.outline_plan.parts]
    value = {
        "spec": spec,
        "outline": state.outline.model_dump() if state.outline else None,
        "materials": [item.sha256 for item in state.materials],
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class ResearchStore:
    def __init__(self, database):
        self.database = Path(database)
        self.assets = self.database.parent / "research-assets"

    @contextmanager
    def connect(self):
        with business_connection(self.database, rows=True) as connection:
            yield connection

    def run(self, run_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT context_hash, data FROM research_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return {"context_hash": row[0], **json.loads(row[1])} if row else None

    def save_run(self, run_id, digest, **data):
        old = self.run(run_id)
        value = {**(old if old and old["context_hash"] == digest else {}), **data}
        value.pop("context_hash", None)
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO research_runs VALUES (?, ?, ?) ON CONFLICT(run_id) DO "
                "UPDATE SET context_hash=excluded.context_hash,data=excluded.data",
                (run_id, digest, json.dumps(value, ensure_ascii=False)),
            )

    def entries(self, run_id, digest, kind):
        """内部取证视图：已用来源的最新版本加可恢复临时工作池，临时状态优先。"""
        with self.connect() as connection:
            values = research_library.working_entries(connection, run_id, digest, kind)
            excluded = {
                row[0]: json.loads(row[1])
                for row in connection.execute(
                    "SELECT id,data FROM research_exclusions WHERE run_id=? "
                    "AND context_hash=? AND kind=?",
                    (run_id, digest, kind),
                )
            }
        for item in values:
            if item["id"] in excluded:
                item["review"] = excluded[item["id"]]
        return values

    def save(self, run_id, digest, kind, value):
        with self.connect() as connection:
            old = connection.execute(
                "SELECT data FROM research_candidates WHERE run_id=? AND context_hash=? "
                "AND kind=? AND id=?",
                (run_id, digest, kind, value["id"]),
            ).fetchone()
            old = (
                json.loads(old[0])
                if old
                else next(
                    (
                        item
                        for item in research_library.working_entries(
                            connection, run_id, digest, kind
                        )
                        if item["id"] == value["id"]
                    ),
                    {},
                )
            )
            updated = {**old, **value}
            # 相同文件的重试保留人工决定；来源文件变化则必须重新审阅。
            if old.get("pdf", {}).get("sha256") != updated.get("pdf", {}).get("sha256"):
                updated["review"] = None
                updated["automatic_review"] = None
                connection.execute(
                    "DELETE FROM research_exclusions WHERE run_id=? AND context_hash=? "
                    "AND kind=? AND id=?",
                    (run_id, digest, kind, value["id"]),
                )
            connection.execute(
                "INSERT INTO research_candidates VALUES (?, ?, ?, ?, ?) ON "
                "CONFLICT(run_id,context_hash,kind,id) DO UPDATE SET data=excluded.data",
                (run_id, digest, kind, value["id"], json.dumps(updated, ensure_ascii=False)),
            )
            if updated.get("review"):
                connection.execute(
                    "INSERT OR REPLACE INTO research_exclusions VALUES (?,?,?,?,?)",
                    (
                        run_id,
                        digest,
                        kind,
                        value["id"],
                        json.dumps(updated["review"], ensure_ascii=False),
                    ),
                )
        return updated

    def event(self, run_id, digest, code, **details):
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO research_events(run_id,context_hash,recorded_at,data) "
                "VALUES (?, ?, ?, ?)",
                (
                    run_id,
                    digest,
                    timestamp(),
                    json.dumps({"code": code, **details}, ensure_ascii=False),
                ),
            )

    def asset_valid(self, asset):
        if not asset or not asset.get("sha256"):
            return False
        name = asset.get("filename", "")
        if name != Path(name).name or name not in {
            asset["sha256"] + ".pdf",
            asset["sha256"] + ".png",
            asset["sha256"] + ".html",
            asset["sha256"] + ".json",
        }:
            return False
        path = self.assets / name
        if not path.resolve().is_relative_to(self.assets.resolve()):
            return False
        return path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == asset["sha256"]

    def summary(self, run_id, state):
        run = self.run(run_id)
        current_hash = context_hash(state)
        sources = self.entries(run_id, run["context_hash"], "source") if run else []
        figures = self.entries(run_id, run["context_hash"], "figure") if run else []
        stale = bool(run and run["context_hash"] != current_hash)
        source_ids = set()
        hashes = set()
        for item in sources:
            item["integrity_ok"] = (
                self.asset_valid(item.get("pdf") or item.get("page_asset"))
                if item.get("verified")
                else False
            )
            key = (item.get("pdf") or {}).get("sha256") or item.get("doi") or item.get("url")
            if (
                not stale
                and item.get("verified")
                and item["integrity_ok"]
                and self.accepted(item)
                and key not in hashes
            ):
                source_ids.add(item["id"])
                hashes.add(key)
        image_hashes = set()
        for item in figures:
            item["license_warnings"] = image_warnings(item)
            item["integrity_ok"] = (
                not item.get("retired")
                and self.asset_valid(item)
                and any(
                    source["id"] == item["source_id"]
                    and source["integrity_ok"]
                    and (
                        (source.get("page_asset") or {}).get("sha256") == item.get("page_sha256")
                        and self.asset_valid(item.get("provenance_asset"))
                        if item.get("method") in WEB_IMAGE_METHODS
                        else (source.get("pdf") or {}).get("sha256") == item.get("pdf_sha256")
                    )
                    for source in sources
                )
            )
            if (
                not stale
                and item["integrity_ok"]
                and item["source_id"] in source_ids
                and self.accepted(item)
            ):
                image_hashes.add(item["sha256"])
        minimum_sources = state.spec.min_sources if state.spec else 3
        minimum_images = state.spec.min_web_images if state.spec else 2
        required_images = required_web_images(state)
        bundles = [
            state.grounded_drafts[item.artifact_id]
            for item in state.drafts
            if item.artifact_id in state.grounded_drafts
            and state.grounded_drafts[item.artifact_id]["version"] == item.version
            and state.grounded_drafts[item.artifact_id]["content_hash"]
            == hashlib.sha256(item.content.encode()).hexdigest()
            and state.grounded_drafts[item.artifact_id]["context_hash"] == current_hash
        ]
        cited_ids = {key for bundle in bundles for key in bundle["citation_ids"]}
        inserted_ids = {key for bundle in bundles for key in bundle["figure_ids"]}
        used_sources = len(source_ids & cited_ids)
        used_images = len(
            {
                item["sha256"]
                for item in figures
                if item["id"] in inserted_ids
                and item["integrity_ok"]
                and item["source_id"] in source_ids
            }
        )
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT recorded_at,data FROM research_events WHERE run_id=? ORDER BY "
                "id DESC LIMIT 40",
                (run_id,),
            ).fetchall()
        return {
            "available": state.spec is not None,
            "image_policy": IMAGE_POLICY,
            "automatic": state.automatic_research
            or state.model_profile.kind == "openai_compatible",
            "context_hash": current_hash,
            "stale": stale,
            "status": "stale" if stale else (run or {}).get("status", "not_started"),
            "queries": (run or {}).get("queries", []),
            "issues": (run or {}).get("issues", []),
            "sources": sources,
            "figures": figures,
            "confirmed_sources": len(source_ids),
            "confirmed_images": len(image_hashes),
            "used_sources": used_sources,
            "used_images": used_images,
            "min_sources": minimum_sources,
            "min_web_images": minimum_images,
            "required_web_images": required_images,
            "image_goal_optional": required_images < minimum_images,
            "ready": (used_sources >= minimum_sources and used_images >= required_images)
            if state.automatic_research
            else (len(source_ids) >= minimum_sources and len(image_hashes) >= required_images),
            "events": [{"recorded_at": row[0], **json.loads(row[1])} for row in rows],
        }

    @staticmethod
    def accepted(item):
        # 保留旧记录；显式排除优先于自动决定，不能被后续重试悄悄覆盖。
        manual = item.get("review")
        return (manual or item.get("automatic_review") or {}).get("action") == "accept"

    def writing_event(self, run_id, digest, artifact_id, version, **data):
        with self.connect() as connection:
            stamp = timestamp()
            if data.get("status") == "passed":
                research_library.archive(
                    connection,
                    run_id,
                    digest,
                    artifact_id,
                    version,
                    stamp,
                    data,
                    asset_valid=self.asset_valid,
                )
            connection.execute(
                "INSERT INTO writing_evidence(run_id,context_hash,artifact_id,version,"
                "recorded_at,data) VALUES (?,?,?,?,?,?)",
                (
                    run_id,
                    digest,
                    artifact_id,
                    version,
                    stamp,
                    json.dumps(data, ensure_ascii=False),
                ),
            )

    def reference_checks(self, run_id, digest):
        """读取已通过的最终复核，供同一正文和证据快照继续交付。"""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT artifact_id,version,data FROM writing_evidence WHERE run_id=? "
                "AND context_hash=? "
                "AND json_extract(data,'$.status')='reference_checked' ORDER BY id",
                (run_id, digest),
            ).fetchall()
        checks = {}
        for artifact, version, raw in rows:
            data = json.loads(raw)
            checks[artifact, version, data["fingerprint"]] = data["verdict"]
        return checks

    def cleanup_candidates(self, run_id, digest, *, preserve_pending=False):
        """章节提交后回收未采用材料；中断时只保留当前取证池用于恢复。"""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            keep = {"source": set(), "figure": set()}
            collecting = False
            if preserve_pending:
                saved = connection.execute(
                    "SELECT data FROM research_runs WHERE run_id=? AND context_hash=?",
                    (run_id, digest),
                ).fetchone()
                progress = json.loads(saved[0]) if saved else {}
                collecting = progress.get("collecting", False)
                pending = progress.get("pending_pool")
                if pending is None:
                    for row in connection.execute(
                        "SELECT data FROM writing_evidence WHERE run_id=? AND context_hash=? "
                        "ORDER BY id DESC",
                        (run_id, digest),
                    ):
                        value = json.loads(row[0])
                        if value.get("status") == "passed":
                            break
                        if value.get("pool"):
                            pending = {
                                kind: [item["id"] for item in value["pool"][plural]]
                                for kind, plural in (("source", "sources"), ("figure", "figures"))
                            }
                            break
                for kind in keep:
                    keep[kind].update((pending or {}).get(kind, []))
            rows = connection.execute(
                "SELECT kind,id,data FROM research_candidates WHERE run_id=? AND context_hash=?",
                (run_id, digest),
            ).fetchall()
            for kind, identity, raw in rows:
                item = json.loads(raw)
                # 已计费但还没完成筛选的结果是恢复所需的临时候选。
                if collecting and kind == "source" and not item.get("retired"):
                    keep[kind].add(identity)
                if (
                    kind == "source"
                    and not item.get("verified")
                    and connection.execute(
                        "SELECT 1 FROM research_library_versions WHERE run_id=? "
                        "AND context_hash=? AND kind='source' AND id=? LIMIT 1",
                        (run_id, digest, identity),
                    ).fetchone()
                ):
                    # 已用来源失效的工作标记必须保留，否则会错误复活历史通过快照。
                    keep["source"].add(identity)
            if preserve_pending:
                for kind, identity, raw in rows:
                    if kind == "figure" and json.loads(raw).get("source_id") in keep["source"]:
                        keep["figure"].add(identity)
            removed = 0
            for kind, identity, raw in rows:
                item = json.loads(raw)
                if identity in keep.get(kind, set()):
                    continue
                latest = connection.execute(
                    "SELECT data FROM research_library_versions WHERE run_id=? AND context_hash=? "
                    "AND kind=? AND id=? ORDER BY rowid DESC LIMIT 1",
                    (run_id, digest, kind, identity),
                ).fetchone()
                if latest:
                    approved = json.loads(latest[0])
                    if set(research_library.asset_names(approved)) == set(
                        research_library.asset_names(item)
                    ):
                        # 文件未变的已用来源保留最新核验状态，历史版本仍不可变。
                        connection.execute(
                            "INSERT OR REPLACE INTO research_library_current VALUES (?,?,?,?,?)",
                            (run_id, digest, kind, identity, raw),
                        )
                if item.get("review"):
                    connection.execute(
                        "INSERT OR REPLACE INTO research_exclusions VALUES (?,?,?,?,?)",
                        (
                            run_id,
                            digest,
                            kind,
                            identity,
                            json.dumps(item["review"], ensure_ascii=False),
                        ),
                    )
                for name in research_library.asset_names(item):
                    connection.execute("INSERT OR IGNORE INTO research_garbage VALUES (?)", (name,))
                connection.execute(
                    "DELETE FROM research_candidates WHERE run_id=? AND context_hash=? "
                    "AND kind=? AND id=?",
                    (run_id, digest, kind, identity),
                )
                removed += 1
            deleted = research_library.collect_garbage(connection, self.assets)
        if removed or deleted:
            self.event(
                run_id,
                digest,
                "candidates.cleaned",
                removed=removed,
                deleted_files=deleted,
                recovery_retained=sum(map(len, keep.values())),
            )
        return {"removed": removed, "deleted_files": deleted}

    def cleanup_interrupted(self):
        with self.connect() as connection:
            runs = connection.execute(
                "SELECT run_id,context_hash,data FROM research_runs"
            ).fetchall()
        for run_id, digest, raw in runs:
            value = json.loads(raw)
            with self.connect() as connection:
                writing = connection.execute(
                    "SELECT 1 FROM writing_evidence WHERE run_id=? AND context_hash=? LIMIT 1",
                    (run_id, digest),
                ).fetchone()
            if value.get("status") != "running" and (value.get("automatic") or writing):
                self.cleanup_candidates(
                    run_id, digest, preserve_pending=value.get("status") != "verified"
                )

    def library_summary(self, run_id, state):
        """默认网站只展示与当前正文版本一致、曾核验通过的正式来源。"""
        result = self.summary(run_id, state)
        active = {
            (a.artifact_id, a.version, hashlib.sha256(a.content.encode()).hexdigest())
            for a in state.drafts
            if a.artifact_id in state.grounded_drafts
            and state.grounded_drafts[a.artifact_id].get("context_hash") == context_hash(state)
            and state.grounded_drafts[a.artifact_id].get("version") == a.version
            and state.grounded_drafts[a.artifact_id].get("content_hash")
            == hashlib.sha256(a.content.encode()).hexdigest()
        }
        records = {"source": {}, "figure": {}}
        history = {}
        with self.connect() as connection:
            for row in connection.execute(
                "SELECT artifact_id,version,content_hash,kind,id,recorded_at,data FROM "
                "research_library_versions WHERE run_id=? AND context_hash=? ORDER BY rowid",
                (run_id, context_hash(state)),
            ):
                key = tuple(row[:3])
                version = history.setdefault(
                    key,
                    {
                        "artifact_id": row[0],
                        "version": row[1],
                        "content_hash": row[2],
                        "recorded_at": row[5],
                        "sources": 0,
                        "figures": 0,
                    },
                )
                version["sources" if row[3] == "source" else "figures"] += 1
                if key in active:
                    records[row[3]][row[4]] = json.loads(row[6])
            count = connection.execute(
                "SELECT COUNT(*) FROM research_candidates WHERE run_id=? AND context_hash=?",
                (run_id, context_hash(state)),
            ).fetchone()[0]
        current = {
            kind: {item["id"]: item for item in result[plural]}
            for kind, plural in (("source", "sources"), ("figure", "figures"))
        }
        for kind, plural in (("source", "sources"), ("figure", "figures")):
            for key, item in records[kind].items():
                live = current[kind].get(key, {})
                item["integrity_ok"] = bool(live.get("integrity_ok")) and (
                    (item.get("pdf") or item.get("page_asset"))
                    == (live.get("pdf") or live.get("page_asset"))
                    if kind == "source"
                    else item.get("sha256") == live.get("sha256") and live.get("integrity_ok")
                )
                if kind == "figure":
                    if item.get("sha256") == live.get("sha256"):
                        for field in ("license", "license_review", "image_review"):
                            if field in live:
                                item[field] = live[field]
                    item["license_warnings"] = image_warnings(item)
            result[plural] = list(records[kind].values())
        return {
            **result,
            "candidate_count": count,
            "history": list(history.values()),
            "storage": "used_sources_only",
        }

    def last_writing_issues(self, run_id, digest, artifact_id, version):
        rows = self.recent_writing_records(run_id, digest, artifact_id, version)
        failures = []
        for data in rows:
            if data.get("status") == "passed":
                break
            if data.get("status") in {"failed", "bindings_rejected"}:
                failures.append(data.get("issues", []))
        # 中途草稿和局部修复不代表整章通过；保留此前语义限制，避免被后续格式错误覆盖。
        return list(dict.fromkeys(issue for group in reversed(failures) for issue in group))

    def resumable_draft(self, run_id, digest, artifact_id, version):
        for data in self.recent_writing_records(run_id, digest, artifact_id, version):
            if data.get("status") == "passed" or (
                data.get("status") == "failed" and data.get("next_action") != "repair"
            ):
                break
            if data.get("status") in {"drafted", "markers_expanded", "bindings_repaired"}:
                return data.get("draft")
        return None

    def pending_evidence_review(self, run_id, digest, artifact_id, version, *, minimum_policy=None):
        """恢复同一版最近的缺口及实际送审片段，以便先复用后来可选的证据。"""
        gaps = []
        outdated = False
        for data in self.recent_writing_records(run_id, digest, artifact_id, version):
            if data.get("status") == "passed":
                break
            if data.get("status") == "failed" and data.get("next_action") != "repair":
                if gaps:
                    break
                gaps = data.get("evidence_gaps", [])
                if not gaps:
                    break
                # 预算用尽后允许新版核验规则先用旧证据复查，真正的缺口仍由新版记录阻断。
                outdated = (
                    minimum_policy is not None and data.get("evidence_policy", 1) < minimum_policy
                )
            elif gaps and data.get("status") in {
                "drafted",
                "markers_expanded",
                "bindings_repaired",
                "outline_revision_proposed",
            }:
                return ([] if outdated else gaps), data.get("pool")
        return ([] if outdated else gaps), None

    def recent_writing_records(self, run_id, digest, artifact_id, version):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT data FROM writing_evidence WHERE run_id=? AND context_hash=? "
                "AND artifact_id=? AND version=? ORDER BY id DESC LIMIT 60",
                (run_id, digest, artifact_id, version),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def passed_source_ids(self, run_id, digest, artifact_id=None):
        """从同一要求版本的最近通过稿恢复取证线索，不代表旧引文仍然有效。"""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT artifact_id,data FROM writing_evidence WHERE run_id=? AND context_hash=? "
                "ORDER BY id DESC",
                (run_id, digest),
            ).fetchall()
        seen = set()
        sources = []
        for row in rows:
            if row[0] in seen or (artifact_id and row[0] != artifact_id):
                continue
            value = json.loads(row[1])
            if value.get("status") == "passed":
                seen.add(row[0])
                sources.extend(value.get("evidence", {}).get("sources", {}))
        return list(dict.fromkeys(sources))

    def review(self, run_id, state, request):
        run = self.run(run_id)
        if (
            request.context_hash != context_hash(state)
            or not run
            or run["context_hash"] != request.context_hash
        ):
            raise ResearchError("来源库要求版本已变化，请刷新并重新检索")
        entry = next(
            (
                item
                for item in self.entries(run_id, request.context_hash, request.kind)
                if item["id"] == request.entry_id
            ),
            None,
        )
        if entry is None:
            raise ResearchError("来源记录不存在")
        if request.action == "accept":
            if request.kind == "source":
                if not entry.get("verified") or not self.asset_valid(
                    entry.get("pdf") or entry.get("page_asset")
                ):
                    raise ResearchError("来源尚未核对成功或缓存已损坏，请恢复检索")
            else:
                current = self.summary(run_id, state)
                if not next(item for item in current["figures"] if item["id"] == entry["id"])[
                    "integrity_ok"
                ]:
                    raise ResearchError("图源文件缺失或已变化，请恢复检索")
                if entry["license"]["status"] != "declared" and not (
                    request.permission_confirmed and request.note.strip()
                ):
                    raise ResearchError("图片许可未确定，请先人工核对并填写使用许可确认说明")
        decision = {
            "action": request.action,
            "note": request.note,
            "permission_confirmed": request.permission_confirmed,
            "recorded_at": timestamp(),
        }
        entry["review_history"] = [*entry.get("review_history", []), decision]
        entry["review"] = decision
        self.save(run_id, request.context_hash, request.kind, entry)
        self.event(
            run_id,
            request.context_hash,
            "review." + request.action,
            kind=request.kind,
            entry_id=entry["id"],
        )


def interrupt_research(database):
    if not Path(database).exists():
        return
    store = ResearchStore(database)
    with store.connect() as connection:
        rows = connection.execute("SELECT run_id,context_hash,data FROM research_runs").fetchall()
        for row in rows:
            value = json.loads(row[2])
            if value.get("status") == "running":
                value.update(
                    status="interrupted", issues=["服务重启中断检索，已保存结果保留，可恢复检索"]
                )
                connection.execute(
                    "UPDATE research_runs SET data=? WHERE run_id=?",
                    (json.dumps(value, ensure_ascii=False), row[0]),
                )
