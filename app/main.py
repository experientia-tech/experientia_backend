from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
# pyrefly: ignore[missing-import]
from sqlalchemy import inspect, text

from app.config import get_settings
from app.database import engine
from app.models import CreativeCampaign, CreativeTask, CreativeVerificationLog
from app.routers import auth, users_brands, campaigns, tasks, uploads, exports, geo, creative
from app.services.creative_jobs import resume_pending_generations
# from app.services.creative_jobs import start_scheduler  # SCHEDULED VERIFICATION (disabled)
from app.services.storage import storage

settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # SCHEDULED VERIFICATION (disabled) — see app/services/creative_jobs.py to re-enable.
    # scheduler = start_scheduler()
    resume_pending_generations()
    yield
    # if scheduler:
    #     scheduler.shutdown(wait=False)


app = FastAPI(title=settings.app_name, version="2.0.0", lifespan=lifespan)

# Allow explicit origins from env, plus any CloudFront default URL used in prod.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_origin_regex=r"https://[a-z0-9.-]+\.cloudfront\.net",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

upload_path = Path(settings.upload_dir)
upload_path.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(upload_path)), name="uploads")

app.include_router(auth.router)
app.include_router(users_brands.router)
app.include_router(campaigns.router)
app.include_router(tasks.router)
app.include_router(uploads.router)
app.include_router(exports.router)
app.include_router(geo.router)
app.include_router(creative.router)


def _ensure_brand_member_table() -> None:
    """Brand supervisors table — not in original Prisma schema."""
    statements = [
        """
        CREATE TABLE IF NOT EXISTS "BrandMember" (
            id UUID PRIMARY KEY,
            "brandId" UUID NOT NULL REFERENCES "Brand"(id) ON DELETE CASCADE,
            "userId" UUID NOT NULL REFERENCES "User"(id) ON DELETE CASCADE,
            "assignedBy" UUID NOT NULL,
            role TEXT NOT NULL DEFAULT 'SUPERVISOR',
            active BOOLEAN NOT NULL DEFAULT TRUE,
            "createdAt" TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        'CREATE INDEX IF NOT EXISTS "BrandMember_brandId_idx" ON "BrandMember" ("brandId")',
        'CREATE INDEX IF NOT EXISTS "BrandMember_userId_idx" ON "BrandMember" ("userId")',
    ]
    try:
        with engine.begin() as conn:
            for stmt in statements:
                conn.execute(text(stmt))
    except Exception as exc:  # noqa: BLE001
        print(f"[experientia] BrandMember table ensure skipped: {exc}")


_ensure_brand_member_table()


def _ensure_campaign_radius_column() -> None:
    if get_settings().is_sqlite:
        return
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    'ALTER TABLE "Campaign" ADD COLUMN IF NOT EXISTS "radiusKm" '
                    'DOUBLE PRECISION NOT NULL DEFAULT 3.0'
                )
            )
    except Exception as exc:  # noqa: BLE001
        print(f"[experientia] Campaign.radiusKm ensure skipped: {exc}")


_ensure_campaign_radius_column()


def _ensure_creative_tables() -> None:
    """Creative side tables — not in original Prisma schema (Campaign/Task stay untouched)."""
    try:
        CreativeCampaign.metadata.create_all(
            engine,
            tables=[
                CreativeCampaign.__table__,
                CreativeTask.__table__,
                CreativeVerificationLog.__table__,
            ],
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[experientia] Creative tables ensure skipped: {exc}")


_ensure_creative_tables()


def _ensure_creative_task_columns() -> None:
    """Columns added to CreativeTask after it first shipped."""
    columns = {
        "codeMissing": "BOOLEAN NOT NULL DEFAULT FALSE",
    }
    try:
        # SQLite has no ADD COLUMN IF NOT EXISTS, so check first (works for both).
        have = {c["name"] for c in inspect(engine).get_columns("CreativeTask")}
        with engine.begin() as conn:
            for name, ddl in columns.items():
                if name not in have:
                    conn.execute(text(f'ALTER TABLE "CreativeTask" ADD COLUMN "{name}" {ddl}'))
    except Exception as exc:  # noqa: BLE001
        print(f"[experientia] CreativeTask columns ensure skipped: {exc}")


_ensure_creative_task_columns()

if settings.use_s3:
    storage.ensure_bucket_cors()


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "app": settings.app_name,
        "schema": "prisma",
        "database": "sqlite" if settings.is_sqlite else "postgres",
        "s3": settings.use_s3,
        "bucket": settings.s3_bucket if settings.use_s3 else None,
    }
