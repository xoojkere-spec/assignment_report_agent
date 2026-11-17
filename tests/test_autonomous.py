import json
import time
from io import BytesIO

import pytest
from conftest import legacy_agent
from docx import Document
from fastapi.testclient import TestClient
from test_grounded_writing import AutomaticFake, automatic_state
from test_research import research_network as research_network

from assignment_agent import research
from assignment_agent.evidence_types import EvidenceDraft, EvidenceGap
from assignment_agent.fake_model import FakeModel
from assignment_agent.grounded_writing import EvidenceFailure, bibliography
from assignment_agent.model_adapter import ModelError
from assignment_agent.research_budget import SearchBudget
from assignment_agent.research_store import ResearchStore
from assignment_agent.schemas import AssignmentSpec, ModelProfile, RequirementReply
from assignment_agent.service import open_agent
from assignment_agent.web import create_app


def test_default_automatically_writes_audits_records_and_exports(
    tmp_path, research_network, monkeypatch
):
    _, _, factory = research_network
    monkeypatch.setattr(research, "PublicFetcher", factory)
    with open_agent(tmp_path / "agent.sqlite", AutomaticFake()) as agent:
        result = agent.start(automatic_state().spec, "auto")
        assert result["autonomous"]
        assert result["state"]["status"] == "completed"
        assert result["interrupt_id"] is None and result["state"]["pending"] is None
        assert result["research"]["used_sources"] == 3
        assert result["research"]["used_images"] == 2
        assert result["delivery"]["confirmed"]
        assert len(result["state"]["approvals"]) == 3
        assert all(a["interrupt_id"].startswith("auto:") for a in result["state"]["approvals"])
        raw, _ = agent.export_docx("auto")
        doc = Document(BytesIO(raw))
        assert len(doc.inline_shapes) == 2
        assert any(p.text == "References" for p in doc.paragraphs)
        assert agent.recover("auto")["delivery"] == result["delivery"]


def test_table_of_contents_uses_no_sources_or_search_and_keeps_global_minimum(
    tmp_path, research_network, monkeypatch
):
    _, _, factory = research_network
    monkeypatch.setattr(research, "PublicFetcher", factory)
    db = tmp_path / "toc.sqlite"
    spec = AssignmentSpec(
        title="Kernel architecture",
        sections=["Table of Contents", "Analysis", "Conclusion"],
        word_limit=900,
        min_web_images=0,
    )
    with open_agent(db, AutomaticFake()) as agent:
        result = agent.start(spec, "toc")
        assert result["state"]["status"] == "completed"
        assert result["state"]["drafts"][0]["content"] == "1. Analysis\n2. Conclusion"
        bundle = result["state"]["grounded_drafts"]["section-1"]
        assert bundle["citation_ids"] == [] and bundle["figure_ids"] == []
        assert result["research"]["used_sources"] >= 3
    assert SearchBudget(ResearchStore(db), "toc", 1).usage()["chapter_used"] == 0


def test_explicit_references_before_appendices_are_not_duplicated(
    tmp_path, research_network, monkeypatch
):
    _, _, factory = research_network
    monkeypatch.setattr(research, "PublicFetcher", factory)
    spec = AssignmentSpec(
        title="SRS",
        sections=["Analysis", "References", "Appendices"],
        citation_style="APA",
        word_limit=900,
        min_web_images=0,
    )
    with open_agent(tmp_path / "explicit-references.sqlite", AutomaticFake()) as agent:
        result = agent.start(spec, "explicit-references")
        assert result["state"]["status"] == "completed"
        references = result["state"]["drafts"][1]["content"]
        appendix = result["state"]["drafts"][2]["content"]
        assert "*Journal of Software Architecture, 10*(2), 41–55." in references
        assert "https://doi.org/10.1234/paper0" in references
        assert "## References" not in appendix
        raw, _ = agent.export_docx("explicit-references")
    assert [p.text for p in Document(BytesIO(raw)).paragraphs].count("References") == 1


def test_apa_bibliography_uses_verified_journal_metadata():
    spec = AssignmentSpec(title="SRS", sections=["Analysis"], citation_style="APA", word_limit=900)
    source = {
        "id": "src-" + "a" * 24,
        "authors": ["Alice Researcher", "Bob Writer"],
        "year": 2024,
        "title": "A study of software architecture",
        "doi": "10.1234/example",
        "url": "https://doi.org/10.1234/example",
        "paper": {"family_names": ["Researcher", "Writer"]},
        "citation_metadata": {
            "authors": [
                {"given": "Alice", "family": "Researcher"},
                {"given": "Bob", "family": "Writer"},
            ],
            "journal": "Journal of Software Architecture",
            "volume": "10",
            "issue": "2",
            "page": "41-55",
        },
    }
    result = bibliography(spec, [source["id"]], {source["id"]: source})
    assert "Researcher, A., & Writer, B. (2024)." in result
    assert "*Journal of Software Architecture, 10*(2), 41–55." in result
    assert "https://doi.org/10.1234/example" in result
    assert "DOI:" not in result


def test_early_body_can_use_zero_sources_while_final_body_meets_global_minimum(
    tmp_path, research_network, monkeypatch
):
    _, _, factory = research_network
    monkeypatch.setattr(research, "PublicFetcher", factory)

    class NoIntroCitations(AutomaticFake):
        def section_with_evidence(self, state, context, pool):
            if state.current_index == 0:
                return EvidenceDraft(content="本报告将分析作业指定的主题。")
            return super().section_with_evidence(state, context, pool)

    db = tmp_path / "global-sources.sqlite"
    spec = AssignmentSpec(
        title="Kernel architecture",
        sections=["Introduction", "Analysis", "Conclusion"],
        word_limit=900,
        min_web_images=0,
    )
    with open_agent(db, NoIntroCitations()) as agent:
        result = agent.start(spec, "global-sources")
        assert result["state"]["status"] == "completed"
        assert result["state"]["grounded_drafts"]["section-1"]["citation_ids"] == []
        assert result["research"]["used_sources"] == 3
        assert result["research"]["min_sources"] == 3
    assert SearchBudget(ResearchStore(db), "global-sources", 1).usage()["chapter_used"] == 0


def test_model_assessed_image_goal_does_not_block_without_relevant_image(
    tmp_path, research_network, monkeypatch
):
    _, _, factory = research_network
    monkeypatch.setattr(research, "PublicFetcher", factory)

    class OptionalImages(AutomaticFake):
        def assess_requirements(self, state, answer, context):
            assessed = super().assess_requirements(state, answer, context)
            return assessed.model_copy(
                update={"spec": assessed.spec.model_copy(update={"min_web_images": 2})}
            )

    brief = tmp_path / "brief.txt"
    brief.write_text("题目：图片可选作业\n部分：分析", encoding="utf-8")
    with open_agent(tmp_path / "optional-images.sqlite", OptionalImages()) as agent:
        waiting = agent.start_materials(brief, run_id="optional-images")
        result = agent.answer_requirements(
            "optional-images",
            RequirementReply(
                interrupt_id=waiting["interrupt_id"],
                answer="字数无额外要求，引用 APA，有相关图才使用",
            ),
        )
        assert result["state"]["status"] == "completed"
        assert result["state"]["spec"]["min_web_images"] == 2
        assert result["research"]["required_web_images"] == 0
        assert result["research"]["used_images"] == 0
        assert result["research"]["used_sources"] >= 3
        assert result["research"]["ready"]
        assert all(
            not bundle["figure_ids"] for bundle in result["state"]["grounded_drafts"].values()
        )


def test_missing_requirements_wait_for_user_without_confirmation(tmp_path):
    brief = tmp_path / "brief.txt"
    brief.write_text("题目：测试\n部分：分析", encoding="utf-8")
    with open_agent(tmp_path / "agent.sqlite") as agent:
        result = agent.start_materials(brief, run_id="missing")
        assert result["state"]["analysis"]["issues"]
        assert result["interrupt_id"]
        assert result["interrupt_payload"]["kind"] == "requirements"
        assert result["state"]["status"] == "awaiting_user"
        assert result["failed_nodes"] == []
        assert not result["state"]["approvals"]
        assert agent.recover("missing") == result


def test_old_requirement_interrupt_still_waits_for_user(tmp_path):
    brief = tmp_path / "brief.txt"
    brief.write_text("题目：测试\n部分：分析", encoding="utf-8")
    db = tmp_path / "agent.sqlite"
    with legacy_agent(db) as agent:
        assert agent.start_materials(brief, run_id="old-missing")["interrupt_id"]
    with open_agent(db) as agent:
        result = agent.recover("old-missing")
        assert result["interrupt_payload"]["kind"] == "requirements"
        assert result["failed_nodes"] == []
        assert result["state"]["spec"] is None and not result["state"]["approvals"]


def test_page_limited_assignment_waits_then_assesses_unspecified_requirements(tmp_path):
    brief = tmp_path / "brief.txt"
    brief.write_text("题目：页数作业\n部分：分析\n页数上限：30", encoding="utf-8")
    with open_agent(tmp_path / "agent.sqlite") as agent:
        waiting = agent.start_materials(brief, run_id="page-limited")
        assert waiting["state"]["status"] == "awaiting_user"
        assert waiting["state"]["analysis"]["requirement_details"]["page_limit"] == 30
        assert agent.recover("page-limited") == waiting
        completed = agent.answer_requirements(
            "page-limited",
            RequirementReply(interrupt_id=waiting["interrupt_id"], answer="没有额外要求"),
        )
        spec = completed["state"]["spec"]
        assert spec["page_limit"] == 30
        assert spec["word_limit"] == 7500
        assert spec["min_sources"] == 3 and spec["min_web_images"] == 0
        assert completed["state"]["clarifications"][-1]["note"] == "没有额外要求"
        assert completed["state"]["clarifications"][-1]["assessment"]["word_limit"]


@pytest.mark.parametrize("model_rule", [None, "封面、参考文献和附录不计入页数"])
def test_assessment_preserves_material_page_count_rules(tmp_path, model_rule):
    class RewordedPageRule(FakeModel):
        def assess_requirements(self, state, answer, context):
            result = super().assess_requirements(state, answer, context)
            return result.model_copy(
                update={"spec": result.spec.model_copy(update={"page_count_rules": model_rule})}
            )

    rule = "30 页上限不含附录、参考文献及封面；这是页数要求，不是字数要求"
    brief = tmp_path / "brief.txt"
    brief.write_text(
        f"题目：页数作业\n部分：分析\n页数上限：30\n页数口径：{rule}",
        encoding="utf-8",
    )
    with open_agent(tmp_path / "agent.sqlite", RewordedPageRule()) as agent:
        waiting = agent.start_materials(brief)
        assert waiting["state"]["analysis"]["requirement_details"]["page_count_rules"] == rule
        completed = agent.answer_requirements(
            waiting["run_id"],
            RequirementReply(
                interrupt_id=waiting["interrupt_id"],
                answer="字数无额外要求，引用采取 APA 格式，图片由模型自行评估",
            ),
        )
        assert completed["state"]["status"] == "completed"
        assert completed["state"]["spec"]["page_count_rules"] == rule
        assert completed["state"]["clarifications"][-1]["spec"]["page_count_rules"] == rule
        assert completed["state"]["spec"]["page_limit"] == 30


def test_model_assessment_cannot_change_pdf_page_limit(tmp_path):
    class WrongPage(FakeModel):
        def assess_requirements(self, state, answer, context):
            result = super().assess_requirements(state, answer, context)
            return result.model_copy(
                update={"spec": result.spec.model_copy(update={"page_limit": 31})}
            )

    brief = tmp_path / "brief.txt"
    brief.write_text("题目：页数作业\n部分：分析\n页数上限：30", encoding="utf-8")
    with open_agent(tmp_path / "agent.sqlite", WrongPage()) as agent:
        waiting = agent.start_materials(brief)
        with pytest.raises(ValueError, match="页数上限"):
            agent.answer_requirements(
                waiting["run_id"],
                RequirementReply(interrupt_id=waiting["interrupt_id"], answer="没有额外要求"),
            )
        assert agent.status(waiting["run_id"]) == waiting


def test_failed_evidence_stops_and_resumes_automatically(tmp_path, research_network, monkeypatch):
    _, _, factory = research_network
    monkeypatch.setattr(research, "PublicFetcher", factory)

    class Unsupported(AutomaticFake):
        def verify_section(self, state, context, draft, pool):
            verdict = super().verify_section(state, context, draft, pool)
            verdict.citations[0].supported = False
            return verdict

    db = tmp_path / "agent.sqlite"
    with open_agent(db, Unsupported()) as agent:
        with pytest.raises(EvidenceFailure, match="连续 3 次"):
            agent.start(automatic_state().spec, "retry")
        result = agent.status("retry")
        assert result["state"]["status"] == "failed"
        assert not result["state"]["drafts"] and len(result["state"]["approvals"]) == 1
    with open_agent(db, AutomaticFake()) as agent:
        result = agent.recover("retry")
        assert result["state"]["status"] == "completed" and result["delivery"]["confirmed"]
        assert result["state"]["outline"]["version"] == 1
        assert len(result["state"]["approvals"]) == 3


def test_recover_with_another_compatible_model_keeps_checkpoint(
    tmp_path, research_network, monkeypatch
):
    _, _, factory = research_network
    monkeypatch.setattr(research, "PublicFetcher", factory)

    class Original(AutomaticFake):
        profile = ModelProfile(
            kind="openai_compatible", base_url="https://old.example/v1", model="old-model"
        )

        def verify_section(self, state, context, draft, pool):
            verdict = super().verify_section(state, context, draft, pool)
            verdict.citations[0].supported = False
            return verdict

    class Replacement(AutomaticFake):
        profile = ModelProfile(
            kind="openai_compatible", base_url="https://new.example/v1", model="new-model"
        )

    db = tmp_path / "model-switch.sqlite"
    with open_agent(db, Original()) as agent:
        with pytest.raises(EvidenceFailure, match="连续 3 次"):
            agent.start(automatic_state().spec, "switch")
        before = agent.status("switch")
        assert before["state"]["model_profile"]["model"] == "old-model"
        assert len(before["state"]["approvals"]) == 1
    with open_agent(db, Replacement()) as agent:
        result = agent.recover("switch")
        assert result["state"]["status"] == "completed"
        assert result["state"]["model_profile"] == Replacement.profile.model_dump()
        assert result["state"]["outline"] == before["state"]["outline"]
        assert result["research"]["used_sources"] >= 3
        assert result["delivery"]["confirmed"]


def test_legacy_interrupt_migrates_without_requesting_approval(
    tmp_path, research_network, monkeypatch
):
    _, _, factory = research_network
    monkeypatch.setattr(research, "PublicFetcher", factory)
    db = tmp_path / "agent.sqlite"
    with legacy_agent(db, AutomaticFake()) as agent:
        old = agent.start(automatic_state().spec, "old")
        assert old["interrupt_id"]
    with open_agent(db, AutomaticFake()) as agent:
        result = agent.recover("old")
        assert result["state"]["status"] == "completed"
        assert result["interrupt_id"] is None and result["delivery"]["confirmed"]
        assert result["state"]["outline"] == old["state"]["outline"]


def test_final_audit_failure_is_bounded_and_recoverable(tmp_path, research_network, monkeypatch):
    _, _, factory = research_network
    monkeypatch.setattr(research, "PublicFetcher", factory)

    class BadFinal(AutomaticFake):
        def verify_section(self, state, context, draft, pool):
            verdict = super().verify_section(state, context, draft, pool)
            if pool.get("verification_scope", {}).get("stage") == "final_reference":
                verdict.citations[0].supported = False
                verdict.citations[0].reason = "最终核验不支持"
            return verdict

    db = tmp_path / "agent.sqlite"
    with open_agent(db, BadFinal()) as agent:
        with pytest.raises(EvidenceFailure, match="最终核验连续三轮"):
            agent.start(automatic_state().spec, "audit")
        result = agent.status("audit")
        assert result["failed_nodes"] == ["finish"] and not result["delivery"]["confirmed"]
        # 界面只返回最近 40 条事件；重试上限应依据完整持久化轨迹，不能漏掉被窗口挤出的记录。
        with ResearchStore(db).connect() as connection:
            events = [
                json.loads(row[0])
                for row in connection.execute(
                    "SELECT data FROM research_events WHERE run_id=? ORDER BY id", ("audit",)
                )
            ]
        failures = [event for event in events if event["code"] == "reference.failed"]
        assert len(failures) == 3
        assert [event["automatic_repair"] for event in failures] == [1, 2, 3]
    with open_agent(db, AutomaticFake()) as agent:
        assert agent.recover("audit")["delivery"]["confirmed"]


def test_reference_repair_resumes_only_affected_chapter_and_keeps_figures(
    tmp_path, research_network, monkeypatch
):
    _, _, factory = research_network
    monkeypatch.setattr(research, "PublicFetcher", factory)
    db = tmp_path / "agent.sqlite"

    def ledger():
        with ResearchStore(db).connect() as connection:
            return connection.execute(
                "SELECT * FROM research_search_attempts WHERE run_id=? ORDER BY id", ("repair",)
            ).fetchall()

    class RecordChapters(AutomaticFake):
        def __init__(self):
            self.written = []
            self.audited = []

        def plan(self, state, context):
            plan = super().plan(state, context)
            plan.parts[2].evidence_needs.append("需要架构图")
            return plan

        def section_with_evidence(self, state, context, pool):
            self.written.append(state.current_index)
            return super().section_with_evidence(state, context, pool)

        def verify_section(self, state, context, draft, pool):
            if pool["verification_scope"]["stage"] == "final_reference":
                self.audited.append(state.current_index)
            verdict = super().verify_section(state, context, draft, pool)
            if (
                pool["verification_scope"]["stage"] == "section"
                and state.current_index in (0, 1)
                and not draft.citations
            ):
                verdict.evidence_gaps = [
                    EvidenceGap(kind="source", detail="该章的论证必须绑定一份可核验文献")
                ]
            return verdict

    class InterruptedRepair(RecordChapters):
        rejected = False

        def verify_section(self, state, context, draft, pool):
            verdict = super().verify_section(state, context, draft, pool)
            if (
                pool["verification_scope"]["stage"] == "final_reference"
                and state.current_index == 1
            ):
                self.rejected = True
                verdict.citations[0].supported = False
                verdict.citations[0].reason = "第二章引用失效"
            return verdict

        def section_with_evidence(self, state, context, pool):
            if self.rejected:
                assert state.current_index == 1
                assert pool["targets"]["new_figures"] == 0
                raise ModelError("测试恢复前的调用中断")
            return super().section_with_evidence(state, context, pool)

    first = InterruptedRepair()
    spec = automatic_state().spec.model_copy(
        update={
            "section_mode": "recommend",
            "sections": ["Introduction", "Selection", "Architecture", "Conclusion"],
        }
    )
    with open_agent(db, first) as agent:
        with pytest.raises(ModelError, match="测试恢复前的调用中断"):
            agent.start(spec, "repair")
        before = agent.status("repair")
    assert first.written == [0, 0, 1, 2, 3] and first.audited == [0, 1]
    assert before["state"]["current_index"] == 1 and before["next_nodes"] == ["section"]
    assert len(before["state"]["drafts"]) == 4 and before["research"]["used_images"] == 2
    spent = ledger()
    assert spent

    second = RecordChapters()
    with open_agent(db, second) as agent:
        after = agent.recover("repair")
        raw, _ = agent.export_docx("repair")
    assert second.written == [1] and second.audited == [1, 2, 3]
    assert after["state"]["drafts"][1]["version"] == before["state"]["drafts"][1]["version"] + 1
    for index in (0, 2, 3):
        key = f"section-{index + 1}"
        old = before["state"]["grounded_drafts"][key]
        new = after["state"]["grounded_drafts"][key]
        assert new["draft"] == old["draft"]
        assert new["figure_ids"] == old["figure_ids"]
    assert after["state"]["drafts"][2] == before["state"]["drafts"][2]
    assert ledger() == spent
    assert {f["sha256"] for f in before["research"]["figures"]} == {
        f["sha256"] for f in after["research"]["figures"]
    }
    document = Document(BytesIO(raw))
    assert after["delivery"]["confirmed"] and len(document.inline_shapes) == 2
    assert any(paragraph.text == "References" for paragraph in document.paragraphs)


def test_default_website_completes_without_decision_or_completion_posts(
    tmp_path, compatible_server
):
    url, requests, _ = compatible_server
    with TestClient(
        create_app(tmp_path / "agent.sqlite", tmp_path / "missing.env", tmp_path / "uploads")
    ) as client:
        headers = {"X-Assignment-Client": "local"}
        assert (
            client.post(
                "/api/config",
                headers=headers,
                json={"base_url": url, "model": "test-model", "api_key": "test-key"},
            ).status_code
            == 200
        )
        job = client.post(
            "/api/runs",
            headers=headers,
            files={
                "assignment": (
                    "brief.txt",
                    (
                        "题目：Kernel\n部分：分析；结论\n字数上限：900\n"
                        "引用格式：APA 7\n网络图片最少：2"
                    ).encode(),
                    "text/plain",
                )
            },
        ).json()
        for _ in range(3000):
            status = client.get("/api/jobs/" + job["job_id"]).json()
            if status["status"] not in {"queued", "running"}:
                break
            time.sleep(0.01)
        assert status["status"] == "completed", status
        result = client.get("/api/runs/" + job["run_id"]).json()
        assert result["autonomous"] and result["delivery"]["confirmed"]
        assert result["interrupt_id"] is None
        exported = client.get("/api/runs/" + job["run_id"] + "/export.docx")
        assert (
            exported.status_code == 200
            and len(Document(BytesIO(exported.content)).inline_shapes) == 2
        )
        assert any('"grounded_section"' in r["body"]["messages"][-1]["content"] for r in requests)
