# Experientia API (Backend)

FastAPI service behind the Experientia campaign operations app. Campaign managers create geofenced outdoor-branding campaigns. Field executors capture GPS-tagged proof photos for each task. Brand supervisors get read-only access and PDF exports.

The React client lives in [`../experientia_frontend`](../experientia_frontend/FRONTEND.md).

---

## 1. Tech stack

| Concern | Choice |
|---|---|
| Framework | FastAPI (`app/main.py`), served by Uvicorn |
| ORM / DB | SQLAlchemy 2.x on **PostgreSQL** (psycopg2). JSONB and native UUID columns are used. |
| Settings | `pydantic-settings` reading env vars / `.env` (`app/config.py`) |
| Auth | Phone + OTP login, JWT bearer tokens (`python-jose`, HS256, 7-day expiry) |
| Storage | AWS S3 via `boto3` (private bucket), local disk fallback under `uploads/` |
| PDF | ReportLab + Pillow (`app/services/pdf.py`) |
| Geocoding | OpenStreetMap Nominatim (server-side proxy) |
| Static maps in PDFs | `staticmap.openstreetmap.de`, with a generated placeholder if it is unreachable |
| Runtime | Python 3.12 (Docker `python:3.12-slim`) |

---

## 2. Directory layout

```
experientia_backend/
├── app/
│   ├── main.py            # App factory, CORS, router wiring, startup DDL, /api/health
│   ├── config.py          # Settings (env vars, aliases, derived flags)
│   ├── database.py        # Engine, SessionLocal, Base, get_db dependency
│   ├── models.py          # ORM models mapped onto the existing Prisma schema
│   ├── schemas.py         # Pydantic request/response DTOs
│   ├── serializers.py     # ORM → DTO conversion (task_out, campaign_out, …)
│   ├── auth.py            # JWT, OTP check, role resolution, permission deps
│   ├── media_rules.py     # Service types → photo slots, detail forms, radius
│   ├── routers/
│   │   ├── auth.py        # /api/auth/*
│   │   ├── users_brands.py# /api/users/*, /api/team, /api/brands
│   │   ├── campaigns.py   # /api/campaigns/* (CRUD, members, tasks, exports)
│   │   ├── tasks.py       # /api/tasks/*, /api/my/*
│   │   ├── uploads.py     # /api/uploads/* (presign, proxy upload, image view)
│   │   ├── exports.py     # /api/exports (Downloads list)
│   │   └── geo.py         # /api/geo/pincode/{pin}
│   └── services/
│       ├── storage.py     # S3/local storage abstraction (singleton `storage`)
│       ├── geo.py         # Haversine, random points in radius, geofence test
│       ├── pdf.py         # Campaign/task report rendering, ZIP chunking
│       └── pdf_jobs.py    # In-memory background export queue (1 worker thread)
├── Dockerfile
├── requirements.txt
├── .env.example
└── .github/workflows/deploy-ecs-express.yml
```

There are no tests, migrations or seed scripts in this repo.

---

## 3. Running locally

**Prerequisite:** a PostgreSQL database that **already has the Prisma schema** (`Organization`, `User`, `Brand`, `Campaign`, `CampaignMember`, `Task`, plus the `CampaignRole` / `TaskStatus` enums). This service does not create those tables. It also needs at least one user. See [§6](#6-data-model).

```bash
cd experientia_backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # set DATABASE_URL and SECRET_KEY at minimum
uvicorn app.main:app --reload --port 8000
```

- Health check: `GET http://localhost:8000/api/health`
- OpenAPI docs: `http://localhost:8000/docs`
- The frontend dev server (port 5173) proxies `/api` and `/uploads` to `127.0.0.1:8000`.

When the S3 settings are blank, files are written to `./uploads/` and served from `/uploads/*` (a `StaticFiles` mount) and `/api/uploads/file/*`.

> The default `DATABASE_URL` is `sqlite:///./experientia.db`, but the models use PostgreSQL-only `UUID`/`JSONB` types and nothing creates the tables. Treat Postgres as required.

---

## 4. Configuration

All settings are in `app/config.py`. Many accept two env-var spellings (`AliasChoices`), shown here as `PRIMARY / alias`.

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./experientia.db` | Postgres DSN. Any `?sslmode=…` query is stripped and `sslmode=require` is forced for Postgres. |
| `SECRET_KEY` | dev placeholder | JWT signing key. **Must be set in prod.** |
| `MANAGER_OTP` | `325476` | Static login OTP for campaign managers |
| `EXECUTOR_OTP` | `123456` | Static login OTP for everyone else (executors and supervisors) |
| `CORS_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173` | Comma-separated allowed origins. `https://*.cloudfront.net` is always allowed by regex. |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | Absolute API origin used to build image proxy URLs and the upload proxy URL |
| `AWS_ACCESS_KEY_ID / ACCESS_KEY_AWS_ID` | – | S3 credentials |
| `AWS_SECRET_ACCESS_KEY / SECRET_KEY_AWS` | – | S3 credentials |
| `AWS_REGION / REGION_AWS` | `ap-south-1` | S3 region |
| `S3_BUCKET / S3_BUCKET_NAME` | – | Bucket. S3 is enabled only when the bucket **and** access key are both set (`settings.use_s3`). |
| `S3_BROWSER_UPLOAD` | `false` | `true` returns real presigned PUT URLs. `false` routes uploads through `/api/uploads/local`. |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | `10` / `20` | SQLAlchemy pool (Postgres only, `pool_recycle=1800`, `pool_pre_ping`) |
| `s3_endpoint_url`, `upload_dir`, `access_token_expire_minutes` | – | Not aliased. Set via lowercase env names if needed. |
| `SEED_DEMO`, `CREATE_TABLES` | `false` | **Declared but not used anywhere** |

---

## 5. Startup behaviour (`app/main.py`)

These steps run at import time, in this order:

1. CORS middleware: explicit origins plus a CloudFront regex, with credentials allowed.
2. The `uploads/` directory is created and mounted at `/uploads`.
3. All routers are registered.
4. `_ensure_brand_member_table()` runs `CREATE TABLE IF NOT EXISTS "BrandMember"` plus indexes. This table is not in the Prisma schema.
5. `_ensure_campaign_radius_column()` runs `ALTER TABLE "Campaign" ADD COLUMN IF NOT EXISTS "radiusKm" … DEFAULT 3.0` (skipped on SQLite).
6. If S3 is enabled, `storage.ensure_bucket_cors()` merges `CORS_ORIGINS` and localhost origins into the bucket CORS rules.

Steps 4–6 log and continue if they fail. They are the only schema changes this service makes.

---

## 6. Data model

The tables are **owned by an external Prisma application**. Table and column names are PascalCase/camelCase (for example `"Task"."executorUserId"`), and the ORM attributes follow them directly. Enums are mapped with `create_type=False`.

```
Organization 1─* User
Organization 1─* Brand 1─* BrandMember *─1 User       (brand supervisors)
Organization 1─* Campaign *─1 Brand (optional)
Campaign 1─* CampaignMember *─1 User                    (role per campaign)
Campaign 1─* Task *─1 User (executorUserId)
```

| Model | Key fields / notes |
|---|---|
| `Organization` | `name`, `type`, `isActive`. Tenancy boundary: almost every query filters on `organizationId`. |
| `User` | `firstName`, `lastName`, `phone` (login identifier), `isActive`, `lastLoginAt`. **Users have no role column.** Roles come from memberships (see §7). |
| `Brand` | `name`, `description`, `image` (S3 key or URL), `isActive` |
| `BrandMember` | `brandId`, `userId`, `assignedBy`, `role='SUPERVISOR'`, `active`. Created at startup if missing. |
| `Campaign` | `name`, `description`, `status` (free text: `ACTIVE`/`COMPLETED`/`CANCELLED`/`DRAFT`), `latitude`/`longitude` (geofence center), `radiusKm`, `address`, `serviceType`, `logo`, `totalTasks`, `startDate`/`endDate`, `isActive`, `brandId` |
| `CampaignMember` | `campaignId`, `userId`, `role` ∈ `CAMPAIGN_MANAGER, SUPERVISOR, EXECUTOR, BRAND_VIEWER`, `assignedBy`, `location`, `active` |
| `Task` | `campaignId`, `executorUserId`, `status` ∈ `PENDING, ACCEPTED, REJECTED`, `startedAt`, `completedAt`, `flagged`, `notes`, `rejectionReason`, **`metadata` (JSONB)** |

### `Task.metadata` JSON shape

Most task state lives in this column. It is accessed as `task.metadata_`. Always deep-copy it and call `flag_modified` when mutating (see `_set_meta` in `routers/tasks.py`).

```jsonc
{
  "sequenceNumber": 12,                         // "Task #12" (1-based, per campaign)
  "target":   { "latitude": 0.0, "longitude": 0.0 },   // generated point inside geofence
  "location": { "latitude": 0.0, "longitude": 0.0, "accuracy": null }, // last capture GPS
  "images": [
    { "url": "...", "key": "gig-management/executor/<hex>.jpg",
      "slot": "front", "latitude": 0.0, "longitude": 0.0, "capturedAt": "ISO-8601" }
    // legacy rows may be plain URL strings
  ],
  "driver": { "name": "", "phone": "", "vehicleNumber": "" },  // Auto Hood
  "gym":    { "name": "", "location": "" },                    // Gym
  "managerOwned": true                                          // tasks a manager created for themself
}
```

`ACCEPTED` is the UI's "Completed" state. Nothing in the API sets `REJECTED`.

---

## 7. Roles and permissions

Roles are **derived on every request** from membership rows (`app/auth.py`):

| Role | Rule |
|---|---|
| `manager` | User has any active `CampaignMember` row with role `CAMPAIGN_MANAGER` |
| `supervisor` | Not a manager, and has an active `BrandMember` row **or** an active `SUPERVISOR` campaign membership |
| `executor` | Everyone else |

The role is also put into the JWT (`role` claim), but the server re-resolves it from the DB and never trusts the claim.

FastAPI dependency aliases:

- `CurrentUser`: any authenticated, active user.
- `ManagerUser`: 403 unless the user is a manager.
- `ExportUser`: manager or supervisor.
- `DbSession`: request-scoped SQLAlchemy session.

Visibility rules:

- **Campaign view** (`can_view_campaign`): same organization, and at least one of: manager; supervisor of the campaign's brand; active campaign member; assigned to at least one task in it.
- **Campaign task list**: managers and supervisors see all tasks. Executors see only their own.
- **Task edit** (`_can_edit_task`): supervisors never. Managers always. Executors only for their own tasks.
- **Proof rules**: executors must fill slots **in order**, cannot overwrite a filled slot, and must be **inside the geofence** (radius + 50 m tolerance). Managers can fill or replace any slot and skip the geofence check. Creative proofs also skip the geofence check — they are accepted as captured and verified later by their code.

### Login

`POST /api/auth/login/json {phone, otp}`:

1. `get_user_by_phone` strips non-digits and matches on the full number or the **last 10 digits**, across all active users.
2. The OTP is compared to `MANAGER_OTP` or `EXECUTOR_OTP` depending on the resolved role. There is no SMS; the OTPs are static.
3. `lastLoginAt` is updated and a JWT `{sub: user_id, role, exp}` is returned.

---

## 8. Domain rules (`app/media_rules.py`)

| Service type | Photos | Capture slots (in order) | Extra form |
|---|---|---|---|
| Auto Hood | 3 | `front`, `side`, `back` | driver (name, phone, vehicle no.) |
| Gym | 2 | `photo_1`, `photo_2` | gym (name, location) |
| No Parking Boards | 1 | `photo_1` | – |
| Pole Boards | 1 | `photo_1` | – |
| Shop Branding | 1 | `photo_1` | – |
| Other | 3 | `photo_1..3` | – |

- `normalize_service_type()` maps free-text DB values to these names (case-insensitive, with fuzzy keyword matching). Unknown values become `Other`.
- `campaign_radius_km()` uses `Campaign.radiusKm`, or 3.0 km when it is missing or invalid.
- **Auto-completion** (`_maybe_complete` in `routers/tasks.py`): a task becomes `ACCEPTED` with `completedAt` set once every slot is filled **and** the detail form, if required, is complete. Deleting a proof reverts it to `PENDING`.

> These tables are duplicated in the frontend (`src/types.ts`). Keep them in sync.

---

## 9. API reference

All routes are prefixed with `/api`. "Auth" lists the minimum required role.

### Health and auth

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/health` | – | App, DB kind, S3 status |
| POST | `/auth/login` | – | OAuth2 form (`username`=phone, `password`=OTP) |
| POST | `/auth/login/json` | – | `{phone, otp}` → `{access_token, token_type}` |
| GET | `/auth/me` | user | `UserOut` with resolved `role` |

### Team and brands (`routers/users_brands.py`)

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/users/executors` | manager | Org users who are executors on any campaign, excluding brand supervisors. Falls back to all non-supervisors. |
| GET | `/users/supervisors?brand_id=` | manager | Active brand supervisors (optionally for one brand) |
| GET | `/team` | manager | All org users with derived role and brand |
| POST | `/team` | manager | Create or reactivate a user. `role` is `executor` or `supervisor`. Supervisors need `brand_id`; they get a `BrandMember` row and are attached as `SUPERVISOR` to every active campaign of that brand. Phone is normalized to 10 digits (strips `+91` / leading `0`). |
| GET | `/brands` | user | Active org brands. `image` is returned as a proxied view URL. |
| POST | `/brands` | manager | `{name, description?, storage_key? / image?}` |
| PATCH | `/brands/{id}` | manager | Partial update. A new logo key replaces the old one. |

There are no delete or deactivate endpoints for users or brands.

### Campaigns (`routers/campaigns.py`)

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/campaigns` | user | List with aggregate task counts (one `GROUP BY` query). Non-managers see only the campaigns they can access. |
| POST | `/campaigns` | manager | Creates the campaign, adds the caller as `CAMPAIGN_MANAGER`, adds executors, **generates `total_tasks` random target points** inside the radius (round-robin over executors), and attaches brand supervisors. |
| GET | `/campaigns/{id}?page&limit&status&q&executor_id` | viewer | Campaign detail with a **paginated** `tasks` page (default 24, max 10000). `q` matches task UUID or sequence number and is filtered in Python. |
| PATCH | `/campaigns/{id}` | manager | Partial update. Changing the brand copies the brand logo and re-attaches brand supervisors. `status != ACTIVE` sets `isActive=false`, which hides the campaign from lists. |
| DELETE | `/campaigns/{id}` | manager | Hard delete; tasks and members cascade |
| POST | `/campaigns/{id}/tasks` | manager | `{count≤500, executor_user_id?, manager_owned?}` adds tasks after the current max sequence. `manager_owned` assigns the tasks to the caller. Returns `{campaign, task_ids}`. |
| POST | `/campaigns/{id}/executors` | manager | Syncs the executor member set. If the campaign has no tasks, generates them. **Otherwise reassigns every existing task round-robin**, including completed and manager-owned tasks. |
| POST | `/campaigns/{id}/supervisors` | manager | Syncs the supervisor set. Supervisors must be `BrandMember`s of the campaign's brand. |
| POST | `/campaigns/{id}/export` | export | Queues a background export and returns the job |
| GET | `/campaigns/{id}/export/jobs` | export | Caller's last 10 jobs for this campaign |
| GET | `/campaigns/{id}/export/jobs/{job}` | export | Job status |
| GET | `/campaigns/{id}/export/jobs/{job}/download` | export | `FileResponse`. 409 if not ready, 410 if the file expired. |
| GET | `/campaigns/{id}/export.pdf` | export | Legacy synchronous PDF. Rejected for campaigns with 100 or more tasks. |

### Tasks (`routers/tasks.py`)

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/tasks/{id}` | viewer | Full task with all images. **Writes on read**: backfills `sequenceNumber` and missing per-image GPS. |
| POST | `/tasks/{id}/details` | editor | Driver or gym form. Can auto-complete the task. |
| POST | `/tasks/{id}/proofs` | editor | `{storage_key, url, latitude, longitude, slot, captured_at?}`. Applies slot order and geofence rules (§7). Can auto-complete the task. |
| DELETE | `/tasks/{id}/proofs/{slot}` | editor | Removes the slot and may revert the task to `PENDING` |
| GET | `/tasks/{id}/export.pdf` | manager | Single-task PDF (not used by the frontend) |
| GET | `/my/campaigns` | user | Executor's campaigns with task and pending counts |
| GET | `/my/tasks?campaign_id&page&limit` | user | Executor's tasks, paginated. When `campaign_id` is given it also returns `next_open_task_id`. |

### Uploads (`routers/uploads.py`)

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/uploads/presign` | task owner / manager | Returns `{upload_url, storage_key, public_url, use_direct_put}`. Key: `gig-management/executor/<uuid>.<ext>`. |
| POST | `/uploads/presign-brand` | manager | Key: `gig-management/brands/<brandId|new>/<uuid>.<ext>` |
| POST | `/uploads/local` | user | Multipart `file` + `storage_key` (max 25 MB). Puts to S3 when enabled, otherwise to local disk. |
| GET | `/uploads/view?key=` | **none** | Streams a private S3 object. Only `gig-management/` and `proofs/` prefixes are allowed. Sends `Access-Control-Allow-Origin: *`. |
| GET | `/uploads/file/{path}` | **none** | Serves a local-disk file |

### Misc

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/exports` | export | The caller's export jobs across campaigns (latest 50), used by the Downloads page |
| GET | `/geo/pincode/{pin}` | **none** | Nominatim lookup for a 6-digit Indian pincode → `{lat, lng, label}`. Not currently called by the frontend. |

---

## 10. Key flows

### Proof photo upload

```
client → POST /uploads/presign {task_id, filename}
       ← {upload_url, storage_key, public_url, use_direct_put}
client → PUT upload_url              (only if use_direct_put; S3_BROWSER_UPLOAD=true)
       or POST /uploads/local (multipart)  (default — server writes to S3)
client → POST /tasks/{id}/proofs {storage_key, url, lat, lng, slot}
       → slot-order + geofence validation → metadata.images += entry → maybe ACCEPTED
```

### Image delivery

The bucket is private. `storage.viewable_url()` turns any stored URL or key into `${PUBLIC_BASE_URL}/api/uploads/view?key=…`. The absolute origin is needed because the SPA is on a different origin (CloudFront), where a relative `/api` path would fall back to `index.html`. `get_bytes()` keeps a per-process LRU-ish cache of up to 64 objects.

### Background PDF export (`services/pdf_jobs.py` + `services/pdf.py`)

- `start_export_job` adds a job to an in-memory dict and a `queue.Queue`. One daemon thread (`pdf-export-worker`) processes jobs **one at a time**, using its own DB session.
- Under 100 tasks the export is a single PDF. With 100 or more it is a **ZIP of PDFs in chunks of 80 tasks** (`CHUNK_SIZE`, `ZIP_THRESHOLD`) plus a `README.txt`. Part 1 has the cover.
- Report layout: a cover page (header, brand logo, static OSM map with up to 45 pins, stat cards for service, locations, verified and completion %), then one page per task with photos, coordinates, a map link and driver/gym details.
- Output is written to `uploads/pdf-exports/`. Jobs and files are purged **12 h** after they finish (lazily, when jobs are listed or fetched).
- Job state is lost on restart and is **not shared between processes or containers** (see §12).

---

## 11. Deployment

`.github/workflows/deploy-ecs-express.yml` runs on every push to `main` or on manual dispatch:

1. Builds the Docker image and pushes it to ECR repo `experientia-api`, tagged with the commit SHA and `latest`.
2. Builds the container env from GitHub secrets: `DATABASE_URL`, `SECRET_KEY`, `MANAGER_OTP`, `EXECUTOR_OTP`, `APP_AWS_ACCESS_KEY_ID`, `APP_AWS_SECRET_ACCESS_KEY`, `S3_BUCKET`, and optionally `PUBLIC_BASE_URL` and `CORS_ORIGINS`. It forces `S3_BROWSER_UPLOAD=false`.
3. Deploys with `aws-actions/amazon-ecs-deploy-express-service` to cluster `experientia`, service `experientia-api`, region `ap-south-1`: 2 vCPU / 4 GB, health check `/api/health`, 1–2 tasks autoscaled on CPU at 70%.

The Dockerfile runs as non-root `appuser` with a **single Uvicorn worker** (`--proxy-headers`, keep-alive 65 s). Its header comment still says "App Runner"; the active target is ECS Express.

If the API endpoint URL changes, the frontend has to be rebuilt with the new `VITE_API_URL`.

---

## 12. Known issues and risks

Ordered roughly by impact.

**Security**

- **Static OTPs.** Anyone who knows a registered phone number and the shared OTP can sign in. SMS delivery has not been built.
- **`POST /uploads/local` accepts any `storage_key`** from any authenticated user (only `..` and leading `/` are rejected). A user could overwrite an arbitrary object in the bucket.
- **`GET /uploads/view` and `/uploads/file/*` are unauthenticated.** Anyone with a key can fetch proof photos and logos. Keys are random UUIDs, but they appear in API responses.
- `GET /geo/pincode` is unauthenticated and can be used as an open Nominatim proxy.
- `get_user_by_phone` matches on the last 10 digits **across organizations**.

**Scaling and correctness**

- **Export jobs live in process memory, but ECS can scale to 2 tasks.** Status polling or downloads routed to the other task return 404. Either set the max task count to 1, add sticky sessions, or move jobs and files to shared storage (DB plus S3).
- `get_user_by_phone` loads **every active user** on each login (a full scan).
- Role helpers (`user_is_manager` and others) run several queries per request and are called repeatedly in one handler. They are not cached.
- `q` search in campaign detail loads all matching tasks into memory.
- `POST /campaigns/{id}/executors` reassigns **all** existing tasks, including completed and manager-owned ones.
- `GET /tasks/{id}` writes to the DB (backfills).
- `/team` and `/users/executors` read `BrandMember` and `CampaignMember` rows without an org filter. The results are still intersected with org users, so they are correct but wasteful.

**Maintainability**

- No tests, no migrations, no linting config.
- Schema changes are ad-hoc DDL in `main.py` at import time, against a schema owned by another (Prisma) codebase.
- Unused settings: `SEED_DEMO`, `CREATE_TABLES`. Dead code: the `legacy` variable in `_photos_done` and the no-op loop in `create_team_member`.
- `datetime.utcnow()` is deprecated in Python 3.12, and naive vs aware datetimes are mixed (some columns are `timezone=True`, others are not).
- `staticmap.openstreetmap.de` is a third-party service with no SLA. PDFs and the "Show All Maps" toggle depend on it.
