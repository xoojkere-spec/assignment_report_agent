from io import BytesIO

import pytest
from conftest import legacy_agent as open_agent
from docx import Document
from docx.shared import Pt
from test_workflow import decision

from assignment_agent.delivery import content_hash, ready_for_delivery
from assignment_agent.export_docx import build_docx
from assignment_agent.schemas import AssignmentSpec, RunState


def completed(agent):
    result = agent.start(
        AssignmentSpec(title="确认与导出", sections=["分析", "结论"], word_limit=900), "export"
    )
    for _ in range(3):
        result = agent.decide("export", decision(result))
    return result


def test_confirmation_export_and_restart_without_api_key(tmp_path):
    db = tmp_path / "state.sqlite"
    with open_agent(db) as agent:
        result = agent.start(
            AssignmentSpec(title="未完成", sections=["分析"], word_limit=500), "pending"
        )
        with pytest.raises(ValueError, match="尚有未确认"):
            agent.confirm_completion("pending", "0" * 64)
        with pytest.raises(ValueError, match="先审阅全文"):
            agent.export_docx("pending")
        result = completed(agent)
        assert result["delivery"]["ready"] is True
        assert result["delivery"]["confirmed"] is False
        with pytest.raises(ValueError, match="版本已变化"):
            agent.confirm_completion("export", "0" * 64)
        with pytest.raises(ValueError, match="先审阅全文"):
            agent.export_docx("export")
        result = agent.confirm_completion("export", result["delivery"]["content_hash"])
        assert result["delivery"]["confirmed"] is True
        assert agent.confirm_completion("export", result["delivery"]["content_hash"]) == result
    with open_agent(db) as agent:
        assert agent.status("export") == result
        content, title = agent.export_docx("export")
        doc = Document(BytesIO(content))
        assert title == "确认与导出"
        assert [p.text for p in doc.paragraphs if p.style.name == "Heading 1"] == ["分析", "结论"]
        assert not any("推荐章节划分" in p.text for p in doc.paragraphs)


def test_changed_or_unapproved_body_invalidates_delivery(tmp_path):
    with open_agent(tmp_path / "state.sqlite") as agent:
        result = completed(agent)
        state = RunState.model_validate(result["state"])
        digest = content_hash(state)
        state.drafts[0].content += "新增内容"
        assert content_hash(state) != digest
        state.drafts[0].version += 1
        assert ready_for_delivery(state) is False


def test_markdown_document_preserves_structure_and_content(tmp_path):
    with open_agent(tmp_path / "state.sqlite") as agent:
        state = RunState.model_validate(completed(agent)["state"])
    state.drafts[0].content = (
        "# 分析\n\n这是**重要论点**，含 *斜体* 与 `inline code`。\n\n"
        "## 小节\n\n1. 第一条\n2. 第二条\n\n"
        "| 方法 | 特点 |\n| --- | --- |\n| A | 可解释 |\n| B | 灵活 |\n\n"
        "```python\ndef sample():\n    return 42\n```\n\n"
        "参见[课程链接](https://example.com/course)。"
    )
    content = build_docx(state)
    doc = Document(BytesIO(content))
    paragraphs = [p.text for p in doc.paragraphs]
    assert paragraphs.count("分析") == 1
    assert "小节" in paragraphs
    assert "def sample():\n    return 42" in paragraphs
    assert any("https://example.com/course" in text for text in paragraphs)
    assert doc.tables[0].cell(1, 0).text == "A"
    assert any(run.bold and run.text == "重要论点" for p in doc.paragraphs for run in p.runs)
    assert "api_key" not in "\n".join(paragraphs)
    assert doc.sections[0].top_margin.cm == pytest.approx(2, abs=0.001)
    assert doc.sections[0].left_margin.cm == pytest.approx(2, abs=0.001)
    assert doc.styles["Normal"].paragraph_format.line_spacing == 1.15
    assert doc.styles["Normal"].paragraph_format.space_after == Pt(2)
    for row in doc.tables[0].rows:
        for cell in row.cells:
            assert cell.paragraphs[0].paragraph_format.line_spacing == 1
            assert cell.paragraphs[0].paragraph_format.space_after == Pt(0)
    assert any(text.strip() for text in doc.tables[0]._element.getnext().xpath(".//w:t/text()"))
