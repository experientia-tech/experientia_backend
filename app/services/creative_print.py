"""Print-ready PDF of every generated creative in a campaign — one full-bleed page each.

Pages keep each creative's aspect ratio, scaled to fit A4 (portrait or landscape), so
office printers can print at "fit to page" and print shops can scale up. JPEGs are
embedded as-is (DCTDecode, no re-encoding) and the PDF is streamed to a temp file one
page at a time, so memory stays flat even for the largest campaigns.
"""

from __future__ import annotations

import io
import tempfile
from pathlib import Path
from typing import BinaryIO
from uuid import UUID

from PIL import Image

from app.models import Campaign, CreativeTask, Task
from app.serializers import resolve_sequence_number
from app.services.storage import storage

A4_SHORT, A4_LONG = 595.2756, 841.8898  # points
_COLOR_SPACES = {"RGB": b"/DeviceRGB", "L": b"/DeviceGray", "CMYK": b"/DeviceCMYK"}


def _page_size(img_w: int, img_h: int) -> tuple[float, float]:
    box_w, box_h = (A4_SHORT, A4_LONG) if img_h >= img_w else (A4_LONG, A4_SHORT)
    scale = min(box_w / img_w, box_h / img_h)
    return img_w * scale, img_h * scale


def _text(value: str) -> bytes:
    """PDF text string as UTF-16BE hex, so any campaign name renders in the viewer."""
    return b"<FEFF" + value.encode("utf-16-be").hex().upper().encode() + b">"


class _PdfWriter:
    """Minimal sequential PDF writer: objects go straight to disk, xref at the end."""

    def __init__(self, fh: BinaryIO) -> None:
        self.fh = fh
        self.offsets: dict[int, int] = {}
        self.next_num = 1
        fh.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")

    def reserve(self) -> int:
        num = self.next_num
        self.next_num += 1
        return num

    def write(self, num: int, body: bytes, stream: bytes | None = None) -> None:
        self.offsets[num] = self.fh.tell()
        self.fh.write(b"%d 0 obj\n" % num)
        if stream is None:
            self.fh.write(body + b"\nendobj\n")
            return
        # `body` is a dictionary; splice /Length in before its closing ">>".
        self.fh.write(body[:-2] + b" /Length %d >>\nstream\n" % len(stream))
        self.fh.write(stream)
        self.fh.write(b"\nendstream\nendobj\n")

    def finish(self, root: int, info: int) -> None:
        xref = self.fh.tell()
        size = self.next_num
        self.fh.write(b"xref\n0 %d\n0000000000 65535 f \n" % size)
        for num in range(1, size):
            self.fh.write(b"%010d 00000 n \n" % self.offsets[num])
        self.fh.write(
            b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (size, root, info, xref)
        )


def build_print_pdf(db, campaign_id: UUID) -> tuple[str, int] | None:
    """Build and upload the campaign's print PDF. Returns (storage key, pages), or None if empty."""
    rows = (
        db.query(CreativeTask, Task)
        .join(Task, Task.id == CreativeTask.taskId)
        .filter(
            CreativeTask.campaignId == campaign_id,
            CreativeTask.generatedImageKey.isnot(None),
        )
        .order_by(Task.createdAt.asc(), Task.id.asc())
        .all()
    )
    if not rows:
        return None
    campaign = db.get(Campaign, campaign_id)
    title = f"{campaign.name if campaign else 'Creatives'} — print creatives"

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "print.pdf"
        with path.open("wb") as fh:
            pdf = _PdfWriter(fh)
            catalog, pages_num, outlines, info = (pdf.reserve() for _ in range(4))
            page_nums: list[int] = []
            bookmarks: list[str] = []
            for i, (ct, task) in enumerate(rows, start=1):
                data = storage.get_bytes(ct.generatedImageKey, ct.generatedImageUrl or "", cache=False)
                if not data:
                    raise RuntimeError(f"Generated image missing for task {task.id}")
                with Image.open(io.BytesIO(data)) as im:  # reads the header only
                    if im.format != "JPEG" or im.mode not in _COLOR_SPACES:
                        raise RuntimeError(f"Generated image for task {task.id} is not a JPEG")
                    (img_w, img_h), mode = im.size, im.mode
                w, h = _page_size(img_w, img_h)

                image, content, page = pdf.reserve(), pdf.reserve(), pdf.reserve()
                pdf.write(
                    image,
                    b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace %s "
                    b"/BitsPerComponent 8 /Filter /DCTDecode >>"
                    % (img_w, img_h, _COLOR_SPACES[mode]),
                    data,
                )
                pdf.write(content, b"<< >>", b"q %.4f 0 0 %.4f 0 0 cm /Im Do Q" % (w, h))
                pdf.write(
                    page,
                    b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %.4f %.4f] "
                    b"/Resources << /XObject << /Im %d 0 R >> >> /Contents %d 0 R >>"
                    % (pages_num, w, h, image, content),
                )
                page_nums.append(page)
                # Bookmark per page so the team can jump to a task in the PDF viewer.
                bookmarks.append(f"Task #{resolve_sequence_number(task) or i} — {ct.verificationCode}")

            items = [pdf.reserve() for _ in page_nums]
            for idx, (item, page, label) in enumerate(zip(items, page_nums, bookmarks)):
                links = b""
                if idx:
                    links += b" /Prev %d 0 R" % items[idx - 1]
                if idx < len(items) - 1:
                    links += b" /Next %d 0 R" % items[idx + 1]
                pdf.write(
                    item,
                    b"<< /Title %s /Parent %d 0 R /Dest [%d 0 R /Fit]%s >>"
                    % (_text(label), outlines, page, links),
                )
            pdf.write(
                outlines,
                b"<< /Type /Outlines /First %d 0 R /Last %d 0 R /Count %d >>"
                % (items[0], items[-1], len(items)),
            )
            kids = b" ".join(b"%d 0 R" % n for n in page_nums)
            pdf.write(pages_num, b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(page_nums)))
            pdf.write(
                catalog,
                b"<< /Type /Catalog /Pages %d 0 R /Outlines %d 0 R /PageMode /UseOutlines >>"
                % (pages_num, outlines),
            )
            pdf.write(info, b"<< /Title %s /Producer (Experientia) >>" % _text(title))
            pdf.finish(catalog, info)

        key = storage.make_creative_print_key(str(campaign_id))
        storage.upload_file(key, path, "application/pdf")
    return key, len(page_nums)
