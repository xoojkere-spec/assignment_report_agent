import hashlib
import json
import sqlite3

import httpx
import pytest
from conftest import legacy_agent as open_agent
from pydantic import ValidationError
from test_delivery import completed
from test_model_adapter import response, settings
from test_workflow import decision

from assignment_agent.delivery import content_hash
from assignment_agent.materials import read_material
from assignment_agent.model_adapter import OpenAICompatibleModel
from assignment_agent.schemas import (
    Analysis,
    AssignmentSpec,
    Clarification,
    RequirementFact,
    RunState,
    required_web_images,
)


def test_default_evidence_targets_and_explicit_higher_targets():
    spec = AssignmentSpec(title="默认目标", sections=["分析"], word_limit=800)
    assert (spec.min_sources, spec.min_web_images) == (3, 2)
    higher = AssignmentSpec(**{**spec.model_dump(), "min_sources": 5, "min_web_images": 4})
    assert (higher.min_sources, higher.min_web_images) == (5, 4)
    assert AssignmentSpec.model_validate_json(higher.model_dump_json()) == higher


def test_model_assessed_image_count_is_optional_without_material_minimum():
    spec = AssignmentSpec(title="图片目标", sections=["分析"], word_limit=800)
    clarification = Clarification(
        interrupt_id="images",
        spec=spec,
        note="有相关图就使用",
        assessment={"min_web_images": "模型建议两张图片"},
    )
    run = RunState(spec=spec, analysis=Analysis(), clarifications=[clarification])
    assert required_web_images(run) == 0
    run.analysis.facts.append(
        RequirementFact(
            field="min_web_images",
            value="1",
            source_id="brief",
            location="page:1",
            quote="至少一张图片",
        )
    )
    assert required_web_images(run) == 1
    assert required_web_images(RunState(spec=spec)) == 2


@pytest.mark.parametrize(
    "values",
    [
        {"min_sources": 2},
        {"min_web_images": -1},
        {"min_sources": True},
        {"min_web_images": 2.5},
        {"min_sources": "3"},
    ],
)
def test_invalid_evidence_targets_are_rejected(values):
    with pytest.raises(ValidationError):
        AssignmentSpec(title="非法目标", sections=["分析"], word_limit=800, **values)


def test_real_extraction_preserves_details_when_spec_is_cleared(tmp_path):
    brief = tmp_path / "brief.txt"
    brief.write_text("题目：证据要求\n引用格式：Harvard\n需要 4 张网络架构图", encoding="utf-8")
    material = read_material(brief, "assignment")
    data = {
        "issues": ["字数上限待确认"],
        "spec": {
            "title": "证据要求",
            "sections": ["分析"],
            "word_limit": 800,
            "citation_style": "Harvard",
            "min_sources": 5,
            "min_web_images": 4,
            "requirements": ["每张架构图需要图注和来源"],
        },
    }
    model = OpenAICompatibleModel(
        settings(), transport=httpx.MockTransport(lambda request: response(json.dumps(data)))
    )
    analysis = model.extract(RunState(materials=[material]), {})
    assert analysis.spec is None
    assert analysis.requirement_details.citation_style == "Harvard"
    assert analysis.requirement_details.min_sources == 5
    assert analysis.requirement_details.min_web_images == 4
    assert analysis.requirement_details.requirements == ["每张架构图需要图注和来源"]
    missing = Analysis.model_validate(
        {**data, "spec": None, "requirement_details": analysis.requirement_details.model_dump()}
    )
    assert missing.requirement_details == analysis.requirement_details


def test_clarify_preserves_unsent_details_across_restart_and_replan(tmp_path):
    brief = tmp_path / "brief.txt"
    brief.write_text(
        "题目：保存引用要求\n部分：分析；结论\n引用格式：Harvard\n语言：英文\n"
        "作业类型：比较报告\n字数口径：不含参考文献\n独立引用来源最少：5\n"
        "网络图片最少：4\n其他要求：图片需注明来源和图注",
        encoding="utf-8",
    )
    db = tmp_path / "agent.sqlite"
    with open_agent(db) as agent:
        pending = agent.start_materials(brief, run_id="details")
        assert pending["state"]["spec"] is None
        details = pending["state"]["analysis"]["requirement_details"]
        assert details["citation_style"] == "Harvard"
        assert details["min_sources"] == 5 and details["min_web_images"] == 4
    with open_agent(db) as agent:
        assert agent.recover("details") == pending
        result = agent.clarify(
            "details",
            Clarification(
                interrupt_id=pending["interrupt_id"],
                spec=AssignmentSpec(
                    title="保存引用要求", sections=["分析", "结论"], word_limit=800
                ),
                note="补充字数上限，其他要求沿用材料",
            ),
        )
        spec = result["state"]["spec"]
        assert {name: spec[name] for name in details} == details
        assert result["state"]["clarifications"][-1]["spec"] == spec
        assert result["state"]["drafts"] == []
        replanned = agent.replan("details", "details-replan")
        assert {name: replanned["state"]["spec"][name] for name in details} == details
        result = agent.decide("details", decision(result))
        assert result["state"]["spec"] == spec
        assert result["state"]["pending"]["artifact_id"] == "section-1"
    with open_agent(db) as agent:
        assert agent.recover("details") == result


def test_explicit_clarification_updates_details(tmp_path):
    brief = tmp_path / "brief.txt"
    brief.write_text(
        "题目：明确修改\n部分：分析\n引用格式：Harvard\n其他要求：需比较方法", encoding="utf-8"
    )
    with open_agent(tmp_path / "agent.sqlite") as agent:
        result = agent.start_materials(brief, run_id="change")
        result = agent.clarify(
            "change",
            Clarification(
                interrupt_id=result["interrupt_id"],
                spec=AssignmentSpec(
                    title="明确修改",
                    sections=["分析"],
                    word_limit=800,
                    citation_style="APA 7",
                    min_sources=6,
                    min_web_images=3,
                    requirements=["每张网络图片需要来源"],
                ),
                note="按教师补充说明更新要求",
            ),
        )
        spec = result["state"]["spec"]
        assert (spec["citation_style"], spec["min_sources"], spec["min_web_images"]) == (
            "APA 7",
            6,
            3,
        )
        assert spec["requirements"] == ["每张网络图片需要来源"]


def test_legacy_confirmation_hash_still_allows_export(tmp_path):
    db = tmp_path / "agent.sqlite"
    with open_agent(db) as agent:
        result = completed(agent)
        run = RunState.model_validate(result["state"])
        legacy_spec = run.spec.model_dump(exclude={"min_sources", "min_web_images"})
        legacy_digest = hashlib.sha256(
            json.dumps(
                {
                    "spec": legacy_spec,
                    "drafts": [draft.model_dump() for draft in run.drafts],
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()
        assert content_hash(run) == legacy_digest
        raw = run.model_dump(mode="json")
        raw["spec"] = legacy_spec
        agent.graph.update_state(agent._config("export"), {"run": raw})
        with sqlite3.connect(db) as connection:
            connection.execute(
                "INSERT INTO completion_records VALUES (?, ?, ?)",
                ("export", legacy_digest, "2026-10-30T00:00:00+00:00"),
            )
        run.spec.min_sources = 5
        assert content_hash(run) != legacy_digest
    with open_agent(db) as agent:
        restored = agent.recover("export")
        assert restored["delivery"]["confirmed"] is True
        assert restored["state"]["spec"]["min_sources"] == 3
        assert agent.export_docx("export")[0].startswith(b"PK")
        raw = agent.graph.get_state(agent._config("export")).values["run"]
        assert "min_sources" not in raw["spec"]


def test_model_receives_targets_without_claiming_search():
    captured = []
    model = OpenAICompatibleModel(
        settings(),
        transport=httpx.MockTransport(
            lambda request: captured.append(json.loads(request.content)) or response("规划草稿")
        ),
    )
    model.outline(
        RunState(
            spec=AssignmentSpec(
                title="模型目标",
                sections=["分析"],
                word_limit=800,
                min_sources=5,
                min_web_images=3,
                citation_style="APA 7",
            )
        ),
        {},
    )
    data = json.loads(captured[0]["messages"][1]["content"])
    assert data["spec"]["min_sources"] == 5 and data["spec"]["min_web_images"] == 3
    assert "当前模型请求不能自行联网" in captured[0]["messages"][0]["content"]
