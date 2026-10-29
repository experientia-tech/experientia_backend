"""Test setup: temp SQLite DB + local uploads, configured before `app` is imported."""

import os
import shutil
import tempfile

_TMP = tempfile.mkdtemp(prefix="experientia-tests-")
os.environ.update(
    {
        "DATABASE_URL": f"sqlite:///{_TMP}/test.db",
        "UPLOAD_DIR": f"{_TMP}/uploads",
        "PUBLIC_BASE_URL": "http://testserver",
        "S3_BUCKET": "",
        "S3_BUCKET_NAME": "",
        "AWS_ACCESS_KEY_ID": "",
        "ACCESS_KEY_AWS_ID": "",
        "CREATIVE_VERIFY_CRON": "",
        "CREATIVE_VERIFY_ON_UPLOAD": "false",
    }
)

import pytest  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402


@compiles(JSONB, "sqlite")
def _jsonb_as_json(_type, _compiler, **_kw):
    # Models target Postgres; SQLite only needs a JSON column for tests.
    return "JSON"


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient

    from sqlalchemy import event

    from app.database import Base, engine
    import app.models  # noqa: F401

    # Postgres enforces ON DELETE CASCADE; SQLite only does with this pragma.
    event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
    engine.dispose()
    Base.metadata.create_all(engine)
    from app.main import app

    return TestClient(app)


@pytest.fixture(autouse=True)
def no_background_jobs(monkeypatch):
    """Tests run generation / verification explicitly instead of on the worker thread."""
    import app.routers.campaigns as campaigns
    import app.routers.creative as creative
    import app.routers.tasks as tasks

    monkeypatch.setattr(campaigns, "enqueue_generation", lambda *_: None)
    monkeypatch.setattr(creative, "enqueue_generation", lambda *_: None)
    monkeypatch.setattr(creative, "enqueue_campaign_verification", lambda *_: None)
    monkeypatch.setattr(tasks, "enqueue_task_verification", lambda *_: None)


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_TMP, ignore_errors=True)
