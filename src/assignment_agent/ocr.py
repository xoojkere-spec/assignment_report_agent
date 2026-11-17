"""按需初始化本地 CPU OCR，逐页渲染扫描内容。"""

from functools import lru_cache

from assignment_agent.schemas import SourceUnit


@lru_cache(maxsize=1)
def get_engine():
    from rapidocr_onnxruntime import RapidOCR

    # 限制推理线程，避免 OCR 占满本机 CPU；模型随依赖安装，无需在线下载。
    return RapidOCR(intra_op_num_threads=1, inter_op_num_threads=1)


def recognize_pdf_page(raw: bytes, index: int) -> list[SourceUnit]:
    import numpy as np
    import pypdfium2 as pdfium

    try:
        document = pdfium.PdfDocument(raw)
        try:
            page = document[index]
            try:
                width, height = page.get_size()
                scale = min(3.0, (16_000_000 / (width * height)) ** 0.5)
                if scale < 1:
                    raise ValueError("PDF 页面尺寸过大，无法在像素限制内清晰识别")
                bitmap = page.render(scale=scale)
                try:
                    with bitmap.to_pil().convert("RGB") as image:
                        pixels = np.asarray(image)[:, :, ::-1].copy()
                finally:
                    bitmap.close()
            finally:
                page.close()
        finally:
            document.close()
        result, _ = get_engine()(pixels)
        return [
            SourceUnit(
                location=f"page:{index + 1}:ocr-line:{line}",
                text=text,
                method="ocr",
                confidence=float(score),
            )
            for line, (_, text, score) in enumerate(result or [], 1)
            if text.strip()
        ]
    except (RuntimeError, pdfium.PdfiumError) as exc:
        raise ValueError(f"PDF 第 {index + 1} 页 OCR 失败：{exc}") from exc
