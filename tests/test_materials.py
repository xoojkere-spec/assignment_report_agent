from io import BytesIO

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from assignment_agent.materials import read_material


def make_pdf(path, text=None, encrypted=False):
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    if text:
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 20 200 Td ({text}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    if encrypted:
        writer.encrypt("password")
    buffer = BytesIO()
    writer.write(buffer)
    path.write_bytes(buffer.getvalue())


def test_text_locations_and_fingerprint(tmp_path):
    path = tmp_path / "brief.md"
    path.write_text("\ufeff题目：测试\n\n字数上限：500", encoding="utf-8")
    material = read_material(path, "assignment")
    assert material.units[2].location == "line:3"
    assert material.units[0].text == "题目：测试"
    assert read_material(path, "assignment") == material
    path.write_text("已更新", encoding="utf-8")
    assert read_material(path, "assignment").sha256 != material.sha256


def test_real_pdf_text_and_page_location(tmp_path):
    path = tmp_path / "brief.pdf"
    make_pdf(path, "Title: Demo")
    material = read_material(path, "assignment")
    assert material.units[0].location == "page:1"
    assert "Title: Demo" in material.units[0].text


def test_rubric_pdf_uses_layout_text_when_it_preserves_content(monkeypatch, tmp_path):
    path = tmp_path / "rubric.pdf"
    make_pdf(path, "Weight")
    modes = []

    def extract_text(page, *args, **kwargs):
        mode = kwargs.get("extraction_mode", "plain")
        modes.append(mode)
        return "Weight (%)  Excellent" if mode == "layout" else "Weig\nht (%) Excelle\nnt"

    monkeypatch.setattr("pypdf._page.PageObject.extract_text", extract_text)
    material = read_material(path, "rubric")
    assert material.units[0].text == "Weight (%)  Excellent"
    assert modes == ["plain", "layout"]


@pytest.mark.parametrize(
    "kind", ["blank_pdf", "encrypted", "broken_pdf", "empty", "encoding", "broken_docx", "doc"]
)
def test_unsupported_materials_fail_explicitly(tmp_path, kind):
    path = tmp_path / ("input.pdf" if "pdf" in kind or kind == "encrypted" else "input.txt")
    if kind in {"blank_pdf", "encrypted"}:
        make_pdf(path, encrypted=kind == "encrypted")
    elif kind == "broken_pdf":
        path.write_bytes(b"not a pdf")
    elif kind == "encoding":
        path.write_bytes(b"\xff\xfe")
    elif kind == "broken_docx":
        path = tmp_path / "input.docx"
        path.write_bytes(b"unsupported")
    elif kind == "doc":
        path = tmp_path / "input.doc"
        path.write_bytes(b"unsupported")
    else:
        path.write_text(" ", encoding="utf-8")
    with pytest.raises(ValueError):
        read_material(path, "assignment")
