"""Read verification codes from Creative proof photos (QR first, OCR fallback).

Used by the scheduled job, "Verify now", the optional verify-on-upload path and manual runs:

    python -m app.services.creative_verification [--campaign <uuid>]
"""

from __future__ import annotations

import argparse
import io
import string
import traceback
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable
from uuid import UUID

import cv2
import numpy as np
from PIL import Image, ImageOps

from app.config import get_settings
from app.database import SessionLocal, advisory_lock
from app.models import (
    CreativeDetectionMethod,
    CreativeTask,
    CreativeVerificationLog,
    CreativeVerificationStatus,
    Task,
)
from app.serializers import resolve_sequence_number
from app.services.creative import apply_status, code_owner
from app.services.creative_codes import extract_codes, normalize, is_valid
from app.services.storage import storage

NIGHTLY_LOCK_KEY = 0x43524541_54495645  # "CREATIVE"
# Any letter can start a code (campaign name prefix).
_OCR_WHITELIST = string.ascii_uppercase + string.digits + "-"
_MAX_SIDE = 2000
_UPSCALE_BELOW = 1600


@dataclass
class Detection:
    codes: list[str] = field(default_factory=list)  # checksum-valid, best first
    method: CreativeDetectionMethod | None = None
    confidence: float | None = None
    raw_text: str = ""
    # OCR confidence of the line each code was read from.
    confidences: dict[str, float | None] = field(default_factory=dict)

    def confidence_of(self, code: str) -> float | None:
        return self.confidences.get(code, self.confidence)


@dataclass
class Decision:
    status: CreativeVerificationStatus
    detected_code: str | None
    method: CreativeDetectionMethod | None
    confidence: float | None
    notes: str | None
    # Nothing in the photo is a code of any task.
    code_missing: bool = False


# ---------------------------------------------------------------- detection


def _load(data: bytes) -> Image.Image:
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    return ImageOps.autocontrast(img.convert("L"))


def _fit(img: Image.Image, max_side: int) -> Image.Image:
    if max(img.size) <= max_side:
        return img
    out = img.copy()
    out.thumbnail((max_side, max_side), Image.LANCZOS)
    return out


def _variants(gray: Image.Image) -> list[Image.Image]:
    """Whole photo, then the bottom part where the code strip usually is (kept at full res)."""
    w, h = gray.size
    return [_fit(gray, _MAX_SIDE), _fit(gray.crop((0, int(h * 0.55), w, h)), _MAX_SIDE * 3 // 2)]


def _qr_variants(variants: list[Image.Image]) -> list[Image.Image]:
    # Small QR symbols decode much better after a 2x upscale.
    out = list(variants)
    for v in variants:
        if max(v.size) < _UPSCALE_BELOW:
            out.append(v.resize((v.width * 2, v.height * 2), Image.BICUBIC))
    return out


def _decode_qr(variants: list[Image.Image]) -> Detection | None:
    detectors = [cv2.QRCodeDetector()]
    if hasattr(cv2, "QRCodeDetectorAruco"):
        detectors.append(cv2.QRCodeDetectorAruco())
    for variant in variants:
        arr = np.array(variant)
        for detector in detectors:
            try:
                text, _, _ = detector.detectAndDecode(arr)
                # The footer carries two QRs, which can confuse the single-symbol detector.
                texts = [text] if text else list(detector.detectAndDecodeMulti(arr)[1] or [])
            except cv2.error:
                continue
            for text in texts:
                code = normalize(text) if text else None
                if code and is_valid(code):
                    return Detection(codes=[code], method=CreativeDetectionMethod.QR, raw_text=text)
    return None


def _ocr_lines(img: Image.Image, psm: int) -> list[tuple[str, float | None]]:
    import pytesseract

    data = pytesseract.image_to_data(
        img,
        config=f"--psm {psm} -c tessedit_char_whitelist={_OCR_WHITELIST}",
        output_type=pytesseract.Output.DICT,
    )
    lines: dict[tuple, list[tuple[str, float]]] = {}
    for i, word in enumerate(data["text"]):
        word = (word or "").strip()
        if not word:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append((word, float(data["conf"][i])))
    out = []
    for words in lines.values():
        confs = [c for _, c in words if c >= 0]
        out.append((" ".join(w for w, _ in words), sum(confs) / len(confs) if confs else None))
    return out


def _prepare_for_ocr(variant: Image.Image) -> Image.Image:
    if variant.width < 1500:
        scale = 1500 / variant.width
        variant = variant.resize((1500, max(1, round(variant.height * scale))), Image.LANCZOS)
    arr = np.array(variant)
    _, binary = cv2.threshold(arr, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return Image.fromarray(binary)


def _ocr(variants: list[Image.Image]) -> Detection:
    texts: list[str] = []
    # Bottom strip first — less clutter, larger text relative to the crop.
    for variant in reversed(variants):
        prepared = _prepare_for_ocr(variant)
        for psm in (11, 6):
            found: dict[str, float | None] = {}
            lines: list[str] = []
            for line, conf in _ocr_lines(prepared, psm):
                texts.append(line)
                codes = extract_codes(line)
                if codes:
                    lines.append(line)
                for code in codes:
                    found.setdefault(code, conf)
            if found:
                # Other text on a poster (phone numbers…) can pass a checksum too, so keep
                # every candidate: ones read with their prefix first, then the most confident.
                codes = sorted(found, key=lambda c: (c.startswith("??"), -(found[c] or 0)))
                return Detection(
                    codes=codes,
                    method=CreativeDetectionMethod.OCR,
                    confidence=found[codes[0]],
                    raw_text="\n".join(lines)[:2000],
                    confidences=found,
                )
    return Detection(raw_text="\n".join(texts)[:2000])


def detect_codes(data: bytes, provider: str | None = None) -> Detection:
    variants = _variants(_load(data))
    found = _decode_qr(_qr_variants(variants))
    if found:
        return found
    provider = (provider or get_settings().creative_ocr_provider).lower()
    if provider != "tesseract":
        raise RuntimeError(f"Unsupported CREATIVE_OCR_PROVIDER '{provider}'")
    return _ocr(variants)


# ---------------------------------------------------------------- matching


@dataclass
class CodeMatch:
    """A code read from a photo that belongs to a real task."""

    code: str  # as stored on its task, so a misread prefix is corrected
    same_campaign: bool
    # Another task in this campaign already passed with this same creative.
    used_by: str | None = None


def decide(
    detection: Detection,
    lookup: Callable[[str], CodeMatch | None],
    min_confidence: float,
) -> Decision:
    """Pass the photo if it shows any creative of this campaign.

    Executors may put any of the campaign's creatives at any spot, so the code
    doesn't have to be this task's own; it only has to belong to this campaign.
    """
    method = detection.method
    matches = [(read, m) for read in detection.codes if (m := lookup(read))]
    # A creative not yet used by another task first.
    ours = sorted((x for x in matches if x[1].same_campaign), key=lambda x: x[1].used_by is not None)
    if ours:
        read, match = ours[0]
        conf = detection.confidence_of(read)
        if method == CreativeDetectionMethod.OCR and conf is not None and conf < min_confidence:
            return Decision(
                CreativeVerificationStatus.NEEDS_REVIEW,
                match.code,
                method,
                conf,
                f"Code read by OCR with low confidence ({conf:.0f}%)",
            )
        if match.used_by:
            return Decision(
                CreativeVerificationStatus.NEEDS_REVIEW,
                match.code,
                method,
                conf,
                f"Creative {match.code} was already submitted for {match.used_by}",
            )
        return Decision(CreativeVerificationStatus.VERIFIED, match.code, method, conf, None)

    if matches:
        read, match = matches[0]
        conf = detection.confidence_of(read)
        # A shaky OCR read can misread two characters and still pass the checksum,
        # so only fail outright when the read is trustworthy.
        if method == CreativeDetectionMethod.OCR and (conf is None or conf < min_confidence):
            return Decision(
                CreativeVerificationStatus.NEEDS_REVIEW,
                match.code,
                method,
                conf,
                f"Low-confidence OCR read {match.code}, which belongs to another campaign",
            )
        return Decision(
            CreativeVerificationStatus.FAILED,
            match.code,
            method,
            conf,
            f"Photo shows code {match.code}, which belongs to another campaign",
        )
    if detection.codes:
        code = detection.codes[0]
        return Decision(
            CreativeVerificationStatus.NEEDS_REVIEW,
            code,
            method,
            detection.confidence,
            f"Read code {code}, which does not match any creative",
            code_missing=True,
        )
    return Decision(
        CreativeVerificationStatus.NEEDS_REVIEW,
        None,
        None,
        None,
        "No verification code found in the photo",
        code_missing=True,
    )


def _task_label(ct: CreativeTask) -> str:
    seq = resolve_sequence_number(ct.task) if ct.task else 0
    return f"task #{seq}" if seq else f"task {ct.taskId}"


def _code_lookup(db, task: Task) -> Callable[[str], CodeMatch | None]:
    def lookup(read: str) -> CodeMatch | None:
        owner = code_owner(db, read)
        if not owner:
            return None
        if owner.campaignId != task.campaignId:
            return CodeMatch(owner.verificationCode, same_campaign=False)
        used = (
            db.query(CreativeTask)
            .filter(
                CreativeTask.campaignId == task.campaignId,
                CreativeTask.taskId != task.id,
                CreativeTask.detectedCode == owner.verificationCode,
                CreativeTask.verificationStatus == CreativeVerificationStatus.VERIFIED,
            )
            .first()
        )
        return CodeMatch(owner.verificationCode, True, _task_label(used) if used else None)

    return lookup


# ---------------------------------------------------------------- DB


def verify_task(db, task_id: UUID) -> CreativeVerificationStatus | None:
    """Verify one PENDING_VERIFICATION task. Returns the new status, or None if skipped."""
    settings = get_settings()
    ct = (
        db.query(CreativeTask)
        .filter(
            CreativeTask.taskId == task_id,
            CreativeTask.verificationStatus == CreativeVerificationStatus.PENDING_VERIFICATION,
        )
        .with_for_update(skip_locked=True)
        .first()
    )
    if not ct:
        db.rollback()
        return None
    task = ct.task
    ct.verificationAttempts = int(ct.verificationAttempts or 0) + 1
    entry = CreativeVerificationLog(taskId=task_id, attemptAt=datetime.utcnow())

    try:
        data = storage.get_bytes(ct.proofImageKey, ct.proofImageUrl or "", cache=False)
        if not data:
            raise RuntimeError("Proof image could not be downloaded")
        detection = detect_codes(data)
    except Exception as exc:  # noqa: BLE001
        entry.result = "ERROR"
        entry.error = str(exc) or exc.__class__.__name__
        if ct.verificationAttempts >= settings.creative_verify_max_attempts:
            apply_status(
                task,
                ct,
                CreativeVerificationStatus.NEEDS_REVIEW,
                f"Verification failed {ct.verificationAttempts} times: {entry.error}",
            )
        ct.updatedAt = datetime.utcnow()
        db.add(entry)
        db.commit()
        print(f"[experientia] creative verify task={task_id} error: {entry.error}")
        return ct.verificationStatus

    decision = decide(
        detection,
        _code_lookup(db, task),
        settings.creative_ocr_min_confidence,
    )
    ct.detectedCode = decision.detected_code
    ct.detectionMethod = decision.method
    ct.ocrConfidence = decision.confidence
    ct.codeMissing = decision.code_missing
    apply_status(task, ct, decision.status, decision.notes)

    entry.method = decision.method.value if decision.method else None
    entry.rawText = detection.raw_text
    entry.detectedCode = decision.detected_code
    entry.ocrConfidence = decision.confidence
    entry.result = decision.status.value
    db.add(entry)
    db.commit()
    return decision.status


def run_verification(campaign_id: UUID | None = None) -> dict:
    """Verify every pending proof (or one campaign's). The all-campaigns run is single-instance."""
    if campaign_id is None:
        with advisory_lock(NIGHTLY_LOCK_KEY) as got:
            if not got:
                print("[experientia] creative verify skipped: another instance holds the lock")
                return {"skipped": True}
            return _run(None)
    return _run(campaign_id)


def _run(campaign_id: UUID | None) -> dict:
    settings = get_settings()
    db = SessionLocal()
    tally: Counter = Counter()
    by_campaign: dict[UUID, Counter] = {}
    try:
        q = db.query(CreativeTask.taskId, CreativeTask.campaignId).filter(
            CreativeTask.verificationStatus == CreativeVerificationStatus.PENDING_VERIFICATION
        )
        if campaign_id:
            q = q.filter(CreativeTask.campaignId == campaign_id)
        pending = q.order_by(CreativeTask.proofUploadedAt.asc()).all()
        batch = max(1, settings.creative_verify_batch_size)
        for start in range(0, len(pending), batch):
            for task_id, cid in pending[start : start + batch]:
                try:
                    status = verify_task(db, task_id)
                except Exception:  # noqa: BLE001
                    db.rollback()
                    print(f"[experientia] creative verify task={task_id} crashed")
                    traceback.print_exc()
                    status = None
                key = status.value if status else "SKIPPED"
                tally[key] += 1
                by_campaign.setdefault(cid, Counter())[key] += 1
            db.expire_all()
    finally:
        db.close()

    summary = {
        "processed": sum(tally.values()),
        "verified": tally[CreativeVerificationStatus.VERIFIED.value],
        "failed": tally[CreativeVerificationStatus.FAILED.value],
        "needs_review": tally[CreativeVerificationStatus.NEEDS_REVIEW.value],
        "still_pending": tally[CreativeVerificationStatus.PENDING_VERIFICATION.value],
        "skipped": tally["SKIPPED"],
    }
    print(f"[experientia] creative verify summary {summary}")
    for cid, counts in by_campaign.items():
        print(f"[experientia] creative verify campaign={cid} {dict(counts)}")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verify pending Creative proof photos.")
    parser.add_argument("--campaign", type=UUID, default=None, help="Only this campaign")
    args = parser.parse_args()
    print(run_verification(args.campaign))
