"""End-to-end Creative flow through the real routes (SQLite + local storage)."""

import io
import shutil
from uuid import UUID, uuid4

import pytest
from PIL import Image

from app.auth import create_access_token
from app.database import SessionLocal
from app.models import (
    Campaign,
    CampaignMember,
    CampaignRole,
    CreativeCampaign,
    CreativeTask,
    CreativeVerificationLog,
    Organization,
    Task,
    TaskStatus,
    User,
)
from app.services.creative import generate_images
from app.services.creative_codes import code_key, is_valid
from app.services.creative_verification import run_verification
from tests.test_creative_verification import poster, to_jpeg

LAT, LNG = 12.9716, 77.5946


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def world(client):
    db = SessionLocal()
    org = Organization(id=uuid4(), name="Org", type="AGENCY")
    users = {
        name: User(id=uuid4(), organizationId=org.id, firstName=name.title(), phone=uuid4().hex[:10])
        for name in ("manager", "executor", "other")
    }
    seed = Campaign(id=uuid4(), organizationId=org.id, name="seed")
    db.add(org)
    db.flush()
    db.add_all([*users.values(), seed])
    db.flush()
    # A manager is anyone with an active CAMPAIGN_MANAGER membership.
    db.add(
        CampaignMember(
            id=uuid4(),
            campaignId=seed.id,
            userId=users["manager"].id,
            assignedBy=users["manager"].id,
            role=CampaignRole.CAMPAIGN_MANAGER,
        )
    )
    db.commit()
    ids = {name: u.id for name, u in users.items()}
    db.close()
    headers = {
        name: {"Authorization": f"Bearer {create_access_token({'sub': str(uid)})}"}
        for name, uid in ids.items()
    }
    return {"ids": ids, "h": headers}


def upload_source(client, headers, img=None) -> str:
    r = client.post(
        "/api/uploads/presign-creative",
        json={"filename": "ad.png", "content_type": "image/png"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    key = r.json()["storage_key"]
    r = client.post(
        "/api/uploads/local",
        files={"file": ("ad.png", _png(img or poster()), "image/png")},
        data={"storage_key": key},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return key


def create_creative(
    client, world, *, total=3, executors=True, key=None, headers=None, name="Creative test"
):
    key = key or upload_source(client, world["h"]["manager"])
    body = {
        "name": name,
        "service_type": "Creative",
        "center_latitude": LAT,
        "center_longitude": LNG,
        "radius_km": 5,
        "total_tasks": total,
        "executor_ids": [str(world["ids"]["executor"])] if executors else [],
        "source_image_key": key,
    }
    return client.post("/api/campaigns", json=body, headers=headers or world["h"]["manager"])


def creative_rows(campaign_id) -> list[tuple[CreativeTask, Task]]:
    db = SessionLocal()
    try:
        rows = (
            db.query(CreativeTask, Task)
            .join(Task, Task.id == CreativeTask.taskId)
            .filter(CreativeTask.campaignId == UUID(str(campaign_id)))
            .order_by(Task.createdAt.asc())
            .all()
        )
        db.expunge_all()
        return rows
    finally:
        db.close()


def submit_proof(client, headers, task_id, key, url="http://testserver/x.jpg", lat=LAT, lng=LNG):
    return client.post(
        f"/api/tasks/{task_id}/proofs",
        json={"storage_key": key, "url": url, "latitude": lat, "longitude": lng, "slot": "photo_1"},
        headers=headers,
    )


def test_creative_campaign_full_flow(client, world):
    h = world["h"]
    r = create_creative(client, world)
    assert r.status_code == 200, r.text
    campaign = r.json()
    cid = campaign["id"]
    assert campaign["service_type"] == "Creative"
    assert campaign["creative"]["generation_status"] == "PENDING"

    generate_images(UUID(cid))

    rows = creative_rows(cid)
    assert len(rows) == 3
    codes = [ct.verificationCode for ct, _ in rows]
    assert len(set(codes)) == 3 and all(is_valid(c) for c in codes)
    assert all(ct.generatedImageKey for ct, _ in rows)
    assert all(t.executorUserId == world["ids"]["executor"] for _, t in rows)

    detail = client.get(f"/api/campaigns/{cid}", headers=h["manager"]).json()
    assert detail["creative"]["generation_status"] == "COMPLETED"
    assert detail["creative"]["generated_count"] == 3
    assert {t["creative"]["verification_code"] for t in detail["tasks"]} == set(codes)

    (ct1, t1), (ct2, t2), (ct3, t3) = rows
    unrelated = upload_source(client, h["manager"], poster(800, 500))

    # Any of the campaign's creatives may go up at any spot. Task 3 reuses task 1's poster.
    assert submit_proof(client, h["executor"], t1.id, ct1.generatedImageKey).status_code == 200
    assert submit_proof(client, h["executor"], t2.id, ct3.generatedImageKey).status_code == 200
    assert submit_proof(client, h["executor"], t3.id, ct1.generatedImageKey).status_code == 200

    task1 = client.get(f"/api/tasks/{t1.id}", headers=h["executor"]).json()
    assert task1["creative"]["verification_status"] == "PENDING_VERIFICATION"
    assert task1["status"] == "PENDING"

    r = submit_proof(client, h["executor"], t1.id, ct1.generatedImageKey)
    assert r.status_code == 400 and "awaiting verification" in r.json()["detail"]

    summary = run_verification()
    assert summary["verified"] == 2 and summary["failed"] == 0 and summary["needs_review"] == 1

    by_task = {t.id: (ct, t) for ct, t in creative_rows(cid)}
    ct, t = by_task[t1.id]
    assert ct.verificationStatus.value == "VERIFIED" and ct.detectionMethod.value == "QR"
    assert t.status == TaskStatus.ACCEPTED and t.completedAt is not None
    ct, t = by_task[t2.id]
    assert ct.verificationStatus.value == "VERIFIED" and ct.detectedCode == ct3.verificationCode
    ct, _ = by_task[t3.id]
    assert ct.verificationStatus.value == "NEEDS_REVIEW" and not ct.codeMissing
    assert "already submitted for task #1" in ct.verificationNotes

    # Running again is a no-op.
    assert run_verification()["processed"] == 0

    # Verified proofs are locked for executors; ones in review can be re-uploaded.
    assert submit_proof(client, h["executor"], t1.id, ct1.generatedImageKey).status_code == 400
    assert submit_proof(client, h["executor"], t3.id, unrelated).status_code == 200
    assert run_verification(UUID(cid))["needs_review"] == 1

    report = client.get(f"/api/campaigns/{cid}/creative-report", headers=h["manager"])
    assert report.status_code == 200, report.text
    totals = report.json()["totals"]
    assert totals == {
        "total_tasks": 3,
        "not_submitted": 0,
        "pending_verification": 0,
        "verified": 2,
        "failed": 0,
        "needs_review": 1,
        "success_rate": 66.7,
    }
    filtered = client.get(
        f"/api/campaigns/{cid}/creative-report?status=needs_review", headers=h["manager"]
    ).json()
    assert [i["task_id"] for i in filtered["items"]] == [str(t3.id)]
    assert filtered["items"][0]["proof_image_url"]

    r = client.post(
        f"/api/tasks/{t3.id}/creative-verify",
        json={"status": "VERIFIED", "note": "Checked on site"},
        headers=h["manager"],
    )
    assert r.status_code == 200, r.text
    assert r.json()["verification_status"] == "VERIFIED"
    assert r.json()["detection_method"] == "MANUAL"

    db = SessionLocal()
    manual = db.query(CreativeVerificationLog).filter_by(taskId=t3.id, method="MANUAL").one()
    assert manual.actorUserId == world["ids"]["manager"]
    assert db.query(CreativeVerificationLog).filter_by(taskId=t1.id).count() == 1
    db.close()


def test_creative_without_executors_then_assign_and_add_tasks(client, world):
    h = world["h"]
    r = create_creative(client, world, total=2, executors=False)
    assert r.status_code == 200, r.text
    cid = r.json()["id"]

    rows = creative_rows(cid)
    assert len(rows) == 2
    assert all(t.executorUserId == world["ids"]["manager"] for _, t in rows)

    r = client.post(
        f"/api/campaigns/{cid}/executors",
        json={"executor_ids": [str(world["ids"]["executor"])]},
        headers=h["manager"],
    )
    assert r.status_code == 200, r.text
    assert all(t.executorUserId == world["ids"]["executor"] for _, t in creative_rows(cid))

    r = client.post(f"/api/campaigns/{cid}/tasks", json={"count": 2}, headers=h["manager"])
    assert r.status_code == 200, r.text
    assert len(r.json()["task_ids"]) == 2
    rows = creative_rows(cid)
    assert len(rows) == 4 and len({ct.verificationCode for ct, _ in rows}) == 4

    generate_images(UUID(cid))
    assert all(ct.generatedImageKey for ct, _ in creative_rows(cid))


def test_creative_validation_and_permissions(client, world, monkeypatch):
    h = world["h"]
    # Executors can't create campaigns or read reports.
    assert create_creative(client, world, headers=h["executor"]).status_code == 403

    body = {
        "name": "No image",
        "service_type": "Creative",
        "center_latitude": LAT,
        "center_longitude": LNG,
        "total_tasks": 2,
    }
    r = client.post("/api/campaigns", json=body, headers=h["manager"])
    assert r.status_code == 400 and "source image" in r.json()["detail"]

    # Only keys handed out by /api/uploads/presign-creative are accepted.
    r = create_creative(client, world, key="gig-management/executor/someone-elses.jpg")
    assert r.status_code == 400 and "Invalid source image key" in r.json()["detail"]

    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "creative_max_tasks", 5)
    r = create_creative(client, world, total=6)
    assert r.status_code == 400 and "at most 5" in r.json()["detail"]

    cid = create_creative(client, world, total=1).json()["id"]
    assert client.get(f"/api/campaigns/{cid}/creative-report", headers=h["executor"]).status_code == 403

    # Only the assigned executor may upload a proof.
    ct, t = creative_rows(cid)[0]
    assert submit_proof(client, h["other"], t.id, "gig-management/x.jpg").status_code == 403

    # Manual override needs a proof first.
    r = client.post(
        f"/api/tasks/{t.id}/creative-verify", json={"status": "VERIFIED"}, headers=h["manager"]
    )
    assert r.status_code == 400


def test_other_service_types_are_unchanged(client, world):
    h = world["h"]
    body = {
        "name": "Poles",
        "service_type": "Pole Boards",
        "center_latitude": LAT,
        "center_longitude": LNG,
        "total_tasks": 2,
        "executor_ids": [str(world["ids"]["executor"])],
    }
    r = client.post("/api/campaigns", json=body, headers=h["manager"])
    assert r.status_code == 200, r.text
    campaign = r.json()
    assert campaign["creative"] is None
    assert all(t["creative"] is None for t in campaign["tasks"])

    db = SessionLocal()
    assert db.query(CreativeCampaign).filter_by(campaignId=UUID(campaign["id"])).count() == 0
    db.close()

    task_id = campaign["tasks"][0]["id"]
    assert submit_proof(client, h["executor"], task_id, "gig-management/p.jpg").status_code == 200
    assert client.get(f"/api/tasks/{task_id}", headers=h["executor"]).json()["status"] == "ACCEPTED"
    r = submit_proof(client, h["executor"], task_id, "gig-management/p.jpg")
    assert r.status_code == 400 and "already uploaded" in r.json()["detail"]


def test_deleting_campaign_removes_creative_rows(client, world):
    cid = create_creative(client, world, total=2).json()["id"]
    r = client.delete(f"/api/campaigns/{cid}", headers=world["h"]["manager"])
    assert r.status_code == 200, r.text
    assert creative_rows(cid) == []


pytestmark = pytest.mark.skipif(
    shutil.which("tesseract") is None, reason="tesseract binary not installed"
)


def test_background_worker_generates_images(client, world):
    from app.services import creative_jobs

    cid = UUID(create_creative(client, world, total=2).json()["id"])
    creative_jobs.enqueue_generation(cid)
    creative_jobs._work_q.join()
    assert all(ct.generatedImageKey for ct, _ in creative_rows(cid))
    detail = client.get(f"/api/campaigns/{cid}", headers=world["h"]["manager"]).json()
    assert detail["creative"]["generation_status"] == "COMPLETED"


@pytest.mark.skip(reason="Scheduled verification is commented out (app/services/creative_jobs.py)")
def test_scheduled_job_is_registered(monkeypatch):
    from app.config import get_settings
    from app.services import creative_jobs
    from app.services.creative_jobs import describe_schedule, start_scheduler

    settings = get_settings()
    monkeypatch.setattr(settings, "creative_verify_cron", "0 * * * *")
    scheduler = start_scheduler()
    try:
        job = scheduler.get_job("creative-verify")
        assert job.next_run_time.minute == 0
        assert str(job.next_run_time.tzinfo) == "Asia/Kolkata"
        # A tick only queues the work; the worker thread does the checking.
        queued = []
        monkeypatch.setattr(creative_jobs, "_enqueue", lambda kind, item: queued.append((kind, item)))
        job.func()
        assert queued == [("verify_all", None)]
    finally:
        scheduler.shutdown(wait=False)

    for cron, text in [
        ("0 * * * *", "every hour"),
        ("0 */2 * * *", "every 2 hours"),
        ("30 2 * * *", "daily at 02:30 (Asia/Kolkata)"),
    ]:
        monkeypatch.setattr(settings, "creative_verify_cron", cron)
        assert describe_schedule() == text

    monkeypatch.setattr(settings, "creative_verify_cron", "")
    assert start_scheduler() is None
    assert describe_schedule() is None


def test_codes_are_optional_and_managers_can_set_them(client, world, monkeypatch):
    import app.services.creative as creative_service
    from app.services.creative_codes import DIGITS, check_char, format_code

    h = world["h"]

    def broken(_db, _count, _prefix):
        raise RuntimeError("code service down")

    monkeypatch.setattr(creative_service, "_unique_codes", broken)
    r = create_creative(client, world, total=2)
    assert r.status_code == 200, r.text  # task creation is not blocked
    monkeypatch.undo()
    cid = r.json()["id"]
    (ct1, t1), (ct2, t2) = creative_rows(cid)
    assert ct1.verificationCode is None and ct2.verificationCode is None

    # Tasks without a code are skipped by generation and land in review on verification.
    generate_images(UUID(cid))
    assert not any(ct.generatedImageKey for ct, _ in creative_rows(cid))
    detail = client.get(f"/api/tasks/{t1.id}", headers=h["manager"]).json()
    assert detail["creative"]["verification_code"] is None
    unrelated = upload_source(client, h["manager"], poster(800, 500))
    assert submit_proof(client, h["executor"], t1.id, unrelated).status_code == 200
    run_verification(UUID(cid))
    ct, _ = creative_rows(cid)[0]
    assert ct.verificationStatus.value == "NEEDS_REVIEW" and ct.codeMissing

    url = f"/api/tasks/{t1.id}/creative-code"
    assert client.put(url, json={}, headers=h["executor"]).status_code == 403
    assert client.put(url, json={"code": "CR-AAAA-AAAA"}, headers=h["manager"]).status_code == 400

    r = client.put(url, json={}, headers=h["manager"])  # generate
    assert r.status_code == 200, r.text
    generated = r.json()["verification_code"]
    assert is_valid(generated)

    payload = "4827193"
    custom = format_code(payload + check_char(payload, DIGITS), "CR")  # "Creative test"
    r = client.put(f"/api/tasks/{t2.id}/creative-code", json={"code": custom.lower()}, headers=h["manager"])
    assert r.status_code == 200 and r.json()["verification_code"] == custom

    r = client.put(url, json={"code": custom}, headers=h["manager"])
    assert r.status_code == 409

    generate_images(UUID(cid))
    rows = creative_rows(cid)
    assert [ct.verificationCode for ct, _ in rows] == [generated, custom]
    assert all(ct.generatedImageKey for ct, _ in rows)


def test_print_pdf_is_built_after_generation(client, world):
    import re

    h = world["h"]
    r = create_creative(client, world, total=3)
    cid = r.json()["id"]
    url = f"/api/campaigns/{cid}/creative-print.pdf"
    assert r.json()["creative"]["print_pdf_ready"] is False
    assert client.get(url, headers=h["manager"]).status_code == 409

    generate_images(UUID(cid))

    creative = client.get(f"/api/campaigns/{cid}", headers=h["manager"]).json()["creative"]
    assert creative["print_pdf_ready"] is True and creative["print_pdf_pages"] == 3
    assert creative["print_pdf_generated_at"]

    assert client.get(url, headers=h["executor"]).status_code == 403
    r = client.get(url, headers=h["manager"])
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert "attachment" in r.headers["content-disposition"]
    assert r.content.startswith(b"%PDF")
    assert len(re.findall(rb"/Type /Page\b", r.content)) == 3
    # JPEGs are embedded as-is, not re-encoded.
    jpeg = storage_bytes(creative_rows(cid)[0][0].generatedImageKey)
    assert jpeg in r.content

    # A changed code makes the PDF stale until the next generation run rebuilds it.
    task_id = creative_rows(cid)[0][1].id
    client.put(f"/api/tasks/{task_id}/creative-code", json={}, headers=h["manager"])
    assert client.get(url, headers=h["manager"]).status_code == 409
    generate_images(UUID(cid))
    assert client.get(url, headers=h["manager"]).status_code == 200


def storage_bytes(key):
    from app.services.storage import storage

    return storage.get_bytes(key, "", cache=False)


def test_codes_use_campaign_prefix_and_are_unique_across_campaigns(client, world, monkeypatch):
    import app.services.creative_codes as codes_mod

    r = create_creative(client, world, total=2, name="Nike Launch")
    assert r.status_code == 200, r.text
    first = [ct.verificationCode for ct, _ in creative_rows(r.json()["id"])]
    assert all(c.startswith("NI-") and is_valid(c) for c in first)

    # Force the generator to repeat a number another campaign already uses.
    taken = first[0][3:].replace("-", "")
    real = codes_mod.generate_number
    numbers = iter([taken, taken])
    monkeypatch.setattr(codes_mod, "generate_number", lambda: next(numbers, None) or real())
    r = create_creative(client, world, total=1, name="Adidas Run")
    assert r.status_code == 200, r.text
    (ct, _), = creative_rows(r.json()["id"])
    assert ct.verificationCode.startswith("AD-")
    assert code_key(ct.verificationCode) not in {code_key(c) for c in first}

    # A manager can't reuse another campaign's number under a different prefix either.
    url = f"/api/tasks/{ct.taskId}/creative-code"
    r = client.put(url, json={"code": "AD-" + first[1][3:]}, headers=world["h"]["manager"])
    assert r.status_code == 409


def test_manual_code_must_carry_campaign_prefix(client, world):
    from app.services.creative_codes import DIGITS, check_char

    r = create_creative(client, world, total=1, name="Puma Drop")
    (_, t), = creative_rows(r.json()["id"])
    url = f"/api/tasks/{t.id}/creative-code"
    number = "5550123" + check_char("5550123", DIGITS)
    h = world["h"]["manager"]
    r = client.put(url, json={"code": f"XY-{number[:4]}-{number[4:]}"}, headers=h)
    assert r.status_code == 400 and "PU-" in r.json()["detail"]
    r = client.put(url, json={"code": number}, headers=h)  # digits only get the prefix
    assert r.status_code == 200 and r.json()["verification_code"] == f"PU-{number[:4]}-{number[4:]}"


def test_executor_creative_proof_skips_geofence(client, world):
    r = create_creative(client, world, total=1)
    (_, t), = creative_rows(r.json()["id"])
    # ~1,000 km from the campaign centre: accepted as captured, checked later.
    r = submit_proof(client, world["h"]["executor"], t.id, "gig-management/far.jpg", lat=LAT + 9)
    assert r.status_code == 200, r.text


def test_proof_without_any_code_is_flagged_missing(client, world):
    h = world["h"]
    r = create_creative(client, world, total=2)
    cid = r.json()["id"]
    generate_images(UUID(cid))
    (ct1, t1), (ct2, t2) = creative_rows(cid)
    unrelated = upload_source(client, h["manager"], poster(800, 500))
    assert submit_proof(client, h["executor"], t1.id, unrelated).status_code == 200
    assert submit_proof(client, h["executor"], t2.id, ct2.generatedImageKey).status_code == 200
    run_verification(UUID(cid))

    items = {
        i["task_id"]: i
        for i in client.get(f"/api/campaigns/{cid}/creative-report", headers=h["manager"]).json()["items"]
    }
    assert items[str(t1.id)]["code_missing"] is True
    assert items[str(t2.id)]["code_missing"] is False
    detail = client.get(f"/api/campaigns/{cid}", headers=h["manager"]).json()
    by_id = {t["id"]: t["creative"] for t in detail["tasks"]}
    assert by_id[str(t1.id)]["code_missing"] is True

    # A manual verdict keeps the warning; a new photo clears it until the next check.
    client.post(f"/api/tasks/{t1.id}/creative-verify", json={"status": "VERIFIED"}, headers=h["manager"])
    assert creative_rows(cid)[0][0].codeMissing is True
    client.post(f"/api/tasks/{t1.id}/creative-verify", json={"status": "FAILED"}, headers=h["manager"])
    assert submit_proof(client, h["executor"], t1.id, ct1.generatedImageKey).status_code == 200
    assert creative_rows(cid)[0][0].codeMissing is False


def test_creative_from_another_campaign_fails(client, world):
    h = world["h"]
    a = create_creative(client, world, total=1, name="Alpha").json()["id"]
    b = create_creative(client, world, total=1, name="Beta").json()["id"]
    generate_images(UUID(a))
    (ct_a, _), = creative_rows(a)
    (_, t_b), = creative_rows(b)
    assert submit_proof(client, h["executor"], t_b.id, ct_a.generatedImageKey).status_code == 200
    run_verification(UUID(b))
    (ct, t), = creative_rows(b)
    assert ct.verificationStatus.value == "FAILED" and ct.detectedCode == ct_a.verificationCode
    assert "another campaign" in ct.verificationNotes
    assert t.status == TaskStatus.REJECTED

