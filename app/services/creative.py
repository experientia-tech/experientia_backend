"""Creative campaigns — per-task codes, code-stamped images and proof status.

Task rows and their codes are created inside the request (one transaction);
only image rendering runs in the background, and it is resumable: every run
renders whatever CreativeTask rows still lack an image.
"""

from __future__ import annotations

import traceback
from datetime import datetime
from uuid import UUID

from sqlalchemy import func

from app.database import SessionLocal, advisory_lock
from app.models import (
    Campaign,
    CreativeCampaign,
    CreativeGenerationStatus,
    CreativeTask,
    CreativeVerificationStatus,
    Task,
    TaskStatus,
)
from app.services.creative_codes import campaign_prefix, code_key, generate_code
from app.services.creative_print import build_print_pdf
from app.services.creative_render import load_source, render_creative_jpeg
from app.services.storage import storage

# Executors may (re)upload a proof in these states; managers always may.
RESUBMITTABLE = {
    CreativeVerificationStatus.NOT_SUBMITTED,
    CreativeVerificationStatus.FAILED,
    CreativeVerificationStatus.NEEDS_REVIEW,
}
# States where the executor still has work to do on the task.
EXECUTOR_OPEN = {
    CreativeVerificationStatus.NOT_SUBMITTED,
    CreativeVerificationStatus.FAILED,
}

_COMMIT_EVERY = 20


# The NNNN-NNNN part of every stored code (all codes are PP-NNNN-NNNN / CR-XXXX-XXXX).
_KEY_SQL = func.substr(CreativeTask.verificationCode, 4)


def _unique_codes(db, count: int, prefix: str) -> list[str]:
    """New codes whose digit part is unused by every task in every campaign."""
    codes: dict[str, str] = {}
    while len(codes) < count:
        batch = {
            code_key(c): c for c in (generate_code(prefix) for _ in range(count - len(codes)))
        }
        batch = {k: c for k, c in batch.items() if k not in codes}
        taken = {r[0] for r in db.query(_KEY_SQL).filter(_KEY_SQL.in_(list(batch))).all()}
        codes.update({k: c for k, c in batch.items() if k not in taken})
    return list(codes.values())


def code_owner(db, code: str) -> CreativeTask | None:
    """The task whose code has the same digit part, whatever prefix was read."""
    return db.query(CreativeTask).filter(_KEY_SQL == code_key(code)).first()


def ensure_task_codes(db, campaign_id: UUID) -> int:
    """Give every task in the campaign a CreativeTask row with a unique code."""
    db.flush()
    missing = (
        db.query(Task.id)
        .outerjoin(CreativeTask, CreativeTask.taskId == Task.id)
        .filter(Task.campaignId == campaign_id, CreativeTask.taskId.is_(None))
        .order_by(Task.createdAt.asc(), Task.id.asc())
        .all()
    )
    if not missing:
        return 0
    campaign = db.get(Campaign, campaign_id)
    try:
        codes: list[str | None] = _unique_codes(
            db, len(missing), campaign_prefix(campaign.name if campaign else None)
        )
    except Exception as exc:  # noqa: BLE001
        # Never block task creation on codes; managers can set them from the task page.
        print(f"[experientia] creative code generation failed campaign={campaign_id}: {exc}")
        codes = [None] * len(missing)
    now = datetime.utcnow()
    for (task_id,), code in zip(missing, codes):
        db.add(
            CreativeTask(
                taskId=task_id,
                campaignId=campaign_id,
                verificationCode=code,
                verificationStatus=CreativeVerificationStatus.NOT_SUBMITTED,
                verificationAttempts=0,
                createdAt=now,
                updatedAt=now,
            )
        )
    cc = db.get(CreativeCampaign, campaign_id)
    if cc:
        cc.generationStatus = CreativeGenerationStatus.PENDING
        cc.updatedAt = now
    return len(missing)


class CodeConflict(ValueError):
    pass


def set_task_code(db, task: Task, code: str | None) -> CreativeTask:
    """Set (or generate) a task's code and queue its image for re-rendering."""
    ct = task.creative
    now = datetime.utcnow()
    if ct is None:
        ct = CreativeTask(
            taskId=task.id,
            campaignId=task.campaignId,
            verificationStatus=CreativeVerificationStatus.NOT_SUBMITTED,
            verificationAttempts=0,
            createdAt=now,
        )
        db.add(ct)
        task.creative = ct
    code = code or _unique_codes(db, 1, campaign_prefix(task.campaign.name if task.campaign else None))[0]
    if code == ct.verificationCode:
        return ct
    owner = code_owner(db, code)
    if owner and owner.taskId != task.id:
        raise CodeConflict(f"Code {code} is already used by another task")
    ct.verificationCode = code
    # Same storage key per task, so the next generation run overwrites the old image.
    ct.generatedImageKey = None
    ct.generatedImageUrl = None
    ct.updatedAt = now
    cc = db.get(CreativeCampaign, task.campaignId)
    if cc and cc.generationStatus != CreativeGenerationStatus.PROCESSING:
        cc.generationStatus = CreativeGenerationStatus.PENDING
        cc.generationError = None
        cc.updatedAt = now
    return ct


def apply_status(
    task: Task, ct: CreativeTask, status: CreativeVerificationStatus, notes: str | None
) -> None:
    """Set the verification status and mirror it onto Task.status for existing dashboards."""
    now = datetime.utcnow()
    ct.verificationStatus = status
    ct.verificationNotes = notes
    ct.updatedAt = now
    if status == CreativeVerificationStatus.VERIFIED:
        ct.verifiedAt = now
        task.status = TaskStatus.ACCEPTED
        task.completedAt = now
        task.rejectionReason = None
    else:
        ct.verifiedAt = None
        task.completedAt = None
        if status == CreativeVerificationStatus.FAILED:
            task.status = TaskStatus.REJECTED
            task.rejectionReason = notes
        else:
            task.status = TaskStatus.PENDING
            task.rejectionReason = None
    task.updatedAt = now


def _reset_detection(ct: CreativeTask) -> None:
    ct.detectedCode = None
    ct.detectionMethod = None
    ct.ocrConfidence = None
    ct.codeMissing = False
    ct.verificationAttempts = 0


def record_proof(task: Task, ct: CreativeTask, key: str | None, url: str) -> None:
    ct.proofImageKey = key
    ct.proofImageUrl = url
    ct.proofUploadedAt = datetime.utcnow()
    _reset_detection(ct)
    apply_status(task, ct, CreativeVerificationStatus.PENDING_VERIFICATION, None)


def clear_proof(task: Task, ct: CreativeTask) -> None:
    ct.proofImageKey = None
    ct.proofImageUrl = None
    ct.proofUploadedAt = None
    _reset_detection(ct)
    apply_status(task, ct, CreativeVerificationStatus.NOT_SUBMITTED, None)


def _campaign_lock_key(campaign_id: UUID) -> int:
    return campaign_id.int >> 65  # fits a signed bigint


def generate_images(campaign_id: UUID) -> None:
    """Background job: render + upload the image for every task that lacks one."""
    with advisory_lock(_campaign_lock_key(campaign_id)) as got:
        if not got:
            return
        db = SessionLocal()
        try:
            _generate(db, campaign_id)
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            print(f"[experientia] creative generation failed campaign={campaign_id}: {exc}")
            traceback.print_exc()
            cc = db.get(CreativeCampaign, campaign_id)
            if cc:
                cc.generationStatus = CreativeGenerationStatus.FAILED
                cc.generationError = (str(exc) or exc.__class__.__name__)[:500]
                cc.updatedAt = datetime.utcnow()
                db.commit()
        finally:
            db.close()


def _generated_count(db, campaign_id: UUID) -> int:
    return (
        db.query(CreativeTask)
        .filter(CreativeTask.campaignId == campaign_id, CreativeTask.generatedImageKey.isnot(None))
        .count()
    )


def _generate(db, campaign_id: UUID) -> None:
    cc = db.get(CreativeCampaign, campaign_id)
    if not cc:
        return
    cc.generationStatus = CreativeGenerationStatus.PROCESSING
    cc.generationError = None
    cc.updatedAt = datetime.utcnow()
    db.commit()

    data = storage.get_bytes(cc.sourceImageKey, cc.sourceImageUrl, cache=False)
    if not data:
        raise RuntimeError("Source image not found in storage")
    source = load_source(data)

    # Loop so tasks added while this run was rendering are picked up too.
    while True:
        pending = (
            db.query(CreativeTask)
            .filter(
                CreativeTask.campaignId == campaign_id,
                CreativeTask.generatedImageKey.is_(None),
                # Tasks without a code wait until a manager sets one.
                CreativeTask.verificationCode.isnot(None),
            )
            .order_by(CreativeTask.createdAt.asc(), CreativeTask.taskId.asc())
            .all()
        )
        if not pending:
            break
        for i, ct in enumerate(pending, start=1):
            key = storage.make_creative_key(str(campaign_id), str(ct.taskId))
            url = storage.upload_bytes(
                key, render_creative_jpeg(source, ct.verificationCode), "image/jpeg"
            )
            ct.generatedImageKey = key
            ct.generatedImageUrl = url
            ct.updatedAt = datetime.utcnow()
            if i % _COMMIT_EVERY == 0:
                db.flush()
                cc.generatedCount = _generated_count(db, campaign_id)
                db.commit()
        db.flush()
        cc.generatedCount = _generated_count(db, campaign_id)
        db.commit()

    try:
        built = build_print_pdf(db, campaign_id)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Print PDF failed: {exc}") from exc
    cc.printPdfKey, cc.printPdfPages = built or (None, None)
    cc.printPdfAt = datetime.utcnow() if built else None
    cc.generationStatus = CreativeGenerationStatus.COMPLETED
    cc.updatedAt = datetime.utcnow()
    db.commit()


def resumable_campaign_ids() -> list[UUID]:
    """Campaigns whose generation was queued or interrupted (e.g. by a deploy)."""
    db = SessionLocal()
    try:
        return [
            r[0]
            for r in db.query(CreativeCampaign.campaignId)
            .filter(
                CreativeCampaign.generationStatus.in_(
                    [CreativeGenerationStatus.PENDING, CreativeGenerationStatus.PROCESSING]
                )
            )
            .all()
        ]
    finally:
        db.close()
