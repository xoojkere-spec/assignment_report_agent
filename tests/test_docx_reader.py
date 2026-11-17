from conftest import legacy_agent as open_agent
from docx import Document

from assignment_agent.materials import read_material


def test_docx_paragraph_table_header_footer_order(tmp_path):
    doc = Document()
    doc.add_paragraph("题目：Word 测试")
    table = doc.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "表格中的评分标准"
    table.cell(0, 1).text = "分析深度 60%"
    doc.add_paragraph("部分：分析；总结")
    doc.add_paragraph("字数上限：500")
    doc.add_paragraph("引用格式：APA 7")
    doc.add_paragraph("网络图片最少：2")
    doc.sections[0].header.paragraphs[0].text = "课程页眉"
    doc.sections[0].footer.paragraphs[0].text = "课程页脚"
    path = tmp_path / "brief.docx"
    doc.save(path)
    material = read_material(path, "assignment")
    texts = [unit.text for unit in material.units]
    assert texts[:4] == ["题目：Word 测试", "表格中的评分标准", "分析深度 60%", "部分：分析；总结"]
    assert "课程页眉" in texts and "课程页脚" in texts
    assert all(unit.method == "text" for unit in material.units)
    assert material.units[0].location == "word/document.xml:paragraph:1"
    with open_agent(tmp_path / "state.sqlite") as agent:
        result = agent.start_materials(path)
        assert result["state"]["spec"]["word_limit"] == 500
        assert result["state"]["outline"] is not None
