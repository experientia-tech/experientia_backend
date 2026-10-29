# Feature: "Creative" Service Type with Unique Code Verification

## Role & ground rules

You are working inside an existing backend codebase for a campaign/task platform. Before writing any code:

1. **Explore the codebase first.** Find how campaigns, service types, tasks, executors, file uploads, storage, background jobs/cron, and reports are currently implemented. Identify the language, framework, ORM/database, storage provider (S3/local/etc.), and job scheduler already in use.
2. **Follow existing patterns.** Reuse the existing folder structure, naming conventions, validation style, error handling, auth/role checks, response format, and migration approach. Do not introduce a new framework or architecture if an equivalent already exists.
3. **Do not break existing service types.** All current campaign/task flows must keep working unchanged.
4. **Before implementing, reply with a short plan:** the files you will create/modify, the schema changes, and any new dependencies. Then implement step by step.

---

## Goal

Add a new campaign service type called **`CREATIVE`**.

- A **campaign manager** creates a Creative campaign by uploading one image and choosing the **number of tasks (N)**.
- The system generates **N unique copies** of that image, each with a **unique verification code** (human-readable text + QR code) placed in a strip at the bottom of the image, and creates **N tasks**, one per copy.
- An **executor** is assigned a task, displays/places that creative in the real world, takes a photo of it, and uploads the photo as proof.
- A **scheduled job (nightly)** reads the code from each uploaded proof photo using **QR decoding first, OCR as fallback**, checks that it matches the code assigned to that task, and updates the task status.
- Campaign managers can view a **verification report** per campaign.

---

## 1. Data model changes

Adapt names/types to the existing ORM and conventions.

**Service type**
- Add `CREATIVE` to the existing service type enum/table.

**Campaign (Creative-specific fields)**
- `sourceImageUrl` – original uploaded image
- `taskCount` – N
- `creativeCodes` – array of generated codes (also stored per task; this array is for quick lookup/reporting)
- `generationStatus` – `PENDING | PROCESSING | COMPLETED | FAILED`

**Task (Creative-specific fields)**
- `verificationCode` – unique code for this task (unique index, globally)
- `generatedImageUrl` – the image copy with code + QR stamped on it
- `proofImageUrl` – photo uploaded by the executor
- `proofUploadedAt`
- `verificationStatus` – `NOT_SUBMITTED | PENDING_VERIFICATION | VERIFIED | FAILED | NEEDS_REVIEW`
- `detectedCode` – what QR/OCR actually read
- `detectionMethod` – `QR | OCR | MANUAL`
- `ocrConfidence` – number, nullable
- `verificationAttempts` – integer
- `verifiedAt`
- `verificationNotes` – reason for failure / review

Prefer a separate `CreativeVerificationLog` table (taskId, attemptAt, method, rawText, detectedCode, result, error) if the codebase keeps audit logs; it makes debugging OCR far easier.

Write a proper migration. Do not edit existing migrations.

---

## 2. Unique code design

Codes must be easy for OCR to read and resistant to misreads:

- Format: `CR-XXXX-XXXX` (prefix + 8 characters), e.g. `CR-7KQ4-M2HX`.
- Alphabet: **Crockford Base32 without ambiguous characters** — exclude `0, O, 1, I, L, U`.
- Last character is a **checksum** so a misread code is detected rather than matched to the wrong task.
- Generate with a cryptographically secure random source; check uniqueness against the DB (unique index + retry on collision).
- Put code generation, normalization (uppercase, strip spaces, map common OCR confusions like `O→0`-style corrections only where the alphabet allows), and checksum validation in **one utility module** with unit tests.

---

## 3. Campaign creation flow (manager)

**Endpoint:** extend the existing create-campaign endpoint (or add one following existing conventions) to accept, when `serviceType = CREATIVE`:
- `image` (multipart file) — validate type (jpg/png/webp) and max size
- `taskCount` — integer, validate min 1 and a sensible configurable max (e.g. 1000)

**Steps:**
1. Validate input and manager permissions (reuse existing auth/role middleware).
2. Upload the source image to existing storage.
3. Save the campaign with `generationStatus = PENDING`.
4. **Generate images asynchronously** using the existing job/queue system (if none exists, use a simple in-process background job and note it). Large N must not block the HTTP request.
5. For each of N tasks:
   - Generate a unique code.
   - Render a new image: original image + a white strip at the bottom containing the **QR code (encoding the code)** and the **code in large, bold, monospace, high-contrast text**. Keep a quiet zone around the QR. Ensure text size scales with image width so it stays readable in photos.
   - Upload the generated image to storage.
   - Create the task with `verificationCode`, `generatedImageUrl`, `verificationStatus = NOT_SUBMITTED`.
   - Push the code into the campaign's `creativeCodes` array.
6. Wrap DB writes in a transaction where possible; on failure mark `generationStatus = FAILED` and clean up partial uploads/tasks. Make the job **idempotent** so a retry doesn't create duplicates.
7. Set `generationStatus = COMPLETED`.

Suggested libraries (use whatever fits the stack; check what's already installed first):
- Image composition: `sharp` (Node) / `Pillow` (Python)
- QR generation: `qrcode` (Node or Python)

---

## 4. Proof submission flow (executor)

**Endpoint:** e.g. `POST /tasks/:taskId/creative-proof` (match existing route style)
- Only the executor assigned to that task can upload.
- Validate file type and size.
- Upload to storage, set `proofImageUrl`, `proofUploadedAt`, `verificationStatus = PENDING_VERIFICATION`.
- Allow re-upload only if status is `NOT_SUBMITTED`, `FAILED`, or `NEEDS_REVIEW` (configurable max attempts).
- Optional (behind a config flag `CREATIVE_VERIFY_ON_UPLOAD`): enqueue an immediate verification for this one task so the executor gets fast feedback; the nightly job remains the safety net.

---

## 5. Nightly verification job

**Schedule:** daily (default 02:00, configurable via env, e.g. `CREATIVE_VERIFY_CRON="0 2 * * *"`). Register it with the existing scheduler.

**Logic:**
1. Acquire a **lock** so two instances never run the job at the same time.
2. Fetch tasks where `verificationStatus = PENDING_VERIFICATION`, in **batches** (e.g. 50), with limited concurrency.
3. For each task:
   1. Download the proof image.
   2. **Preprocess:** auto-rotate via EXIF, resize, grayscale, increase contrast; optionally crop the bottom portion as a second attempt.
   3. **Try QR decode first** (e.g. `jsQR`/`zxing` in Node, `pyzbar`/`opencv` in Python). QR is far more reliable than OCR.
   4. **If no QR found, run OCR** (e.g. `tesseract.js` / `pytesseract`, or a cloud OCR like Google Vision / AWS Textract if already used in the project). Restrict OCR to the code alphabet where the engine supports a whitelist.
   5. Extract candidates with a regex for the `CR-XXXX-XXXX` pattern, normalize, validate checksum.
   6. **Match against the task's own `verificationCode`**, not just any code in the campaign.
4. Set the result:
   - Exact match → `VERIFIED`, set `verifiedAt`, `detectedCode`, `detectionMethod`.
   - A valid code found but it belongs to **another task** → `FAILED`, note "code belongs to task X" (prevents executors reusing someone else's photo).
   - Code already verified on a different submission / duplicate photo → `FAILED` or `NEEDS_REVIEW`.
   - No code found or low OCR confidence → `NEEDS_REVIEW` (manager can verify manually).
   - Processing error (download failed, etc.) → leave as `PENDING_VERIFICATION`, increment `verificationAttempts`; after a configurable limit move to `NEEDS_REVIEW`.
5. Write a verification log entry for every attempt.
6. At the end, log a summary (processed / verified / failed / needs review) and, if the project has notifications, notify managers per campaign.

The job must be **idempotent** and safe to re-run. Put OCR/QR logic in its own service module (e.g. `creativeVerification.service`) so it can be called from both the cron job and the optional on-upload path, and so the engine can be swapped later.

---

## 6. Report & manual review

**Endpoints (manager only):**
- `GET /campaigns/:id/creative-report` → totals (total tasks, not submitted, pending, verified, failed, needs review, success rate %) plus a per-task list: task id, executor, code, status, detected code, method, confidence, proof image URL, verified at, notes. Support pagination and status filter.
- `POST /tasks/:taskId/creative-verify` → manual override (`VERIFIED` / `FAILED` with a note), sets `detectionMethod = MANUAL`, logged with the manager's id.
- Optional: `POST /campaigns/:id/creative-verify-now` to trigger verification for one campaign on demand.

---

## 7. Configuration

Add to the existing config/env handling with sensible defaults:
- `CREATIVE_MAX_TASKS`
- `CREATIVE_VERIFY_CRON`
- `CREATIVE_VERIFY_BATCH_SIZE`
- `CREATIVE_VERIFY_MAX_ATTEMPTS`
- `CREATIVE_OCR_MIN_CONFIDENCE`
- `CREATIVE_VERIFY_ON_UPLOAD`
- `CREATIVE_OCR_PROVIDER` (`tesseract` | cloud provider, if applicable)

---

## 8. Tests

- Unit: code generation (format, alphabet, checksum, uniqueness), normalization, regex extraction, matching rules (own code / other task's code / invalid / missing).
- Unit: image generation produces an image whose QR decodes back to the same code.
- Integration: create Creative campaign → N tasks with unique codes and images; executor uploads proof → status becomes `PENDING_VERIFICATION`; run the verification job against a generated image → `VERIFIED`; against an unrelated image → `NEEDS_REVIEW`; against another task's image → `FAILED`.
- Authorization: non-managers can't create/report; executors can only upload to their own tasks.

---

## 9. Deliverables

1. Short plan (files, schema, dependencies) before coding.
2. Migration(s) and model updates.
3. Code utility, image generation service, verification service, cron job registration.
4. Controllers/routes with validation and auth.
5. Tests.
6. A brief summary at the end: new endpoints with example request/response, env vars, and how to run the job manually.
