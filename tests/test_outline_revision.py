import json
from io import BytesIO

import httpx
import pytest
from docx import Document
from pydantic import ValidationError
from test_grounded_writing import AutomaticFake
from test_model_adapter import response, settings
from test_research import research_network as research_network

from assignment_agent.grounded_writing import AutomaticWriting, EvidenceFailure
from assignment_agent.model_adapter import ModelError, OpenAICompatibleModel
from assignment_agent.outline_revision import (
    OutlineReviser,
    plan_hash,
    restore_revisions,
    revision_records,
)
from assignment_agent.outline_types import (
    OutlineExamplePlan,
    OutlineRevisionProposal,
    OutlineRevisionReview,
)
from assignment_agent.research import ResearchCollector
from assignment_agent.research_search import LiteratureSearch
from assignment_agent.research_store import context_hash
from assignment_agent.research_types import ResearchError
from assignment_agent.schemas import Artifact, AssignmentSpec, OutlinePlan, RunState, SectionPlan
from assignment_agent.service import open_agent


def recommended():
    parts = [
        SectionPlan(
            title="Analysis",
            word_budget=650,
            purpose="Compare Linux and Windows corrections, with PatchGuard as an AI example.",
            rubric_coverage=["Explain deviations, costs and corrections in detail"],
            evidence_needs=["Evidence for the example mechanism"],
        ),
        SectionPlan(title="Conclusion", word_budget=250, purpose="Summarize the comparison"),
    ]
    return RunState(
        spec=AssignmentSpec(
            title="Kernel architecture",
            sections=[p.title for p in parts],
            section_mode="recommend",
            word_limit=900,
            requirements=["Compare Linux and Windows corrections in detail"],
        ),
        outline_plan=OutlinePlan(parts=parts),
        outline=Artifact(artifact_id="outline", version=1, content="Original AI outline"),
        feedback="大纲示例缺乏直接证据，需要以同等详尽的机制论证覆盖评分项",
        automatic_research=True,
    )


class Adaptive(AutomaticFake):
    autonomous = True
    proposals = 0

    def revise_outline_examples(self, state, context, pool):
        self.proposals += 1
        return OutlineRevisionProposal(
            replacement=OutlineExamplePlan(
                purpose=(
                    "Compare Linux and Windows deviations, costs and corrections "
                    "using verified mechanisms."
                ),
                evidence_needs=["Direct evidence from the verified mechanism source"],
            ),
            reason=(
                "Replace an AI-only example while preserving the material's comparison requirement"
            ),
            source_ids=[pool["sources"][0]["id"]],
        )

    def review_outline_revision(self, state, context, pool, proposal):
        return OutlineRevisionReview(
            hard_requirements_preserved=True,
            scope_preserved=True,
            evidence_supported=True,
            notes=["在正文如实说明原始来源的适用范围。"],
        )


def prepared(tmp_path, research_network, model=None):
    _, _, factory = research_network
    run = recommended()
    pipeline = AutomaticWriting(
        tmp_path / "agent.sqlite", model or Adaptive(), fetcher_factory=factory
    )
    ResearchCollector(pipeline.store, fetcher=factory()).collect("revision", run, [run.spec.title])
    return run, pipeline, pipeline.pool("revision", run)


def revise(pipeline, run, pool, version=1):
    OutlineReviser(pipeline.store, pipeline.model).adapt(
        "revision",
        run,
        {},
        pool,
        version,
        lambda ids: pipeline.check_links("revision", run, ids),
    )


def test_revision_preserves_hard_fields_context_and_original_outline(tmp_path, research_network):
    run, pipeline, pool = prepared(tmp_path, research_network)
    before = run.model_copy(deep=True)
    revise(pipeline, run, pool)
    assert run.spec == before.spec and run.outline == before.outline
    assert context_hash(run) == context_hash(before)
    assert run.outline_plan.parts[1:] == before.outline_plan.parts[1:]
    for field in ["title", "word_budget", "rubric_coverage"]:
        assert getattr(run.outline_plan.parts[0], field) == getattr(
            before.outline_plan.parts[0], field
        )
    records = revision_records(pipeline.store, "revision", run)
    assert [r["status"] for r in records] == ["outline_revised"]
    record = records[0]
    assert record["before"] == before.outline_plan.parts[0].model_dump()
    assert record["after"] == run.outline_plan.parts[0].model_dump()
    assert record["review"]["hard_requirements_preserved"]
    assert not record["review"]["issues"]
    assert record["review"]["notes"] == ["在正文如实说明原始来源的适用范围。"]
    assert record["source_snapshots"][record["source_ids"][0]]["asset"]["sha256"]
    restored = before.model_copy(deep=True)
    restore_revisions(pipeline.store, "revision", restored)
    assert restored.outline_plan == run.outline_plan
    revise(pipeline, restored, pool)
    assert pipeline.model.proposals == 1
    changed = before.model_copy(deep=True)
    changed.spec.word_limit += 1
    assert restore_revisions(pipeline.store, "revision", changed) == []
    assert changed.outline_plan == before.outline_plan


@pytest.mark.parametrize(
    "fault",
    ["hard_requirements_preserved", "scope_preserved", "evidence_supported", "foreign_source"],
)
def test_unsafe_revision_is_recorded_but_never_applied(tmp_path, research_network, fault):
    class Unsafe(Adaptive):
        def revise_outline_examples(self, state, context, pool):
            proposal = super().revise_outline_examples(state, context, pool)
            if fault == "foreign_source":
                proposal.source_ids = ["src-" + "f" * 24]
            return proposal

        def review_outline_revision(self, state, context, pool, proposal):
            assert fault != "foreign_source"
            review = super().review_outline_revision(state, context, pool, proposal)
            setattr(review, fault, False)
            return review

    run, pipeline, pool = prepared(tmp_path, research_network, Unsafe())
    before = run.model_dump()
    with pytest.raises(ResearchError, match="大纲示例修订"):
        revise(pipeline, run, pool)
    assert run.model_dump() == before
    assert (
        revision_records(pipeline.store, "revision", run)[0]["status"]
        == "outline_revision_rejected"
    )
    with pytest.raises(ResearchError, match="大纲示例修订"):
        revise(pipeline, run, {**pool, "sources": list(reversed(pool["sources"]))})
    assert pipeline.model.proposals == 1
    assert len(revision_records(pipeline.store, "revision", run)) == 1


def test_rejected_revision_feedback_returns_to_research_without_writing_old_plan(
    tmp_path, research_network, monkeypatch
):
    # 缓存先通过数量检查；只有独立审查指出缺口后才允许补检索。
    monkeypatch.setattr(LiteratureSearch, "providers", ("openalex",))

    class Rejected(Adaptive):
        feedback = []

        def research_plan(self, state, targets=None):
            self.feedback.append(state.feedback)
            return super().research_plan(state, targets)

        def review_outline_revision(self, state, context, pool, proposal):
            return OutlineRevisionReview(
                hard_requirements_preserved=True,
                scope_preserved=True,
                evidence_supported=False,
                issues=["缺少偏离动机的直接证据"],
            )

        def section_with_evidence(self, state, context, pool):
            raise AssertionError("已知修订失败时不能继续生成旧计划正文")

    model = Rejected()
    run, pipeline, _ = prepared(tmp_path, research_network, model)
    with pytest.raises(EvidenceFailure, match="连续 3 次"):
        pipeline.write("revision", run, {}, 1)
    assert len(model.feedback) == 2
    assert all("缺少偏离动机的直接证据" in text for text in model.feedback)
    assert model.proposals == 1
    assert (
        revision_records(pipeline.store, "revision", run)[0]["status"]
        == "outline_revision_rejected"
    )
    assert not run.drafts


@pytest.mark.parametrize("manual,autonomous", [(True, True), (False, False)])
def test_revision_does_not_override_manual_outlines_or_legacy_review(
    tmp_path, research_network, manual, autonomous
):
    run, pipeline, pool = prepared(tmp_path, research_network)
    if manual:
        run.spec.section_mode = "manual"
    pipeline.model.autonomous = autonomous
    before = run.model_dump()
    revise(pipeline, run, pool)
    assert pipeline.model.proposals == 0 and run.model_dump() == before
    assert revision_records(pipeline.store, "revision", run) == []


def test_revision_survives_generation_failure_and_does_not_approve_old_body(
    tmp_path, research_network
):
    class Outage(Adaptive):
        def section_with_evidence(self, state, context, pool):
            raise ModelError("模型连接中断")

    original, pipeline, pool = prepared(tmp_path, research_network, Outage())
    checkpoint = original.model_copy(deep=True)
    with pytest.raises(ModelError, match="连接中断"):
        pipeline.write("revision", original, {}, 1)
    assert (
        revision_records(pipeline.store, "revision", checkpoint)[0]["status"] == "outline_revised"
    )
    _, _, factory = research_network
    model = Adaptive()
    resumed = AutomaticWriting(pipeline.store.database, model, fetcher_factory=factory)
    content, bundle = resumed.write("revision", checkpoint, {}, 1)
    assert model.proposals == 0
    assert bundle["execution_plan_hash"] == plan_hash(checkpoint)
    assert bundle["content_hash"] and content


def test_changed_execution_plan_requires_rewrite_before_final_audit_and_export(
    tmp_path, research_network
):
    run, pipeline, pool = prepared(tmp_path, research_network)
    run.feedback = ""
    content, bundle = pipeline.write("revision", run, {}, 1)
    old = Artifact(artifact_id="section-1", version=1, content=content)
    run.drafts = [old]
    run.grounded_drafts = {old.artifact_id: bundle}
    run.feedback = "大纲示例需要调整"
    revise(pipeline, run, pool, version=2)
    assert bundle["execution_plan_hash"] != plan_hash(run)
    index, issues = pipeline.audit("revision", run, {})
    assert index == 0 and issues
    with pytest.raises(EvidenceFailure, match="正文版本"):
        pipeline.assert_exportable("revision", run)
    assert run.drafts[0] == old


def test_autonomous_revision_is_checkpointed_audited_and_exported_without_human_review(
    tmp_path, research_network, monkeypatch
):
    from assignment_agent import research

    _, _, factory = research_network
    monkeypatch.setattr(research, "PublicFetcher", factory)

    class EndToEnd(Adaptive):
        def plan(self, state, context):
            return recommended().outline_plan

        def verify_section(self, state, context, draft, pool):
            verdict = super().verify_section(state, context, draft, pool)
            if state.current_index == 0 and "PatchGuard" in state.outline_plan.parts[0].purpose:
                verdict.issues = ["大纲中的额外机制示例缺少依据"]
            return verdict

    db = tmp_path / "web.sqlite"
    with open_agent(db, EndToEnd()) as agent:
        result = agent.start(recommended().spec, "auto-revision")
        assert result["state"]["status"] == "completed" and result["delivery"]["confirmed"]
        assert result["outline_revisions"][0]["status"] == "outline_revised"
        assert "当前执行大纲" in result["effective_outline"]
        assert result["interrupt_id"] is None
        assert all(a["interrupt_id"].startswith("auto:") for a in result["state"]["approvals"])
        assert result["research"]["used_sources"] == 3 and result["research"]["used_images"] == 2
        raw, _ = agent.export_docx("auto-revision")
        assert len(Document(BytesIO(raw)).inline_shapes) == 2
    with open_agent(db, EndToEnd()) as agent:
        assert agent.recover("auto-revision")["state"] == result["state"]


def test_adapter_revision_contract_does_not_accept_budget_or_missing_review_fields():
    with pytest.raises(ValidationError):
        OutlineExamplePlan(purpose="A valid comparison", evidence_needs=["Evidence"], word_budget=1)
    captured = []
    proposals = [
        {
            "replacement": {
                "purpose": "Keep Linux and Windows comparison",
                "evidence_needs": ["Verified mechanism"],
            },
            "reason": "Replace an optional example",
            "source_ids": ["src-" + "a" * 24],
        },
        {
            "hard_requirements_preserved": True,
            "scope_preserved": True,
            "evidence_supported": True,
            "issues": [],
        },
    ]

    def handler(request):
        captured.append(json.loads(request.content))
        return response(json.dumps(proposals[len(captured) - 1]))

    model = OpenAICompatibleModel(settings(), transport=httpx.MockTransport(handler))
    run = recommended()
    pool = {"sources": []}
    proposal = model.revise_outline_examples(run, {}, pool)
    assert model.review_outline_revision(run, {}, pool, proposal).accepted
    data = json.loads(captured[1]["messages"][-1]["content"])
    assert data["proposal"] == proposal.model_dump()
    assert data["current_part"]["word_budget"] == 650
    assert data["spec"]["requirements"] == run.spec.requirements
    bad = OpenAICompatibleModel(
        settings(), transport=httpx.MockTransport(lambda request: response('{"issues":[]}'))
    )
    with pytest.raises(ModelError, match="独立核验格式"):
        bad.review_outline_revision(run, {}, pool, proposal)


@pytest.mark.parametrize("source_ids", [[], ["src-01234567"]])
def test_revision_missing_or_short_source_ids_get_one_local_schema_repair(source_ids):
    captured = []
    full = "src-01234567" + "a" * 16
    proposal = {
        "replacement": {"purpose": "Keep Linux and Windows scope", "evidence_needs": ["Evidence"]},
        "reason": "Adjust only an optional mechanism example",
        "source_ids": source_ids,
    }

    def handler(request):
        captured.append(json.loads(request.content))
        return response(
            json.dumps({**proposal, "source_ids": source_ids if len(captured) == 1 else [full]})
        )

    model = OpenAICompatibleModel(settings(), transport=httpx.MockTransport(handler))
    result = model.revise_outline_examples(recommended(), {}, {"sources": [{"id": full}]})
    assert result.source_ids == [full] and len(captured) == 2
    correction = captured[1]["messages"][-1]["content"]
    assert "source_ids" in correction
    assert settings().api_key.get_secret_value() not in correction


def test_new_contract_reconsiders_old_unchanged_decision(tmp_path, research_network, monkeypatch):
    from assignment_agent import outline_revision

    class Unchanged(Adaptive):
        def revise_outline_examples(self, state, context, pool):
            return OutlineRevisionProposal(reason="Old contract did not produce a usable proposal")

    run, pipeline, pool = prepared(tmp_path, research_network, Unchanged())
    monkeypatch.setattr(outline_revision, "OUTLINE_REVISION_VERSION", 1)
    revise(pipeline, run, pool)
    assert revision_records(pipeline.store, "revision", run)[0]["status"] == "outline_unchanged"
    monkeypatch.setattr(outline_revision, "OUTLINE_REVISION_VERSION", 2)
    pipeline.model = Adaptive()
    revise(pipeline, run, pool)
    assert pipeline.model.proposals == 1
    assert [r["status"] for r in revision_records(pipeline.store, "revision", run)] == [
        "outline_unchanged",
        "outline_revised",
    ]
