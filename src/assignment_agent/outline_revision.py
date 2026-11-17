"""保留原始大纲及来源上下文，只修订执行计划中的机制示例并记录独立核验。"""

import hashlib
import json
import re

from assignment_agent.evidence_types import EvidenceGap
from assignment_agent.outline_types import OUTLINE_REVISION_VERSION, OutlineRevisionReview
from assignment_agent.research_store import context_hash
from assignment_agent.research_types import EvidenceFailure, MissingEvidence, ResearchError
from assignment_agent.schemas import SectionPlan


def plan_hash(run, index=None):
    if not run.outline_plan:
        return None
    part = run.outline_plan.parts[run.current_index if index is None else index]
    return hashlib.sha256(json.dumps(part.model_dump(), sort_keys=True).encode()).hexdigest()


def revision_records(store, run_id, run):
    with store.connect() as db:
        rows = db.execute(
            "SELECT artifact_id,version,recorded_at,data FROM writing_evidence "
            "WHERE run_id=? AND context_hash=? AND json_extract(data,'$.status') "
            "IN ('outline_revised','outline_revision_rejected','outline_unchanged') ORDER BY id",
            (run_id, context_hash(run)),
        ).fetchall()
    result = []
    for artifact, version, recorded_at, raw in rows:
        data = json.loads(raw)
        if data.get("status") in {
            "outline_revised",
            "outline_revision_rejected",
            "outline_unchanged",
        }:
            result.append(
                {**data, "artifact_id": artifact, "version": version, "recorded_at": recorded_at}
            )
    return result


def restore_revisions(store, run_id, run):
    records = revision_records(store, run_id, run)
    if not run.outline_plan:
        return records
    approved = [item for item in records if item["status"] == "outline_revised"]
    for index in sorted({item["part_index"] for item in approved}):
        history = [item for item in approved if item["part_index"] == index]
        if not isinstance(index, int) or not 0 <= index < len(run.outline_plan.parts):
            raise ResearchError("大纲修订记录的章节已变化，不能恢复旧修订")
        current = run.outline_plan.parts[index].model_dump()
        known = [part for item in history for part in (item["before"], item["after"])]
        if current not in known:
            raise ResearchError("当前执行计划与已核验大纲修订不对应，不能覆盖")
        after = SectionPlan.model_validate(history[-1]["after"])
        if not all(
            OutlineRevisionReview.model_validate(item["review"]).accepted for item in history
        ):
            raise ResearchError("大纲修订缺少通过的独立审查，不能恢复")
        # 标题、预算和评分对应必须始终与原计划相同，防止恢复绕过修订边界。
        for field in ("title", "word_budget", "rubric_coverage"):
            if getattr(after, field) != history[0]["before"][field]:
                raise ResearchError("大纲修订改变了受保护的章节约束")
        run.outline_plan.parts[index] = after
    return records


class OutlineReviser:
    def __init__(self, store, model):
        self.store, self.model = store, model

    @staticmethod
    def reject(issues, review=None):
        details = ["大纲示例修订未通过独立审查：" + "；".join(issues)]
        if review:
            result = OutlineRevisionReview.model_validate(review)
            if (
                result.hard_requirements_preserved
                and result.scope_preserved
                and not result.evidence_supported
            ):
                raise MissingEvidence(
                    details, [EvidenceGap(kind="source", detail=issue[:1000]) for issue in issues]
                )
        raise EvidenceFailure(details)

    def adapt(self, run_id, run, context, pool, version, check_sources):
        records = restore_revisions(self.store, run_id, run)
        artifact = f"section-{run.current_index + 1}"
        if (
            not getattr(self.model, "autonomous", False)
            or not run.outline_plan
            or run.spec.section_mode != "recommend"
            or pool["targets"].get("section_role") != "body"
            or not re.search(r"大纲|必答|outline", run.feedback, re.I)
        ):
            return
        if any(
            r["artifact_id"] == artifact
            and r["version"] == version
            and r["status"] == "outline_revised"
            for r in records
        ):
            return
        before = run.outline_plan.parts[run.current_index]
        key = hashlib.sha256(
            json.dumps(
                {
                    "policy_version": OUTLINE_REVISION_VERSION,
                    "part": before.model_dump(),
                    "sources": [
                        {
                            **item,
                            "passages": sorted(
                                item["passages"], key=lambda p: (p["page"] or 0, p["text"])
                            ),
                        }
                        for item in sorted(pool["sources"], key=lambda s: s["id"])
                    ],
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        previous = next(
            (
                r
                for r in records
                if r["artifact_id"] == artifact
                and r["version"] == version
                and r.get("attempt_key") == key
            ),
            None,
        )
        if previous:
            if previous["status"] == "outline_revision_rejected":
                self.reject(
                    previous.get("issues") or ["原方案未通过独立审查"], previous.get("review")
                )
            return
        proposal = self.model.revise_outline_examples(run, context, pool)
        details = {
            "part_index": run.current_index,
            "before": before.model_dump(),
            "reason": proposal.reason,
            "source_ids": proposal.source_ids,
            "attempt_key": key,
            "policy_version": OUTLINE_REVISION_VERSION,
        }

        def record(status, **extra):
            self.store.writing_event(
                run_id, context_hash(run), artifact, version, status=status, **details, **extra
            )
            self.store.event(
                run_id,
                context_hash(run),
                status.replace("_", "."),
                artifact_id=artifact,
                reason=proposal.reason,
                **extra,
            )

        self.store.writing_event(
            run_id,
            context_hash(run),
            artifact,
            version,
            status="outline_revision_proposed",
            **details,
            proposal=proposal.model_dump(),
            pool=pool,
        )
        if proposal.replacement is None:
            record("outline_unchanged")
            return
        after = SectionPlan.model_validate(
            {**before.model_dump(), **proposal.replacement.model_dump()}
        )
        if after == before:
            record("outline_unchanged")
            return
        available = {source["id"] for source in pool["sources"]}
        if not proposal.source_ids or not set(proposal.source_ids).issubset(available):
            record("outline_revision_rejected", issues=["修订未绑定当前已核验的来源"])
            self.reject(["修订未绑定当前已核验的来源"])
        review = self.model.review_outline_revision(run, context, pool, proposal)
        if not review.accepted:
            issues = review.issues or ["独立审查未确认硬要求、选型或证据支持性"]
            record(
                "outline_revision_rejected",
                issues=issues,
                review=review.model_dump(),
            )
            self.reject(issues, review.model_dump())
        check_sources(sorted(set(proposal.source_ids)))
        snapshots = {
            item["id"]: {
                "asset": item.get("pdf") or item.get("page_asset"),
                "text_asset": item.get("text_asset"),
                "url": item.get("pdf_url") or item.get("url"),
                "doi": item.get("doi"),
            }
            for item in self.store.entries(run_id, context_hash(run), "source")
            if item["id"] in proposal.source_ids
        }
        # 原始 outline Artifact 不变，保持材料/来源上下文哈希稳定；执行计划及前后差异另存。
        record(
            "outline_revised",
            after=after.model_dump(),
            review=review.model_dump(),
            source_snapshots=snapshots,
        )
        run.outline_plan.parts[run.current_index] = after
