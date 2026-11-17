"""本地 OCR 只用于发现图片中的版权冲突；没有识别到冲突不等于获得转载许可。"""

import re
from functools import lru_cache
from io import BytesIO

import numpy as np
from PIL import Image

from assignment_agent.ocr import get_engine
from assignment_agent.research_types import ResearchError

IMAGE_REVIEW_VERSION = 1
RESTRICTED = re.compile(
    r"(?:reprinted|reproduced|used)\s*(?:by|with)\s*permission"
    r"|all\s*rights\s*reserved|版权所有|经.{0,8}许可.{0,6}转载",
    re.I,
)


@lru_cache(maxsize=16)
def inspect_image_notice(raw):
    try:
        with Image.open(BytesIO(raw)) as original:
            with original.convert("RGB") as image:
                scale = min(2, 1800 / max(image.size))
                if scale != 1:
                    image = image.resize(tuple(round(x * scale) for x in image.size))
                pixels = np.asarray(image)[:, :, ::-1].copy()
        result, _ = get_engine()(pixels)
        text = " ".join(line[1] for line in result or [] if str(line[1]).strip())
        notices = [text[max(0, m.start() - 60) : m.end() + 120] for m in RESTRICTED.finditer(text)]
        return {
            "version": IMAGE_REVIEW_VERSION,
            "status": "blocked" if notices else "checked",
            "method": "local_ocr_copyright_notice",
            "notices": notices[:4],
        }
    except (OSError, ValueError, RuntimeError, Image.DecompressionBombError):
        raise ResearchError("本地图片版权声明检查失败，不能自动插入该图片") from None
