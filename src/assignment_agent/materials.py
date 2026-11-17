"""完整提取本地文本材料，保留文件指纹和行号或 PDF 页码。"""

from hashlib import sha256
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from assignment_agent.docx_reader import read_docx
from assignment_agent.ocr import recognize_pdf_page
from assignment_agent.schemas import Material, SourceUnit

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TEXT_CHARS = 200_000
MAX_PAGES = 200


def read_material(path: str | Path, role: str) -> Material:
    path = Path(path).resolve(strict=True)
    if path.suffix.lower() not in {".txt", ".md", ".pdf", ".docx"}:
        raise ValueError(f"不支持的材料格式：{path.suffix}")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"材料超过 20 MiB 限制：{path}")
    raw = path.read_bytes()
    units = []
    if path.suffix.lower() == ".docx":
        units = read_docx(raw)
    elif path.suffix.lower() == ".pdf":
        try:
            reader = PdfReader(BytesIO(raw))
            if reader.is_encrypted:
                raise ValueError("暂不支持加密 PDF")
            if not 0 < len(reader.pages) <= MAX_PAGES:
                raise ValueError("PDF 页数必须在 1–200 之间")
            for index, page in enumerate(reader.pages, 1):
                text = page.extract_text() or ""
                if role == "rubric" and text.strip():
                    # 表格按布局提取时保留列关系；若丢失较多字符则保留原提取结果。
                    layout = page.extract_text(extraction_mode="layout") or ""
                    if len("".join(layout.split())) >= len("".join(text.split())) * 0.9:
                        text = layout
                if text.strip():
                    units.append(SourceUnit(location=f"page:{index}", text=text))
                # 图文混排也检查图像区域，避免仅因存在页码或少量文本层便漏掉扫描内容。
                if not text.strip() or len(page.images):
                    recognized = recognize_pdf_page(raw, index - 1)
                    existing = "".join(text.split())
                    units.extend(
                        unit for unit in recognized if "".join(unit.text.split()) not in existing
                    )
                    if not text.strip() and not recognized:
                        raise ValueError(f"PDF 第 {index} 页未能识别文字，请检查是否空白或扫描不清")
        except PyPdfError as exc:
            raise ValueError(f"PDF 解析失败：{path.name}：{exc}") from exc
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError(f"文本材料必须使用 UTF-8 编码：{path}") from exc
        units = [
            SourceUnit(location=f"line:{index}", text=line)
            for index, line in enumerate(text.splitlines(), 1)
        ]
    if not any(unit.text.strip() for unit in units):
        raise ValueError(f"材料为空：{path}")
    if sum(len(unit.text) for unit in units) > MAX_TEXT_CHARS:
        raise ValueError("材料提取文本超过 200000 字符，未截断，请拆分后重试")
    return Material(
        source_id="src-" + sha256(str(path).encode() + raw).hexdigest()[:16],
        path=str(path),
        role=role,
        sha256=sha256(raw).hexdigest(),
        units=units,
    )
