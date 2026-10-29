"""Attach a slim verification footer (QR · code · QR) under a creative."""

from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path

import qrcode
from PIL import Image, ImageDraw, ImageFont, ImageOps

FONT_PATH = Path(__file__).resolve().parent.parent / "assets" / "fonts" / "DejaVuSansMono-Bold.ttf"

# Small sources are upscaled so the badge text stays legible in field photos.
MIN_WIDTH = 1200
# QR size as a share of the creative's shorter side, with a floor in pixels.
QR_RATIO = 0.05
MIN_QR_PX = 64
QUIET_ZONE = 2  # modules; the white footer around the QR extends it
JPEG_QUALITY = 92


def load_source(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, "white")
        bg.paste(rgba, mask=rgba.split()[-1])
        img = bg
    else:
        img = img.convert("RGB")
    if img.width < MIN_WIDTH:
        scale = MIN_WIDTH / img.width
        img = img.resize((MIN_WIDTH, max(1, round(img.height * scale))), Image.LANCZOS)
    return img


@lru_cache(maxsize=32)
def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_PATH), size)


def _qr_image(code: str, target: int) -> Image.Image:
    # Q fits CR-XXXX-XXXX in a version-1 (21-module) symbol — bigger modules than H,
    # which decodes far more reliably from small/blurry field photos.
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_Q,
        box_size=1,
        border=QUIET_ZONE,
    )
    qr.add_data(code)
    qr.make(fit=True)
    modules = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    # Whole pixels per module keep the symbol crisp; at least 2 so phone photos can read it.
    box = max(2, round(target / modules.width))
    return modules.resize((modules.width * box, modules.height * box), Image.NEAREST)


def render_creative(source: Image.Image, code: str) -> Image.Image:
    """Return the creative with a white [QR   code   QR] footer strip added below it.

    The strip sits outside the artwork, so nothing in the design is covered.
    """
    w, h = source.size
    # The QR's own white quiet zone doubles as the footer's padding.
    qr = _qr_image(code, max(int(min(w, h) * QR_RATIO), MIN_QR_PX))
    footer_h = qr.height

    canvas = Image.new("RGB", (w, h + footer_h), "white")
    canvas.paste(source, (0, 0))
    canvas.paste(qr, (0, h))
    canvas.paste(qr, (w - qr.width, h))
    draw = ImageDraw.Draw(canvas)

    # Largest font that fits between the QRs at up to ~40% of the strip height.
    max_text_w = w - 2 * qr.width - footer_h
    size = footer_h
    while size > 10:
        left, top, right, bottom = draw.textbbox((0, 0), code, font=_font(size))
        if right - left <= max_text_w and bottom - top <= footer_h * 0.4:
            break
        size = int(size * 0.92)
    font = _font(size)
    left, top, right, bottom = draw.textbbox((0, 0), code, font=font)
    x = (w - (right - left)) // 2 - left
    y = h + (footer_h - (bottom - top)) // 2 - top
    draw.text((x, y), code, fill="black", font=font)
    return canvas


def render_creative_jpeg(source: Image.Image, code: str) -> bytes:
    buf = io.BytesIO()
    render_creative(source, code).save(buf, format="JPEG", quality=JPEG_QUALITY, subsampling=0)
    return buf.getvalue()
