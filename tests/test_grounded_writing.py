import json
from io import BytesIO
from zipfile import ZipFile

import pytest
import test_research
from conftest import legacy_agent as open_agent
from docx import Document
from test_research import paper_pdf
from test_research import research_network as research_network
from test_workflow import decision

from assignment_agent import research
from assignment_agent.evidence_types import (
    CitationRepair,
    CitationRepairs,
    EvidenceDraft,
    EvidenceGap,
    EvidenceVerdict,
    FigureUse,
    FigureVerdict,
)
from assignment_agent.fake_model import FakeModel
from assignment_agent.grounded_writing import (
    AutomaticWriting,
    EvidenceFailure,
    LocalRepairFailure,
    auto_license,
)
from assignment_agent.research_budget import SearchBudget
from assignment_agent.research_store import ResearchStore, context_hash
from assignment_agent.schemas import Artifact, AssignmentSpec, RunState


def automatic_state():
    return RunState(
        spec=AssignmentSpec(
            title="Kernel architecture", sections=["Analysis", "Conclusion"], word_limit=900
        ),
        automatic_research=True,
    )


def test_optional_image_does_not_accept_unverified_figure(tmp_path):
    pipeline = AutomaticWriting(tmp_path / "optional-figure.sqlite", FakeModel())
    pool = {
        "sources": [],
        "figures": [],
        "targets": {
            "new_sources": 0,
            "new_figures": 0,
            "used_sources": [],
            "used_figures": [],
            "image_goal_optional": True,
        },
    }
    draft = EvidenceDraft(content="本章说明设计结构。\n\n[[figure:fig-" + "a" * 24 + "]]\n")
    with pytest.raises(LocalRepairFailure, match="正文插图位置与图片记录不对应"):
        pipeline.validate(draft, pool)


def test_no_image_section_ignores_verifier_record_for_candidate_only(tmp_path):
    candidate_id = "fig-" + "a" * 24

    class CandidateOnly(FakeModel):
        calls = 0

        def verify_section(self, state, context, draft, pool):
            self.calls += 1
            return EvidenceVerdict(
                citations=[],
                figures=[
                    FigureVerdict(
                        figure_id=candidate_id,
                        relevant=False,
                        caption_accurate=False,
                        license_applicable=False,
                        reason="候选图片并未插入正文",
                    )
                ],
            )

    model = CandidateOnly()
    pipeline = AutomaticWriting(tmp_path / "candidate-only.sqlite", model)
    no_image = EvidenceDraft(content="附录说明。")
    verdict = pipeline.checked_verdict(
        RunState(), {}, no_image, {"figures": [{"id": candidate_id}]}
    )
    assert verdict.figures == []
    assert model.calls == 1

    with_image = EvidenceDraft(
        content="附录说明。\n\n[[figure:" + candidate_id + "]]",
        figures=[
            FigureUse(
                figure_id=candidate_id,
                caption="候选图片",
                explanation="本图展示候选图片的含义。",
            )
        ],
    )
    assert AutomaticWriting.verdict_structure_errors(
        with_image, EvidenceVerdict(citations=[], figures=[])
    ) == ["语义核验遗漏或重复了图片记录"]


def test_exhausted_contents_checkpoint_resumes_without_search_and_moves_source_debt(tmp_path):
    sections = [
        "Cover page, Declaration Form, and marking rubrics",
        "Table of Contents",
        "Introduction",
        "Stakeholders",
        "Software Requirements",
        "Sustainability and SDG traceability",
        "Peer Assessment and Workload Matrix",
        "References",
        "Appendices",
    ]
    run = RunState(
        spec=AssignmentSpec(
            title="Underwater SRS", sections=sections, word_limit=7500, min_web_images=0
        ),
        current_index=1,
        automatic_research=True,
        drafts=[Artifact(artifact_id="section-1", version=1, content="封面")],
        grounded_drafts={"section-1": {"citation_ids": ["src-" + "a" * 24], "figure_ids": []}},
    )
    db = tmp_path / "exhausted-toc.sqlite"
    store = ResearchStore(db)
    budget = SearchBudget(store, "resume", 2)
    for _ in range(6):
        budget.reserve("old.example")
    pipeline = AutomaticWriting(db, FakeModel())
    content, bundle = pipeline.write("resume", run, {}, 1)
    assert content.startswith("1. Introduction\n2. Stakeholders")
    assert bundle["citation_ids"] == []
    assert budget.usage()["chapter_used"] == 6
    run.current_index = 2
    assert pipeline.targets(run)["new_sources"] == 0
    run.current_index = 5
    assert pipeline.targets(run)["new_sources"] == 2
    assert pipeline.targets(run)["source_topup"]
    run.current_index = 6
    assert pipeline.targets(run)["section_role"] == "administrative"
    assert pipeline.targets(run)["new_sources"] == 0


def test_verification_notes_do_not_block_but_unsupported_citations_still_do():
    draft = EvidenceDraft(content="Analysis")
    verdict = EvidenceVerdict(citations=[], figures=[], notes=["本章图片目标为零，不因无图失败"])
    assert AutomaticWriting.verdict_errors(draft, verdict) == []
    verdict.issues = ["存在未绑定来源的产品属性断言"]
    assert AutomaticWriting.verdict_errors(draft, verdict) == verdict.issues


def test_recovery_retains_last_evidence_failure_and_does_not_reuse_other_versions(
    tmp_path, research_network
):
    _, _, factory = research_network
    run = automatic_state()
    run.automatic_research = True
    model = FakeModel()
    pipeline = AutomaticWriting(tmp_path / "agent.sqlite", model, fetcher_factory=factory)
    pipeline.store.writing_event(
        "resume",
        context_hash(run),
        "section-1",
        1,
        status="failed",
        issues=["上次引文误用了 hypervisor 的结论"],
    )
    pipeline.store.writing_event(
        "resume",
        context_hash(run),
        "section-1",
        1,
        status="failed",
        issues=["引用 1 没有绑定正文同段论述"],
    )
    pipeline.store.writing_event(
        "resume",
        context_hash(run),
        "section-1",
        1,
        status="drafted",
    )

    class CheckFeedback(FakeModel):
        def research_plan(self, state, targets=None):
            assert "上次引文误用了 hypervisor 的结论" in state.feedback
            assert "引用 1 没有绑定正文同段论述" in state.feedback
            return super().research_plan(state, targets)

    pipeline.model = CheckFeedback()
    pipeline.write("resume", run, {}, 1)
    assert pipeline.store.last_writing_issues("resume", context_hash(run), "section-1", 1) == []
    assert pipeline.store.last_writing_issues("resume", context_hash(run), "section-1", 2) == []


def test_semantic_feedback_survives_a_later_binding_failure(tmp_path, research_network):
    _, _, factory = research_network

    class RetryHistory(FakeModel):
        writes = 0
        feedback = []
        repairs = []

        def research_plan(self, state, targets=None):
            self.feedback.append(state.feedback)
            return super().research_plan(state, targets)

        def section_with_evidence(self, state, context, pool):
            self.writes += 1
            draft = super().section_with_evidence(state, context, pool)
            if self.writes == 2:
                self.valid_claim = draft.citations[0].claim
                draft.citations[0].claim = "这句话没有出现在真实正文中，不能建立绑定"
            return draft

        def repair_bindings(self, state, draft, pool, issues):
            self.repairs.append((state.feedback, issues))
            fixed = draft.model_copy(deep=True)
            fixed.citations[0].claim = self.valid_claim
            return fixed

        def verify_section(self, state, context, draft, pool):
            result = super().verify_section(state, context, draft, pool)
            if self.writes == 1:
                result.citations[0].supported = False
                result.citations[0].reason = "必须限定实验平台、系统版本和测量负载"
            return result

    model = RetryHistory()
    pipeline = AutomaticWriting(tmp_path / "history.sqlite", model, fetcher_factory=factory)
    run = automatic_state()
    _, bundle = pipeline.write("history", run, {}, 1)
    assert model.writes == 2 and len(model.feedback) == len(model.repairs) == 1
    assert "必须限定实验平台、系统版本和测量负载" in model.repairs[0][0]
    assert "引用 1 没有绑定正文同段论述" in model.repairs[0][1]
    assert bundle["verdict"]["citations"][0]["supported"]
    assert pipeline.store.last_writing_issues("history", context_hash(run), "section-1", 1) == []


def test_automatic_write_collects_inserts_and_verifies_without_human_search(
    tmp_path, research_network
):
    calls, _, factory = research_network
    pipeline = AutomaticWriting(tmp_path / "agent.sqlite", FakeModel(), fetcher_factory=factory)
    run = automatic_state()
    content, bundle = pipeline.write("auto", run, {}, 1)
    assert "(Alice Researcher0, 2024)" in content
    assert "[[cite:" not in content
    assert "![Figure 1." in content
    assert "Figure 1. Figure 1:" not in content
    assert "License: https://creativecommons.org/licenses/by/4.0/" in content
    assert len(bundle["citation_ids"]) == 3
    assert len(bundle["figure_ids"]) == 2
    assert bundle["verdict"]["citations"][0]["supported"]
    assert any(call.headers["host"] == "papers.example" for call in calls)
    summary = ResearchStore(tmp_path / "agent.sqlite").summary("auto", run)
    assert summary["confirmed_sources"] == 3
    assert summary["confirmed_images"] == 2
    assert all(not item.get("review") for item in summary["sources"])


def test_bad_quote_is_repaired_on_the_same_draft_before_exposing_it(tmp_path, research_network):
    _, _, factory = research_network

    class BadOnce(FakeModel):
        writes = 0
        repairs = 0

        def section_with_evidence(self, state, context, pool):
            self.writes += 1
            draft = super().section_with_evidence(state, context, pool)
            if self.writes == 1:
                draft.citations[
                    0
                ].quote = "This invented quotation is absent from the actual source paper."
            return draft

        def repair_citations(self, state, draft, pool, indices):
            self.repairs += 1
            assert indices == [0]
            use = draft.citations[0]
            source = next(item for item in pool["sources"] if item["id"] == use.source_id)
            return CitationRepairs(
                repairs=[
                    CitationRepair(index=0, page=source["passages"][0]["page"], quote=use.claim)
                ]
            )

    model = BadOnce()
    pipeline = AutomaticWriting(tmp_path / "agent.sqlite", model, fetcher_factory=factory)
    _, bundle = pipeline.write("auto", automatic_state(), {}, 1)
    assert model.writes == model.repairs == 1
    assert "invented quotation" not in json.dumps(bundle)
    with pipeline.store.connect() as connection:
        records = [
            json.loads(row[0])
            for row in connection.execute("SELECT data FROM writing_evidence ORDER BY id")
        ]
    assert [item["status"] for item in records] == ["drafted", "bindings_repaired", "passed"]
    assert "invented quotation" in records[0]["draft"]["citations"][0]["quote"]
    assert records[0]["draft"]["content"] == bundle["draft"]["content"]


def test_semantic_failure_is_bounded_and_never_counts_as_confirmed(tmp_path, research_network):
    _, _, factory = research_network

    class Unsupported(FakeModel):
        checks = 0

        def verify_section(self, state, context, draft, pool):
            self.checks += 1
            result = super().verify_section(state, context, draft, pool)
            result.citations[0].supported = False
            result.citations[0].reason = "原文不支持该论述"
            return result

    model = Unsupported()
    run = automatic_state()
    pipeline = AutomaticWriting(tmp_path / "agent.sqlite", model, fetcher_factory=factory)
    with pytest.raises(EvidenceFailure, match="连续 3 次"):
        pipeline.write("auto", run, {}, 1)
    assert model.checks == 3
    assert not pipeline.store.summary("auto", run)["ready"]
    assert pipeline.store.summary("auto", run)["confirmed_sources"] == 0


class AutomaticFake(FakeModel):
    automatic_research = True


def test_reference_audit_disambiguates_author_year_across_chapters(
    tmp_path, monkeypatch, research_network
):
    _, _, factory = research_network
    monkeypatch.setattr(research, "PublicFetcher", factory)
    old_candidate = test_research.candidate
    old_pdf = test_research.paper_pdf

    def same_author_candidate(index=0):
        return old_candidate(index).model_copy(update={"authors": ["Example Institute"]})

    def same_author_pdf(index=0, **options):
        return old_pdf(index, **options).replace(
            f"Alice Researcher{index}".encode(), b"Example Institute"
        )

    monkeypatch.setattr(test_research, "candidate", same_author_candidate)
    monkeypatch.setattr(test_research, "paper_pdf", same_author_pdf)

    class RecordFormatting(AutomaticFake):
        written = []
        audited = []

        def section_with_evidence(self, state, context, pool):
            self.written.append(state.current_index)
            return super().section_with_evidence(state, context, pool)

        def verify_section(self, state, context, draft, pool):
            if pool["verification_scope"]["stage"] == "final_reference":
                self.audited.append((pool["inserted_article"], pool["reference_list"]))
            verdict = super().verify_section(state, context, draft, pool)
            if pool["verification_scope"]["stage"] == "section" and not draft.citations:
                verdict.evidence_gaps = [
                    EvidenceGap(kind="source", detail="本章的技术陈述需要可核验来源")
                ]
            return verdict

    model = RecordFormatting()
    spec = automatic_state().spec.model_copy(
        update={"sections": ["Introduction", "Analysis", "Trade-offs", "References"]}
    )
    from assignment_agent.service import open_agent as open_autonomous

    with open_autonomous(tmp_path / "agent.sqlite", model) as agent:
        completed = agent.start(spec, "citations")
        raw, _ = agent.export_docx("citations")
    assert completed["state"]["status"] == "completed"
    assert model.written == [0, 0, 1, 2] and len(model.audited) == 3
    first, *_ = completed["state"]["drafts"]
    assert first["version"] == 2 and "(Example Institute, 2024a)" in first["content"]
    assert any(
        item["artifact_id"] == "section-1" and "(Example Institute, 2024)" in item["content"]
        for item in completed["state"]["history"]
    )
    assert "(Example Institute, 2024a)" in model.audited[0][0]
    paragraphs = "\n".join(p.text for p in Document(BytesIO(raw)).paragraphs)
    for suffix in ("a", "b", "c"):
        assert f"(Example Institute, 2024{suffix})" in paragraphs
        assert f"Example Institute (2024{suffix}). Paper architecture" in paragraphs
    assert completed["research"]["used_sources"] == 3
    assert completed["research"]["used_images"] == 2


def test_graph_automatic_research_recovery_completion_and_embedded_docx(
    tmp_path, research_network, monkeypatch
):
    calls, _, factory = research_network
    monkeypatch.setattr(research, "PublicFetcher", factory)
    db = tmp_path / "agent.sqlite"
    with open_agent(db, AutomaticFake()) as agent:
        result = agent.start(automatic_state().spec, "auto")
        assert not calls  # 大纲确认前不启动写作或自动检索。
        result = agent.decide("auto", decision(result))
        assert result["state"]["pending"]["artifact_id"] == "section-1"
        assert result["research"]["confirmed_sources"] == 3
        assert result["research"]["confirmed_images"] == 2
        assert result["research"]["ready"]
        assert not result["delivery"]["ready"]
        call_count = len(calls)
    with open_agent(db, AutomaticFake()) as agent:
        assert agent.recover("auto") == result
        assert len(calls) == call_count
        result = agent.decide("auto", decision(result))
        assert "References" in result["state"]["pending"]["content"]
        assert result["research"]["confirmed_sources"] == 3
        assert result["research"]["confirmed_images"] == 2
        result = agent.decide("auto", decision(result))
        assert result["state"]["status"] == "completed"
        assert any(item["code"] == "reference.passed" for item in result["research"]["events"])
        result = agent.confirm_completion("auto", result["delivery"]["content_hash"])
        assert result["delivery"]["confirmed"]
    with open_agent(db) as agent:
        raw, _ = agent.export_docx("auto")
        doc = Document(BytesIO(raw))
        assert len(doc.inline_shapes) == 2
        assert any(p.text == "References" for p in doc.paragraphs)
        assert any("DOI: 10.1234/paper0" in p.text for p in doc.paragraphs)
        with ZipFile(BytesIO(raw)) as archive:
            assert len([name for name in archive.namelist() if name.startswith("word/media/")]) == 2
        assert all(not item.get("review") for item in agent.status("auto")["research"]["sources"])


def test_wrong_figure_caption_reuses_evidence_and_checks_inserted_article(
    tmp_path, research_network
):
    _, _, factory = research_network

    class BadCaptionOnce(FakeModel):
        checks = 0

        def verify_section(self, state, context, draft, pool):
            self.checks += 1
            assert "![Figure 1." in pool["inserted_article"]
            assert "[[cite:" not in pool["inserted_article"]
            result = super().verify_section(state, context, draft, pool)
            if self.checks == 1:
                result.figures[0].caption_accurate = False
                result.figures[0].reason = "新图注超出论文原图的含义"
            return result

    model = BadCaptionOnce()
    pipeline = AutomaticWriting(tmp_path / "agent.sqlite", model, fetcher_factory=factory)
    _, bundle = pipeline.write("auto", automatic_state(), {}, 1)
    assert model.checks == 2
    assert bundle["verdict"]["figures"][0]["caption_accurate"]
    events = pipeline.store.summary("auto", automatic_state())["events"]
    assert len([event for event in events if event["code"] == "writing.search"]) == 1
    assert len([event for event in events if event["code"] == "writing.reuse"]) == 1


def test_failed_automatic_writing_resumes_from_checkpoint_without_new_approval(
    tmp_path, research_network, monkeypatch
):
    _, _, factory = research_network
    monkeypatch.setattr(research, "PublicFetcher", factory)

    class Unsupported(AutomaticFake):
        def verify_section(self, state, context, draft, pool):
            verdict = super().verify_section(state, context, draft, pool)
            verdict.citations[0].supported = False
            return verdict

    db = tmp_path / "agent.sqlite"
    with open_agent(db, Unsupported()) as agent:
        result = agent.start(automatic_state().spec, "auto")
        with pytest.raises(EvidenceFailure, match="连续 3 次"):
            agent.decide("auto", decision(result))
        failed = agent.status("auto")
        assert failed["state"]["status"] == "failed"
        assert failed["failed_nodes"] == ["section"]
        assert not failed["state"]["drafts"]
        assert len(failed["state"]["approvals"]) == 1
    with open_agent(db, AutomaticFake()) as agent:
        recovered = agent.recover("auto")
        assert recovered["state"]["pending"]["artifact_id"] == "section-1"
        assert len(recovered["state"]["approvals"]) == 1
        assert recovered["state"]["outline"]["version"] == 1


def test_final_reference_failure_rewrites_earlier_chapter_as_new_version(
    tmp_path, research_network, monkeypatch
):
    _, _, factory = research_network
    monkeypatch.setattr(research, "PublicFetcher", factory)

    class FinalFailureOnce(AutomaticFake):
        failed_final = False

        def verify_section(self, state, context, draft, pool):
            verdict = super().verify_section(state, context, draft, pool)
            if "references/verification/guide.md" in context and not self.failed_final:
                assert pool["verification_scope"]["stage"] == "final_reference"
                assert "DOI: 10.1234/paper0" in pool["reference_list"]
                self.failed_final = True
                verdict.citations[0].supported = False
                verdict.citations[0].reason = "最终检查发现第一章论述与引用不对应"
            return verdict

    model = FinalFailureOnce()
    with open_agent(tmp_path / "agent.sqlite", model) as agent:
        current = agent.start(automatic_state().spec, "auto")
        for _ in range(3):
            current = agent.decide("auto", decision(current))
        assert model.failed_final
        assert current["state"]["status"] == "awaiting_user"
        assert current["state"]["pending"]["artifact_id"] == "section-1"
        assert current["state"]["pending"]["version"] == 2
        assert any(event["code"] == "reference.failed" for event in current["research"]["events"])
        assert any(
            item["artifact_id"] == "section-1" and item["version"] == 1
            for item in current["state"]["approvals"]
        )
        current = agent.decide("auto", decision(current))
        assert current["state"]["status"] == "completed"
        assert current["state"]["pending"] is None
        assert current["state"]["drafts"][1]["version"] == 1


def test_missing_export_image_blocks_delivery_and_recovery_rewrites(
    tmp_path, research_network, monkeypatch
):
    _, _, factory = research_network
    monkeypatch.setattr(research, "PublicFetcher", factory)
    db = tmp_path / "agent.sqlite"
    with open_agent(db, AutomaticFake()) as agent:
        current = agent.start(automatic_state().spec, "auto")
        for _ in range(3):
            current = agent.decide("auto", decision(current))
        current = agent.confirm_completion("auto", current["delivery"]["content_hash"])
        figure = current["research"]["figures"][0]
        (ResearchStore(db).assets / figure["filename"]).unlink()
        broken = agent.status("auto")
        assert broken["state"]["status"] == "failed"
        assert broken["failed_nodes"] == ["reference_integrity"]
        assert not broken["delivery"]["ready"]
        with pytest.raises(ValueError):
            agent.export_docx("auto")
        recovered = agent.recover("auto")
        assert recovered["state"]["pending"]["artifact_id"] == "section-1"
        assert recovered["state"]["pending"]["version"] == 2
        assert not recovered["delivery"]["confirmed"]


@pytest.mark.parametrize(
    "status,url,allowed",
    [
        ("declared", "https://creativecommons.org/licenses/by/4.0/", True),
        ("declared", "https://creativecommons.org/licenses/by-sa/4.0/", True),
        ("declared", "https://creativecommons.org/licenses/by-nc/4.0/", False),
        ("needs_review", "https://creativecommons.org/licenses/by/4.0/", False),
        ("unknown", None, False),
    ],
)
def test_uncertain_or_restricted_image_license_is_not_claimed_as_open_permission(
    status, url, allowed
):
    assert (
        auto_license(
            {
                "license": {
                    "status": status,
                    "url": url,
                    "evidence": "Licensed under the stated terms.",
                }
            }
        )
        is allowed
    )


def test_cc_link_without_permission_declaration_is_not_automatic_permission():
    assert not auto_license(
        {
            "license": {
                "status": "declared",
                "url": "https://creativecommons.org/licenses/by/4.0/",
                "evidence": "References: https://creativecommons.org/licenses/by/4.0/",
            }
        }
    )


@pytest.mark.parametrize("fault", ["page", "marker", "position", "verdict"])
def test_invalid_binding_or_incomplete_verdict_never_passes(tmp_path, research_network, fault):
    _, _, factory = research_network

    class Invalid(FakeModel):
        def section_with_evidence(self, state, context, pool):
            draft = super().section_with_evidence(state, context, pool)
            if fault == "page":
                draft.citations[0].page = 999
            elif fault == "marker":
                draft.content = draft.content.replace("[[cite:", "[[cite:unknown-", 1)
            elif fault == "position":
                draft.figures[0].explanation = "正文并未讨论这张图的意义。"
            return draft

        def verify_section(self, state, context, draft, pool):
            verdict = super().verify_section(state, context, draft, pool)
            if fault == "verdict":
                verdict.citations = []
            return verdict

    run = automatic_state()
    pipeline = AutomaticWriting(tmp_path / "agent.sqlite", Invalid(), fetcher_factory=factory)
    with pytest.raises(EvidenceFailure, match="连续 3 次"):
        pipeline.write("auto", run, {}, 1)
    assert pipeline.store.summary("auto", run)["confirmed_sources"] == 0
    assert pipeline.store.summary("auto", run)["confirmed_images"] == 0


def test_changed_pdf_invalidates_cached_identity_and_downloads_new_version(
    tmp_path, research_network
):
    _, _, factory = research_network
    run = automatic_state()
    pipeline = AutomaticWriting(tmp_path / "agent.sqlite", FakeModel(), fetcher_factory=factory)
    _, bundle = pipeline.write("auto", run, {}, 1)
    source_id = bundle["citation_ids"][0]
    old = next(
        item
        for item in pipeline.store.entries("auto", context_hash(run), "source")
        if item["id"] == source_id
    )

    class Changed:
        def json(self, *args):
            return factory().json(*args)

        def get(self, url, **kwargs):
            if url == old["pdf_url"]:
                index = int(url.split("paper")[-1].split(".")[0])
                return url, "application/pdf", paper_pdf(index) + b"\n% changed document version\n"
            return factory().get(url, **kwargs)

    pipeline.fetcher_factory = Changed
    with pytest.raises(EvidenceFailure, match="已变化"):
        pipeline.check_links("auto", run, [source_id])
    invalid = next(
        item
        for item in pipeline.store.entries("auto", context_hash(run), "source")
        if item["id"] == source_id
    )
    assert not invalid["verified"]
    assert not invalid["pdf"]
    research.ResearchCollector(pipeline.store, fetcher=Changed()).inspect_source(
        "auto", context_hash(run), invalid
    )
    fresh = next(
        item
        for item in pipeline.store.entries("auto", context_hash(run), "source")
        if item["id"] == source_id
    )
    assert fresh["verified"]
    assert fresh["pdf"]["sha256"] != old["pdf"]["sha256"]
