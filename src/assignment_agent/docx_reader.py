"""按 OOXML 文档顺序读取正文、表格和附属文本，保留段落位置。"""

import re
from io import BytesIO
from zipfile import BadZipFile, ZipFile

from docx import Document
from lxml import etree

from assignment_agent.schemas import SourceUnit

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def read_docx(raw: bytes) -> list[SourceUnit]:
    try:
        with ZipFile(BytesIO(raw)) as archive:
            if sum(item.file_size for item in archive.infolist()) > 40 * 1024 * 1024:
                raise ValueError("DOCX 解压内容超过 40 MiB 限制")
            # 先校验为真正的 Word 文档，避免把任意 ZIP 当成 DOCX。
            Document(BytesIO(raw))
            names = ["word/document.xml"] + sorted(
                name
                for name in archive.namelist()
                if re.fullmatch(
                    r"word/(?:header\d+|footer\d+|footnotes|endnotes|comments)\.xml", name
                )
            )
            units = []
            for name in names:
                root = etree.fromstring(
                    archive.read(name), etree.XMLParser(resolve_entities=False, no_network=True)
                )
                for index, paragraph in enumerate(root.iter(W + "p"), 1):
                    fragments = []
                    for node in paragraph.iter():
                        # 文本框含嵌套段落，各自提取，避免同一文本被父段落重复记录。
                        parent = next(node.iterancestors(W + "p"), None)
                        if parent is not paragraph:
                            continue
                        if node.tag == W + "t":
                            fragments.append(node.text or "")
                        elif node.tag in {W + "br", W + "cr"}:
                            fragments.append("\n")
                        elif node.tag == W + "tab":
                            fragments.append("\t")
                    text = "".join(fragments)
                    if text.strip():
                        units.append(SourceUnit(location=f"{name}:paragraph:{index}", text=text))
            return units
    except (BadZipFile, KeyError, etree.XMLSyntaxError) as exc:
        raise ValueError(f"DOCX 解析失败，文件可能损坏或加密：{exc}") from exc
