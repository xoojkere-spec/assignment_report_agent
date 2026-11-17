from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

from assignment_agent.image_review import inspect_image_notice


def test_real_local_ocr_detects_copyright_notice_inside_image():
    image = Image.new("RGB", (1500, 400), "white")
    ImageDraw.Draw(image).multiline_text(
        (30, 80),
        "System components\nReprinted with permission.\nAll rights reserved.",
        fill="black",
        font=ImageFont.load_default(size=42),
        spacing=15,
    )
    output = BytesIO()
    image.save(output, "PNG")
    result = inspect_image_notice(output.getvalue())
    assert result["status"] == "blocked"
    assert result["method"] == "local_ocr_copyright_notice"
    assert result["notices"]
