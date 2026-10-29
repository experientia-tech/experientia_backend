"""Background worker for Creative campaigns (image generation and "Verify now").

Same in-process model as pdf_jobs: one daemon thread drains a queue, so jobs never
run on top of each other. Image generation state lives in the DB, so interrupted work
is re-queued on startup.

SCHEDULED VERIFICATION IS DISABLED FOR NOW. The code that checks every pending proof
on a timer (hourly by default) is kept below, commented out. To turn it back on:
  1. Uncomment every block marked "SCHEDULED VERIFICATION" in this file, plus the
     imports it needs (re, ZoneInfo, get_settings).
  2. Uncomment the start_scheduler() lines in app/main.py (lifespan).
  3. Uncomment `verify_schedule=describe_schedule()` and its import in app/routers/creative.py.
  4. Remove the skip on test_scheduled_job_is_registered in tests/test_creative_api.py.
  5. Set CREATIVE_VERIFY_CRON (e.g. "0 * * * *" hourly, "0 */2 * * *" every 2 hours).
How it works: the scheduler only enqueues a "verify_all" job; a run already waiting in
the queue absorbs later ticks. Pending proofs are DB state, so a check missed by a
restart is picked up on the next tick. On Postgres an advisory lock keeps it to one instance.
"""

from __future__ import annotations

import queue
# import re  # SCHEDULED VERIFICATION
import threading
import traceback
from uuid import UUID
# from zoneinfo import ZoneInfo  # SCHEDULED VERIFICATION

# from app.config import get_settings  # SCHEDULED VERIFICATION
from app.database import SessionLocal
from app.services.creative import generate_images, resumable_campaign_ids
from app.services.creative_verification import run_verification, verify_task

_work_q: queue.Queue[tuple[str, UUID | None]] = queue.Queue()
_queued: set[tuple[str, UUID | None]] = set()
_lock = threading.Lock()
_worker_started = False


def _ensure_worker() -> None:
    global _worker_started
    with _lock:
        if _worker_started:
            return
        threading.Thread(target=_worker_loop, daemon=True, name="creative-worker").start()
        _worker_started = True


def _enqueue(kind: str, item_id: UUID | None) -> None:
    _ensure_worker()
    with _lock:
        if (kind, item_id) in _queued:
            return
        _queued.add((kind, item_id))
    _work_q.put((kind, item_id))


def enqueue_generation(campaign_id: UUID) -> None:
    _enqueue("generate", campaign_id)


def enqueue_task_verification(task_id: UUID) -> None:
    _enqueue("verify_task", task_id)


def enqueue_campaign_verification(campaign_id: UUID) -> None:
    _enqueue("verify_campaign", campaign_id)


# --- SCHEDULED VERIFICATION (disabled) ---------------------------------------
# def enqueue_scheduled_verification() -> None:
#     """Every pending proof in every campaign (the scheduled run)."""
#     _enqueue("verify_all", None)


def _worker_loop() -> None:
    while True:
        kind, item_id = _work_q.get()
        with _lock:
            _queued.discard((kind, item_id))
        try:
            if kind == "generate":
                generate_images(item_id)
            elif kind == "verify_task":
                db = SessionLocal()
                try:
                    verify_task(db, item_id)
                finally:
                    db.close()
            elif kind == "verify_campaign":
                run_verification(item_id)
            # SCHEDULED VERIFICATION (disabled)
            # elif kind == "verify_all":
            #     run_verification()
        except Exception:  # noqa: BLE001
            print(f"[experientia] creative job {kind} {item_id} failed")
            traceback.print_exc()
        finally:
            _work_q.task_done()


def resume_pending_generations() -> None:
    try:
        for campaign_id in resumable_campaign_ids():
            enqueue_generation(campaign_id)
    except Exception as exc:  # noqa: BLE001
        print(f"[experientia] creative generation resume skipped: {exc}")


# --- SCHEDULED VERIFICATION (disabled) ---------------------------------------
# def describe_schedule() -> str | None:
#     """Plain-words version of CREATIVE_VERIFY_CRON for the manager UI, or None if disabled."""
#     settings = get_settings()
#     cron = settings.creative_verify_cron.strip()
#     if not cron:
#         return None
#     parts = cron.split()
#     if len(parts) == 5 and parts[2:] == ["*", "*", "*"] and parts[0].isdigit():
#         minute, hour = int(parts[0]), parts[1]
#         if hour == "*":
#             return "every hour" if minute == 0 else f"every hour at :{minute:02d}"
#         if m := re.fullmatch(r"\*/(\d+)", hour):
#             return "every hour" if m[1] == "1" else f"every {m[1]} hours"
#         if hour.isdigit():
#             return f"daily at {int(hour):02d}:{minute:02d} ({settings.creative_verify_timezone})"
#     return f"on schedule {cron} ({settings.creative_verify_timezone})"
#
#
# def start_scheduler():
#     """Register the scheduled verification job. Returns the scheduler, or None if disabled."""
#     settings = get_settings()
#     cron = settings.creative_verify_cron.strip()
#     if not cron:
#         return None
#     from apscheduler.schedulers.background import BackgroundScheduler
#     from apscheduler.triggers.cron import CronTrigger
#
#     try:
#         tz = ZoneInfo(settings.creative_verify_timezone)
#         scheduler = BackgroundScheduler(timezone=tz)
#         scheduler.add_job(
#             enqueue_scheduled_verification,
#             CronTrigger.from_crontab(cron, timezone=tz),
#             id="creative-verify",
#             max_instances=1,
#             coalesce=True,
#             misfire_grace_time=3600,
#         )
#         scheduler.start()
#     except Exception as exc:  # noqa: BLE001
#         print(f"[experientia] creative verify scheduler not started: {exc}")
#         return None
#     print(f"[experientia] creative verify scheduled '{cron}' ({settings.creative_verify_timezone})")
#     return scheduler
