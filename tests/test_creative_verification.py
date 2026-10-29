import io
import shutil

import cv2
import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter

from app.models import CreativeDetectionMethod as Method
from app.models import CreativeVerificationStatus as Status
from app.services.creative_codes import code_key
from app.services.creative_codes import generate_code as _generate_code
from app.services.creative_render import load_source, render_creative, render_creative_jpeg
from app.services.creative_verification import CodeMatch, Detection, decide, detect_codes

needs_tesseract = pytest.mark.skipif(
    shutil.which("tesseract") is None, reason="tesseract binary not installed"
)


def poster(w=900, h=600) -> Image.Image:
    img = Image.new("RGB", (w, h))
    draw = ImageDraw.Draw(img)
    for y in range(h):
        draw.line([(0, y), (w, y)], fill=(40 + y * 150 // h, 90, 160))
    draw.ellipse([w // 4, h // 4, w * 3 // 4, h * 3 // 4], fill=(240, 180, 30))
    return img


def to_jpeg(img: Image.Image, quality=90) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


# ------------------------------------------------------------------ render


def generate_code() -> str:
    return _generate_code("NI")


# Fixed code for layout-sensitive tests: random ones make OCR/detection flaky (~3%).
FIXED_CODE = "NI-4827-1936"


def test_fixed_code_is_valid():
    from app.services.creative_codes import is_valid

    assert is_valid(FIXED_CODE)


def test_rendered_footer_qrs_decode_back_to_code():
    out = render_creative(load_source(to_jpeg(poster())), FIXED_CODE)
    ok, texts, _, _ = cv2.QRCodeDetector().detectAndDecodeMulti(np.array(out.convert("L")))
    assert ok and list(texts) == [FIXED_CODE, FIXED_CODE]


def test_render_adds_slim_footer_and_upscales_small_sources():
    source = load_source(to_jpeg(poster(600, 400)))
    assert source.width == 1200  # upscaled for legibility
    out = render_creative(source, generate_code())
    # The footer is attached below the artwork, which stays fully visible.
    footer_h = out.height - source.height
    assert out.width == source.width
    assert 0 < footer_h <= max(int(min(source.size) * 0.07), 80)
    assert out.crop((0, 0, *source.size)).tobytes() == source.tobytes()
    # White strip with a QR in each corner.
    assert out.getpixel((out.width // 2, source.height + 2)) == (255, 255, 255)
    ok, points = cv2.QRCodeDetector().detectMulti(np.array(out.convert("L")))
    assert ok and len(points) == 2
    assert all(quad[:, 1].min() >= source.height for quad in points)


def test_detect_reads_qr_from_generated_image():
    code = generate_code()
    data = render_creative_jpeg(load_source(to_jpeg(poster())), code)
    found = detect_codes(data)
    assert found.method == Method.QR
    assert found.codes == [code]


def _wall_photo(img: Image.Image, scale: float) -> bytes:
    # Placed on a wall, shot slightly rotated and soft.
    wall = Image.new("RGB", (img.width + 400, img.height + 400), (128, 120, 110))
    wall.paste(img, (200, 200))
    photo = wall.rotate(6, expand=True, fillcolor=(128, 120, 110))
    photo = photo.resize((int(photo.width * scale), int(photo.height * scale)))
    return to_jpeg(photo.filter(ImageFilter.GaussianBlur(1)), quality=80)


def test_detect_survives_photo_like_distortion():
    img = render_creative(load_source(to_jpeg(poster())), FIXED_CODE)
    found = detect_codes(_wall_photo(img, 1))
    assert found.method == Method.QR and found.codes == [FIXED_CODE]


@needs_tesseract
def test_low_res_photo_falls_back_to_printed_code():
    # The footer QR is small; in a low-res, soft shot the printed code is still read.
    img = render_creative(load_source(to_jpeg(poster())), FIXED_CODE)
    found = detect_codes(_wall_photo(img, 2 / 3))
    # The prefix may be lost; matching uses the digit part, which is unique on its own.
    assert code_key(FIXED_CODE) in [code_key(c) for c in found.codes]


@needs_tesseract
def test_ocr_fallback_when_qr_is_unreadable():
    code = FIXED_CODE
    img = render_creative(load_source(to_jpeg(poster())), code)
    # Cover both footer QRs so only the printed text remains.
    ok, points = cv2.QRCodeDetector().detectMulti(np.array(img.convert("L")))
    assert ok and len(points) == 2
    draw = ImageDraw.Draw(img)
    for quad in points:
        xs, ys = quad[:, 0], quad[:, 1]
        draw.rectangle([xs.min() - 10, ys.min() - 10, xs.max() + 10, ys.max() + 10], fill="white")
    found = detect_codes(to_jpeg(img))
    assert found.method == Method.OCR
    assert found.codes == [code]
    assert found.confidence is not None


@needs_tesseract
def test_unrelated_photo_has_no_code():
    found = detect_codes(to_jpeg(poster()))
    assert found.codes == []
    assert found.method is None


# ------------------------------------------------------------------ matching


def _no_match(_code):
    return None


def _ours(code, used_by=None):
    return lambda read: CodeMatch(code, True, used_by) if code_key(read) == code_key(code) else None


def test_decide_verifies_any_code_of_the_campaign():
    code = generate_code()
    d = decide(Detection([code], Method.QR), _ours(code), 60)
    assert d.status == Status.VERIFIED and d.detected_code == code and d.method == Method.QR
    assert not d.code_missing


def test_decide_prefers_campaign_code_when_several_are_visible():
    own, noise = generate_code(), generate_code()
    d = decide(Detection([noise, own], Method.OCR, 90), _ours(own), 60)
    assert d.status == Status.VERIFIED and d.detected_code == own


def test_decide_corrects_a_misread_prefix():
    code = generate_code()
    for read in ("??" + code[2:], "M1" + code[2:]):
        d = decide(Detection([read], Method.OCR, 90), _ours(code), 60)
        assert d.status == Status.VERIFIED and d.detected_code == code


def test_decide_low_ocr_confidence_needs_review():
    code = generate_code()
    d = decide(Detection([code], Method.OCR, 41.0), _ours(code), 60)
    assert d.status == Status.NEEDS_REVIEW
    assert "low confidence" in d.notes


def test_decide_creative_already_used_by_another_task_needs_review():
    code = generate_code()
    d = decide(Detection([code], Method.QR), _ours(code, used_by="task #4"), 60)
    assert d.status == Status.NEEDS_REVIEW
    assert "task #4" in d.notes and not d.code_missing


def test_decide_other_campaigns_code_fails():
    other = generate_code()
    d = decide(Detection([other], Method.QR), lambda c: CodeMatch(other, False), 60)
    assert d.status == Status.FAILED
    assert other in d.notes and "another campaign" in d.notes


def test_decide_low_confidence_read_of_other_campaign_needs_review():
    other = generate_code()
    d = decide(Detection([other], Method.OCR, 30.0), lambda c: CodeMatch(other, False), 60)
    assert d.status == Status.NEEDS_REVIEW


def test_decide_unknown_code_counts_as_missing():
    d = decide(Detection([generate_code()], Method.QR), _no_match, 60)
    assert d.status == Status.NEEDS_REVIEW and d.code_missing
    assert "does not match" in d.notes


def test_decide_nothing_found_needs_review():
    d = decide(Detection(), _no_match, 60)
    assert d.status == Status.NEEDS_REVIEW
    assert d.detected_code is None and d.code_missing


def test_decide_uses_confidence_of_the_matching_line():
    own, noise = generate_code(), generate_code()
    detection = Detection([noise, own], Method.OCR, 20.0, confidences={noise: 20.0, own: 91.0})
    d = decide(detection, _ours(own), 60)
    assert d.status == Status.VERIFIED and d.confidence == 91.0
