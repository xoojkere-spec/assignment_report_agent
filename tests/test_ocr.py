from pathlib import Path

from conftest import legacy_agent as open_agent
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader

from assignment_agent.materials import read_material
from assignment_agent.schemas import SourceUnit


def make_scan(path, text="Title: Scan Test\nSections: Analysis\nWord limit: 500"):
    image = Image.new("RGB", (1400, 700), "white")
    ImageDraw.Draw(image).multiline_text(
        (80, 80), text, font=ImageFont.load_default(size=48), fill="black", spacing=40
    )
    image.save(path, "PDF", resolution=150)
    image.close()


def test_real_scanned_pdf_ocr_and_workflow(tmp_path):
    path = tmp_path / "scan.pdf"
    make_scan(path)
    assert not PdfReader(path).pages[0].extract_text().strip()
    material = read_material(path, "assignment")
    assert all(unit.method == "ocr" for unit in material.units)
    assert all(unit.location.startswith("page:1:ocr-line:") for unit in material.units)
    assert "Scan Test" in "\n".join(unit.text for unit in material.units)
    assert all(unit.confidence is not None for unit in material.units)
    with open_agent(tmp_path / "state.sqlite") as agent:
        result = agent.start_materials(path)
        assert any(
            fact["field"] == "word_limit" and fact["value"] == "500"
            for fact in result["state"]["analysis"]["facts"]
        )
        assert result["interrupt_payload"]["kind"] == "requirements"
        assert result["state"]["status"] == "awaiting_user"
        assert agent.recover(result["run_id"]) == result


def test_mixed_page_keeps_text_layer_and_ocr(monkeypatch, tmp_path):
    path = tmp_path / "mixed.pdf"
    make_scan(path)
    # 不依赖字体资源构造文本层：直接替换提取结果，保留真实图像资源。
    monkeypatch.setattr("pypdf._page.PageObject.extract_text", lambda self: "Footer")
    monkeypatch.setattr(
        "assignment_agent.materials.recognize_pdf_page",
        lambda raw, index: [
            SourceUnit(location="page:1:ocr-line:1", text="Footer", method="ocr", confidence=0.9),
            SourceUnit(location="page:1:ocr-line:2", text="扫描正文", method="ocr", confidence=0.9),
        ],
    )
    material = read_material(path, "assignment")
    assert [unit.text for unit in material.units] == ["Footer", "扫描正文"]


def test_mixed_page_drops_ocr_line_already_present_inside_text_layer(monkeypatch, tmp_path):
    path = tmp_path / "mixed.pdf"
    make_scan(path)
    monkeypatch.setattr(
        "pypdf._page.PageObject.extract_text", lambda self: "Requirements for Assessment Title"
    )
    monkeypatch.setattr(
        "assignment_agent.materials.recognize_pdf_page",
        lambda raw, index: [
            SourceUnit(location="page:1:ocr-line:1", text="AssessmentTitle", method="ocr"),
            SourceUnit(location="page:1:ocr-line:2", text="独有图中文字", method="ocr"),
        ],
    )
    material = read_material(path, "assignment")
    assert [unit.text for unit in material.units] == [
        "Requirements for Assessment Title",
        "独有图中文字",
    ]


def test_real_chinese_scan():
    material = read_material(Path(__file__).parent / "fixtures" / "chinese-scan.pdf", "assignment")
    text = "\n".join(unit.text for unit in material.units)
    assert "本科作业" in text
    assert "500" in text
    assert "两种理论" in text
    assert all(unit.method == "ocr" for unit in material.units)
