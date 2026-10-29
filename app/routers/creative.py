from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from app.auth import ManagerUser, DbSession
from app.media_rules import is_creative
from app.models import (
    Campaign,
    CreativeCampaign,
    CreativeDetectionMethod,
    CreativeGenerationStatus,
    CreativeTask,
    CreativeVerificationLog,
    CreativeVerificationStatus,
    Task,
    User,
)
from app.schemas import (
    CreativeCampaignOut,
    CreativeCodeUpdate,
    CreativeManualVerify,
    CreativeReport,
    CreativeReportItem,
    CreativeReportTotals,
)
from app.serializers import creative_campaign_out, creative_report_item
from app.services.creative import CodeConflict, apply_status, set_task_code
from app.services.creative_codes import CODE_RE, campaign_prefix, is_valid, normalize
from app.services.creative_jobs import enqueue_campaign_verification, enqueue_generation
# from app.services.creative_jobs import describe_schedule  # SCHEDULED VERIFICATION (disabled)
from app.services.storage import storage

router = APIRouter(prefix="/api", tags=["creative"])


def _creative_campaign(db, user, campaign_id: UUID) -> Campaign:
    campaign = (
        db.query(Campaign)
        .options(joinedload(Campaign.creative))
        .filter(Campaign.id == campaign_id)
        .first()
    )
    if not campaign or campaign.organizationId != user.organizationId:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if not is_creative(campaign.serviceType) or not campaign.creative:
        raise HTTPException(status_code=400, detail="Not a Creative campaign")
    return campaign


@router.get("/campaigns/{campaign_id}/creative-report", response_model=CreativeReport)
def creative_report(
    campaign_id: UUID,
    db: DbSession,
    user: ManagerUser,
    status: str | None = None,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=500),
):
    campaign = _creative_campaign(db, user, campaign_id)

    counts = {
        s: n
        for s, n in db.query(CreativeTask.verificationStatus, func.count(CreativeTask.taskId))
        .filter(CreativeTask.campaignId == campaign_id)
        .group_by(CreativeTask.verificationStatus)
        .all()
    }
    total = sum(counts.values())
    not_submitted = counts.get(CreativeVerificationStatus.NOT_SUBMITTED, 0)
    verified = counts.get(CreativeVerificationStatus.VERIFIED, 0)
    submitted = total - not_submitted
    totals = CreativeReportTotals(
        total_tasks=total,
        not_submitted=not_submitted,
        pending_verification=counts.get(CreativeVerificationStatus.PENDING_VERIFICATION, 0),
        verified=verified,
        failed=counts.get(CreativeVerificationStatus.FAILED, 0),
        needs_review=counts.get(CreativeVerificationStatus.NEEDS_REVIEW, 0),
        success_rate=round(verified * 100 / submitted, 1) if submitted else 0.0,
    )

    q = (
        db.query(CreativeTask, Task, User)
        .join(Task, Task.id == CreativeTask.taskId)
        .outerjoin(User, User.id == Task.executorUserId)
        .filter(CreativeTask.campaignId == campaign_id)
    )
    if status:
        try:
            q = q.filter(CreativeTask.verificationStatus == CreativeVerificationStatus(status.upper()))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Unknown status '{status}'")
    items_total = q.count()
    rows = (
        q.order_by(Task.createdAt.asc(), Task.id.asc())
        .offset((page - 1) * limit)
        .limit(limit)
        .all()
    )

    return CreativeReport(
        campaign_id=campaign.id,
        campaign_name=campaign.name,
        generation=creative_campaign_out(campaign.creative),
        totals=totals,
        items=[creative_report_item(ct, task, executor) for ct, task, executor in rows],
        page=page,
        page_size=limit,
        items_total=items_total,
        # SCHEDULED VERIFICATION (disabled): None makes the report say checks are manual.
        # verify_schedule=describe_schedule(),
    )


@router.post("/tasks/{task_id}/creative-verify", response_model=CreativeReportItem)
def manual_verify(task_id: UUID, payload: CreativeManualVerify, db: DbSession, user: ManagerUser):
    ct = (
        db.query(CreativeTask)
        .options(joinedload(CreativeTask.task).joinedload(Task.campaign))
        .filter(CreativeTask.taskId == task_id)
        .first()
    )
    if not ct or ct.task.campaign.organizationId != user.organizationId:
        raise HTTPException(status_code=404, detail="Creative task not found")
    if not ct.proofImageKey and not ct.proofImageUrl:
        raise HTTPException(status_code=400, detail="No proof uploaded for this task yet")

    status = CreativeVerificationStatus(payload.status)
    note = (payload.note or "").strip() or None
    ct.detectionMethod = CreativeDetectionMethod.MANUAL
    ct.ocrConfidence = None
    apply_status(ct.task, ct, status, note)
    db.add(
        CreativeVerificationLog(
            taskId=task_id,
            attemptAt=datetime.utcnow(),
            method=CreativeDetectionMethod.MANUAL.value,
            rawText=note,
            detectedCode=ct.detectedCode,
            result=status.value,
            actorUserId=user.id,
        )
    )
    db.commit()
    executor = db.get(User, ct.task.executorUserId)
    return creative_report_item(ct, ct.task, executor)


@router.put("/tasks/{task_id}/creative-code", response_model=CreativeReportItem)
def update_code(task_id: UUID, payload: CreativeCodeUpdate, db: DbSession, user: ManagerUser):
    """Set a task's verification code, or generate one when `code` is omitted."""
    task = (
        db.query(Task)
        .options(joinedload(Task.campaign), joinedload(Task.creative))
        .filter(Task.id == task_id)
        .first()
    )
    if not task or task.campaign.organizationId != user.organizationId:
        raise HTTPException(status_code=404, detail="Task not found")
    if not is_creative(task.campaign.serviceType):
        raise HTTPException(status_code=400, detail="Not a Creative task")

    code = None
    if (payload.code or "").strip():
        prefix = campaign_prefix(task.campaign.name)
        code = normalize(payload.code, prefix)
        # Older CR-XXXX-XXXX codes stay valid; new-style codes must carry the campaign's prefix.
        if not code or not is_valid(code) or (CODE_RE.match(code) and not code.startswith(prefix)):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid code; expected {prefix}-NNNN-NNNN with a valid check digit",
            )
    try:
        ct = set_task_code(db, task, code)
        db.commit()
    except CodeConflict as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc))
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Code is already used by another task")
    if not ct.generatedImageKey:
        enqueue_generation(task.campaignId)
    executor = db.get(User, task.executorUserId)
    return creative_report_item(ct, task, executor)


@router.get("/campaigns/{campaign_id}/creative-print.pdf")
def download_print_pdf(campaign_id: UUID, db: DbSession, user: ManagerUser):
    """All generated creatives, one page each, ready to print."""
    campaign = _creative_campaign(db, user, campaign_id)
    cc: CreativeCampaign = campaign.creative
    if not cc.printPdfKey or cc.generationStatus != CreativeGenerationStatus.COMPLETED:
        raise HTTPException(status_code=409, detail="Print PDF is not ready yet")
    chunks = storage.iter_bytes(cc.printPdfKey)
    if chunks is None:
        raise HTTPException(status_code=404, detail="Print PDF not found; retry generation")
    name = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in campaign.name)[:40] or "creatives"
    return StreamingResponse(
        chunks,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{name}-print.pdf"'},
    )


@router.post("/campaigns/{campaign_id}/creative-verify-now")
def verify_now(campaign_id: UUID, db: DbSession, user: ManagerUser):
    _creative_campaign(db, user, campaign_id)
    pending = (
        db.query(CreativeTask)
        .filter(
            CreativeTask.campaignId == campaign_id,
            CreativeTask.verificationStatus == CreativeVerificationStatus.PENDING_VERIFICATION,
        )
        .count()
    )
    if pending:
        enqueue_campaign_verification(campaign_id)
    return {"ok": True, "queued": pending}


@router.post("/campaigns/{campaign_id}/creative-generate", response_model=CreativeCampaignOut)
def retry_generation(campaign_id: UUID, db: DbSession, user: ManagerUser):
    """Re-run image generation (e.g. after FAILED). Only missing images are rendered."""
    campaign = _creative_campaign(db, user, campaign_id)
    cc: CreativeCampaign = campaign.creative
    if cc.generationStatus != CreativeGenerationStatus.PROCESSING:
        cc.generationStatus = CreativeGenerationStatus.PENDING
        cc.generationError = None
        cc.updatedAt = datetime.utcnow()
        db.commit()
    enqueue_generation(campaign_id)
    return creative_campaign_out(cc)
