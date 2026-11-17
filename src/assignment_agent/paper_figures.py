"""论文页面定位与裁剪；裁剪按审计项目 crop_figure 的比例换算逻辑重构。"""

import hashlib
import json
import re
from contextlib import closing
from io import BytesIO
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from assignment_agent.research_search import normalized, title_matches
from assignment_agent.research_types import ResearchError

CAPTION = re.compile(
    r"^(?:Figure|Fig\.?|图)\s*(\d+(?:\.\d+)*[a-z]?)[.:：\s\-]+([^\r\n]{3,250})", re.M | re.I
)
EXTRACTOR_VERSION = 2


def save_asset(directory, raw, extension):
    digest = hashlib.sha256(raw).hexdigest()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{digest}.{extension}"
    if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        path.write_bytes(raw)
    return {"sha256": digest, "filename": path.name}


def crop_figure(image, reference_size, box):
    """按参考尺寸缩放裁剪坐标；禁止越界、空白或过小的裁剪区域。"""
    width, height = reference_size
    left, top, right, bottom = box
    if not (0 <= left < right <= width and 0 <= top < bottom <= height):
        raise ResearchError("图片裁剪坐标越界或为空")
    scaled = tuple(
        round(value * scale)
        for value, scale in zip(box, (image.width / width, image.height / height) * 2, strict=True)
    )
    if scaled[2] - scaled[0] < 100 or scaled[3] - scaled[1] < 70:
        raise ResearchError("论文图片分辨率不足，需更合适的图源")
    return image.crop(scaled)


def license_info(pages):
    for number, text in enumerate(pages, 1):
        flat = " ".join(text.split())
        match = re.search(
            r"creativecommons\.org/(licenses/[a-z\-]+/[0-9.]+|publicdomain/zero/[0-9.]+)",
            flat,
            re.I,
        )
        if match:
            code = match[1].rstrip(".")
            return {
                "status": "declared",
                "url": "https://creativecommons.org/" + code + "/",
                "page": number,
                "evidence": flat[max(0, match.start() - 160) : match.end() + 180],
            }
        match = re.search(
            r"(?:Creative Commons|CC)[\s\S]{0,110}?"
            r"(Attribution[\s\-]*ShareAlike|Attribution|BY-SA|BY)[\s\S]{0,30}?(4\.0|3\.0)",
            flat,
            re.I,
        )
        if match:
            kind = "by-sa" if re.search(r"ShareAlike|BY-SA", match[1], re.I) else "by"
            # 存在 NC/ND 条款时保留原文，交人工核对，不推导更宽松的许可。
            quote = flat[max(0, match.start() - 80) : match.end() + 120]
            if re.search(r"NonCommercial|NoDerivatives|BY-NC|BY-ND", quote, re.I):
                return {"status": "needs_review", "url": None, "page": number, "evidence": quote}
            return {
                "status": "declared",
                "url": f"https://creativecommons.org/licenses/{kind}/{match[2]}/",
                "page": number,
                "evidence": quote,
            }
    notices = [
        (i + 1, text)
        for i, text in enumerate(pages)
        if re.search(r"copyright|©|all rights reserved", text, re.I)
    ]
    return {
        "status": "unknown",
        "url": None,
        "page": notices[0][0] if notices else None,
        "evidence": notices[0][1][:350] if notices else "未找到可确定的图片转载许可声明",
    }


def linked_license(raw, texts, fallback):
    """部分论文只将许可版本放在 PDF 超链接中，必须核对同页授权文字。"""
    if fallback["status"] != "unknown":
        return fallback
    try:
        reader = PdfReader(BytesIO(raw))
        for index in range(min(len(reader.pages), len(texts))):
            page = reader.pages[index]
            text = " ".join(texts[index].split())
            grant = re.search(r"licensed\s+under|licenced\s+under|授权|许可", text, re.I)
            if not grant:
                continue
            for annotation in page.get("/Annots", []):
                action = annotation.get_object().get("/A") or {}
                url = str(action.get("/URI", "")).replace("http://", "https://").rstrip("/") + "/"
                if re.fullmatch(
                    r"https://creativecommons\.org/(?:licenses/[a-z-]+/[0-9.]+|publicdomain/zero/[0-9.]+)/",
                    url,
                ):
                    return {
                        "status": "declared",
                        "url": url,
                        "page": index + 1,
                        "evidence": text[max(0, grant.start() - 60) : grant.end() + 220]
                        + " PDF license hyperlink: "
                        + url,
                    }
    except (ValueError, TypeError, KeyError, PdfReadError):
        pass
    return fallback


def figure_bounds(page, caption_top, textpage):
    objects = []
    for obj in page.get_objects(
        filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE, pdfium_c.FPDF_PAGEOBJ_PATH], max_depth=3
    ):
        left, bottom, right, top = obj.get_bounds()
        if (
            right - left >= 4
            and top - bottom >= 4
            and bottom >= caption_top
            and top <= caption_top + 360
        ):
            objects.append((left, bottom, right, top))
        if len(objects) > 2500:
            raise ResearchError("论文图形过于复杂，需人工定位裁剪区域")
    if not objects:
        return None
    selected = [min(objects, key=lambda box: box[1] - caption_top)]
    if selected[0][1] - caption_top > 100:
        return None
    # 仅合并邻近图形对象，不把整页截图或任意正文当作论文插图。
    for _ in range(30):
        left = min(x[0] for x in selected)
        bottom = min(x[1] for x in selected)
        right = max(x[2] for x in selected)
        top = max(x[3] for x in selected)
        added = [
            box
            for box in objects
            if box not in selected
            and box[0] <= right + 22
            and box[2] >= left - 22
            and box[1] <= top + 22
            and box[3] >= bottom - 22
        ]
        if not added:
            break
        selected.extend(added)
    left, bottom = min(x[0] for x in selected), min(x[1] for x in selected)
    right, top = max(x[2] for x in selected), max(x[3] for x in selected)
    label_top = top
    # 矢量图的上方标签可能独立于图形对象；纳入紧邻的短标签，避免截断图中文字。
    for index in range(min(textpage.count_rects(), 2000)):
        text_left, text_bottom, text_right, text_top = textpage.get_rect(index)
        if (
            top - 3 <= text_bottom <= top + 35
            and text_right >= left
            and text_left <= right
            and text_top - text_bottom < 24
        ):
            label = textpage.get_text_bounded(text_left, text_bottom, text_right, text_top).strip()
            if 1 <= len(label) <= 65 and not label.endswith(".") and not CAPTION.match(label):
                label_top = max(label_top, text_top)
    return left, bottom, right, label_top


def inspect_paper(raw, paper, directory):
    if not raw.startswith(b"%PDF-"):
        raise ResearchError("下载内容不是 PDF，不能作为论文图片来源")
    try:
        with pdfium.PdfDocument(raw) as document:
            if len(document) < 1:
                raise ResearchError("论文 PDF 没有可读取的页面")
            texts = []
            total_pages = len(document)
            parsed_pages = min(total_pages, 120)
            for index in range(parsed_pages):
                page = document[index]
                try:
                    with closing(page.get_textpage()) as textpage:
                        texts.append(textpage.get_text_range())
                        if sum(map(len, texts)) > 1_000_000:
                            raise ResearchError("论文文本超过本轮处理限制")
                finally:
                    page.close()
            first = "\n".join(texts[:2])
            checks = {
                "title": title_matches(paper.title, first),
                "author": bool(paper.authors)
                and any(
                    normalized(author).split()[-1] in normalized(first).split()
                    for author in paper.authors
                    if normalized(author)
                ),
                "year": paper.year is not None and str(paper.year) in first,
            }
            if not all(checks.values()):
                raise ResearchError("PDF 标题、作者或年份与检索记录不对应，未采纳该图源")
            pdf = save_asset(directory, raw, "pdf")
            text_asset = save_asset(
                directory,
                json.dumps(
                    [{"page": i + 1, "text": text} for i, text in enumerate(texts)],
                    ensure_ascii=False,
                ).encode(),
                "json",
            )
            license_record = linked_license(raw, texts, license_info(texts))
            figures = []
            warnings = (
                [f"本轮仅检索前 {parsed_pages} 页，共 {total_pages} 页；未对其余页面作核验"]
                if total_pages > parsed_pages
                else []
            )
            for index, text in enumerate(texts):
                if len(figures) >= 8:
                    break
                with closing(document[index]) as page:
                    width, height = page.get_size()
                    if width <= 0 or height <= 0:
                        continue
                    with closing(page.get_textpage()) as textpage:
                        for match in list(CAPTION.finditer(text))[:8]:
                            label = match.group(0).strip()
                            with closing(textpage.search(label[:70])) as search:
                                found = search.get_next()
                            if not found:
                                continue
                            pos = textpage.get_charbox(found[0])
                            bounds = figure_bounds(page, pos[3], textpage)
                            if bounds is None:
                                if len(warnings) < 20:
                                    warnings.append(
                                        f"PDF 第 {index + 1} 页 Figure {match[1]}："
                                        "未找到可靠裁剪区域"
                                    )
                                continue
                            left, bottom, right, top = bounds
                            box = (
                                max(0, left - 8),
                                max(0, height - top - 8),
                                min(width, right + 8),
                                min(
                                    height, height - bottom + min(5, max(0, (bottom - pos[3]) / 2))
                                ),
                            )
                            scale = min(300 / 72, (16_000_000 / (width * height)) ** 0.5)
                            if scale < 1:
                                continue
                            bitmap = page.render(scale=scale)
                            try:
                                try:
                                    with bitmap.to_pil().convert("RGB") as image:
                                        with crop_figure(image, (width, height), box) as crop:
                                            output = BytesIO()
                                            crop.save(output, format="PNG")
                                            asset = save_asset(directory, output.getvalue(), "png")
                                except ResearchError as exc:
                                    if len(warnings) < 20:
                                        warnings.append(
                                            f"PDF 第 {index + 1} 页 Figure {match[1]}：{exc}"
                                        )
                                    continue
                            finally:
                                bitmap.close()
                            own_license = dict(license_record)
                            if re.search(r"reprinted|reproduced|adapted|copyright|©", label, re.I):
                                own_license = {
                                    "status": "needs_review",
                                    "url": license_record["url"],
                                    "page": index + 1,
                                    "evidence": label,
                                }
                            figures.append(
                                {
                                    **asset,
                                    "pdf_sha256": pdf["sha256"],
                                    "page": index + 1,
                                    "figure_number": match[1],
                                    "caption": label,
                                    "bbox": list(box),
                                    "dpi": round(scale * 72),
                                    "license": own_license,
                                }
                            )
                            if len(figures) >= 8:
                                break
            return {
                "pdf": pdf,
                "text_asset": text_asset,
                "checks": checks,
                "evidence": first[:1800],
                "license": license_record,
                "figures": figures,
                "warnings": warnings,
                "extractor_version": EXTRACTOR_VERSION,
                "parsed_pages": parsed_pages,
                "total_pages": total_pages,
            }
    except ResearchError:
        raise
    except (ValueError, RuntimeError, pdfium.PdfiumError):
        raise ResearchError("论文 PDF 解析失败或需要密码，未保存为已核验来源") from None
