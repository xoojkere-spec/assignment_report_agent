"""图片许可只作如实提示；来源、文件及图意核验仍是使用门槛。"""

import re

WEB_IMAGE_METHODS = {"licensed_web_image", "web_page_image"}
IMAGE_POLICY = "verified_source_license_notice"


def auto_license(figure):
    """仅识别已有开放许可声明，不代表单图已经取得授权，也不用于阻断图片。"""
    record = figure["license"]
    evidence = record.get("evidence", "")
    return bool(
        re.search(
            r"\blicensed\b|\blicenced\b|\bdistributed under\b|\breleased under\b|\bCC0\b|许可|授权",
            evidence,
            re.I,
        )
        and not re.search(r"not licensed|not licenced|does not apply|no permission", evidence, re.I)
        and record["status"] == "declared"
        and re.fullmatch(
            r"https://creativecommons\.org/(?:licenses/by(?:-sa)?/(?:3\.0|4\.0)|publicdomain/zero/1\.0)/",
            record.get("url") or "",
        )
    )


def image_warnings(figure):
    warnings = []
    if not auto_license(figure):
        warnings.append("图片许可或适用条件未确认；保留来源记录不代表已获授权。")
    notices = (figure.get("image_review") or {}).get("notices", [])
    if notices:
        warnings.append("原图版权声明：" + "；".join(notices))
    if (figure.get("license_review") or {}).get("applicable") is False:
        warnings.append("模型未确认该许可适用于单张图片。")
    return warnings
