"""将确认后的正文转换为可编辑 Word 文档，不含规划和运行信息。"""

import re
from io import BytesIO

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from markdown_it import MarkdownIt
from PIL import Image

from assignment_agent.schemas import RunState


def inline(paragraph, tokens, images=None):
    bold = False
    italic = False
    link = None
    for token in tokens or []:
        if token.type in {"strong_open", "strong_close"}:
            bold = token.type.endswith("open")
        elif token.type in {"em_open", "em_close"}:
            italic = token.type.endswith("open")
        elif token.type == "link_open":
            link = token.attrGet("href")
        elif token.type == "link_close":
            if link:
                paragraph.add_run(f" ({link})")
            link = None
        elif token.type in {"softbreak", "hardbreak"}:
            paragraph.add_run(" " if token.type == "softbreak" else "\n")
        elif token.type == "image" and images is not None:
            target = re.fullmatch(
                r"/api/runs/[^/]+/research/assets/(fig-[0-9a-f]{24})", token.attrGet("src") or ""
            )
            if not target or target[1] not in images:
                raise ValueError("正文图片不在已核验来源库中，不能导出为链接或占位符")
            # 仅嵌入服务层验证过的本地文件，导出时不会访问远程图片网址。
            with Image.open(images[target[1]]) as image:
                width = min(15.5, 18 * image.width / image.height)
            picture = paragraph.add_run().add_picture(str(images[target[1]]), width=Cm(width))
            picture._inline.docPr.set("descr", token.content)
            paragraph.paragraph_format.keep_with_next = True
        elif token.type in {"text", "code_inline", "image"}:
            run = paragraph.add_run(token.content)
            run.bold = bold
            run.italic = italic
            if token.type == "code_inline":
                run.font.name = "Consolas"
                run.font.size = Pt(10)
            if token.type == "image" and token.attrGet("src"):
                paragraph.add_run(f" ({token.attrGet('src')})")


def append_markdown(document, text, section_title, images=None):
    tokens = MarkdownIt("commonmark", {"html": False}).enable("table").parse(text)
    index = 0
    lists = []
    prefix = ""
    # 正文常以同名标题开头；仅移除这个重复标题，保留不同名称的真实小节。
    if len(tokens) >= 3 and tokens[0].type == "heading_open":

        def normalized(value):
            return re.sub(r"^\d+(?:\.\d+)*[.、)\s]+", "", value.strip()).casefold()

        if normalized(tokens[1].content) == normalized(section_title):
            index = 3
    while index < len(tokens):
        token = tokens[index]
        if token.type == "table_open":
            rows = []
            current_row = None
            while index < len(tokens) and tokens[index].type != "table_close":
                item = tokens[index]
                if item.type == "tr_open":
                    current_row = []
                    rows.append(current_row)
                elif item.type == "inline" and current_row is not None:
                    current_row.append(item.children)
                index += 1
            if rows:
                table = document.add_table(rows=0, cols=max(len(row) for row in rows))
                table.style = "Table Grid"
                for row_index, row in enumerate(rows):
                    cells = table.add_row().cells
                    for column, children in enumerate(row):
                        paragraph = cells[column].paragraphs[0]
                        # 表格沿用正文段距会在需求矩阵中累积大量空白，影响作业页数上限。
                        paragraph.paragraph_format.space_before = Pt(0)
                        paragraph.paragraph_format.space_after = Pt(0)
                        paragraph.paragraph_format.line_spacing = 1.0
                        inline(paragraph, children, images)
                        for run in paragraph.runs:
                            run.font.size = Pt(10)
                            if row_index == 0:
                                run.bold = True
                        if row_index == 0:
                            shade = OxmlElement("w:shd")
                            shade.set(qn("w:fill"), "EEF2F4")
                            cells[column]._tc.get_or_add_tcPr().append(shade)
                    if row_index == 0:
                        header = OxmlElement("w:tblHeader")
                        table.rows[0]._tr.get_or_add_trPr().append(header)
        elif token.type == "heading_open":
            level = min(int(token.tag[1:]) + 1, 4)
            inline(document.add_heading(level=level), tokens[index + 1].children, images)
            index += 2
        elif token.type in {"bullet_list_open", "ordered_list_open"}:
            lists.append(
                {
                    "ordered": token.type == "ordered_list_open",
                    "next": int(token.attrGet("start") or 1),
                }
            )
        elif token.type in {"bullet_list_close", "ordered_list_close"}:
            lists.pop()
        elif token.type == "list_item_open":
            current = lists[-1]
            prefix = f"{current['next']}. " if current["ordered"] else "• "
            current["next"] += 1
        elif token.type == "paragraph_open":
            paragraph = document.add_paragraph()
            if lists:
                paragraph.paragraph_format.left_indent = Cm(0.6 * len(lists))
            paragraph.add_run(prefix)
            prefix = ""
            inline(paragraph, tokens[index + 1].children, images)
            if re.match(r"^(?:Figure \d+\.|Source:)", tokens[index + 1].content):
                paragraph.style = "Caption"
                # 图、图注与完整出处保持连续，避免长网址或许可提示被拆到下一页。
                paragraph.paragraph_format.keep_together = True
                paragraph.paragraph_format.keep_with_next = not tokens[
                    index + 1
                ].content.startswith("Source:")
            index += 2
        elif token.type in {"fence", "code_block"}:
            paragraph = document.add_paragraph()
            paragraph.paragraph_format.line_spacing = 1
            run = paragraph.add_run(token.content.rstrip("\n"))
            run.font.name = "Consolas"
            run.font.size = Pt(9)
        elif token.type == "hr":
            document.add_paragraph()
        index += 1


def build_docx(run: RunState, *, images=None) -> bytes:
    if run.automatic_research and images is None:
        raise ValueError("自动写作导出必须提供已核验的本地插图，不能退化为图片链接")
    document = Document()
    section = document.sections[0]
    section.page_height = Cm(29.7)
    section.page_width = Cm(21)
    section.top_margin = section.bottom_margin = Cm(2)
    section.left_margin = section.right_margin = Cm(2)
    for name in ("Normal", "Title", "Heading 1", "Heading 2", "Heading 3", "Heading 4", "Caption"):
        style = document.styles[name]
        style.font.name = "Times New Roman"
        style.font.color.rgb = RGBColor(0, 0, 0)
        style.element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "宋体")
        if name == "Title":
            # 清除默认模板的标题底线，避免导出时出现额外的主题色装饰。
            for border in style.element.xpath("./w:pPr/w:pBdr"):
                border.getparent().remove(border)
    normal = document.styles["Normal"]
    normal.font.size = Pt(12)
    normal.paragraph_format.line_spacing = 1.15
    normal.paragraph_format.space_after = Pt(2)
    document.styles["Title"].font.size = Pt(20)
    document.styles["Caption"].font.size = Pt(10)
    document.styles["Caption"].paragraph_format.line_spacing = 1.15
    for level, size in ((1, 15), (2, 13), (3, 12), (4, 12)):
        document.styles[f"Heading {level}"].font.size = Pt(size)
        document.styles[f"Heading {level}"].paragraph_format.keep_with_next = True
        document.styles[f"Heading {level}"].paragraph_format.space_before = Pt(
            8 if level == 1 else 5
        )
        document.styles[f"Heading {level}"].paragraph_format.space_after = Pt(3)
    document.core_properties.title = run.spec.title
    document.core_properties.author = ""
    document.core_properties.comments = ""
    document.add_paragraph(run.spec.title, "Title")
    for title, draft in zip(run.spec.sections, run.drafts, strict=True):
        document.add_heading(title, level=1)
        append_markdown(document, draft.content, title, images)
    footer = section.footer.paragraphs[0]
    footer.alignment = 1
    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), "PAGE")
    footer._p.append(field)
    output = BytesIO()
    document.save(output)
    return output.getvalue()
