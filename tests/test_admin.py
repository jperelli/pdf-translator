import re
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.worker import job_dir, store

TOKEN = "test-admin-token"


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def admin(client):
    assert client.post("/api/admin/login", json={"token": TOKEN}).status_code == 200
    return client


def queued_job(samples):
    """A job stuck in the queue (immediate mode would run it right away, so create it by hand)."""
    import shutil
    import uuid

    jid = uuid.uuid4().hex
    job_dir(jid).mkdir(parents=True)
    shutil.copy(samples / "01-simple-en.pdf", job_dir(jid) / "source.pdf")
    store().create_job(jid, "01-simple-en.pdf", "de", "fast")
    return jid


def test_admin_area_requires_login(client):
    assert client.get("/api/admin/session").json() == {"enabled": True, "admin": False}
    for method, path in [("get", "/api/admin/overview"), ("get", "/api/jobs"),
                         ("post", "/api/admin/jobs/" + "0" * 32 + "/retry")]:
        assert getattr(client, method)(path).status_code == 401, path
    assert client.post("/api/admin/login", json={"token": "wrong"}).status_code == 401
    admin(client)
    assert client.get("/api/admin/session").json()["admin"] is True
    client.post("/api/admin/logout")
    assert client.get("/api/admin/overview").status_code == 401


def test_overview_cancel_and_retry(client, samples):
    admin(client)
    jid = queued_job(samples)
    ov = client.get("/api/admin/overview").json()
    assert any(j["id"] == jid for j in ov["queued"]) and ov["queue"]["pending"] is not None
    assert client.post(f"/api/admin/jobs/{jid}/cancel").status_code == 200
    row = store().job(jid)
    assert row["status"] == "error" and "Canceled" in row["error"]
    assert client.post(f"/api/admin/jobs/{jid}/cancel").status_code == 409  # already finished
    failed = client.get("/api/admin/overview").json()["failed"]
    assert any(j["id"] == jid for j in failed)
    # retry runs it (immediate mode) as a new attempt, and the log shows that attempt
    assert client.post(f"/api/admin/jobs/{jid}/retry").status_code == 200
    row = store().job(jid)
    assert row["status"] == "done" and row["attempt"] == 3
    events = client.get(f"/api/jobs/{jid}").json()["events"]
    assert events[0]["message"] == "Retried by an administrator" and events[-1]["type"] == "done"


def test_cancel_stops_a_running_worker(samples):
    """The worker checks ownership between events and stops without writing a result."""
    from app.worker import run_job

    db = store()
    jid = queued_job(samples)
    calls = []
    orig = db.add_event

    def add_event(job_id, version, attempt, event):  # cancel as soon as the pipeline reports progress
        calls.append(event.get("type"))
        if len(calls) == 3 and db.job(job_id)["status"] == "running":
            db.cancel(job_id)
            time.sleep(2.1)  # past the worker's ownership check interval
        return orig(job_id, version, attempt, event)

    db.add_event = add_event
    try:
        run_job(jid, 1, 1)
    finally:
        db.add_event = orig
    row = db.job(jid)
    assert row["status"] == "error" and "Canceled" in row["error"]
    assert db.version(jid, 1)["result"] is None
    assert len(calls) < 15  # stopped early, did not run the whole pipeline


def test_metrics(client, samples):
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer nope"}).status_code == 401
    jid = client.post("/api/jobs", data={"target": "es", "sample": "01-simple-en.pdf"}).json()["id"]
    body = client.get("/metrics", headers={"Authorization": f"Bearer {TOKEN}"}).text
    for metric in ("translator_up 1", "translator_queue_pending", 'translator_jobs{status="done"}',
                   "translator_worker_last_seen_seconds", 'translator_jobs_finished_total{purpose="fast",status="done"}',
                   'translator_job_duration_seconds_bucket{status="done",le="+Inf"}',
                   "translator_llm_output_tokens_total", "# TYPE translator_job_duration_seconds histogram",
                   "# TYPE translator_llm_cost_usd_total counter"):
        assert metric in body, metric
    line = re.compile(r'^translator_\w+(\{[^}]*\})? (-?[\d.e+-]+|NaN|\+Inf)$')
    for ln in body.splitlines():
        assert ln.startswith("#") or line.match(ln), ln  # valid exposition format
    done = float(re.search(r'translator_jobs_finished_total\{purpose="fast",status="done"\} (\S+)', body).group(1))
    admin(client).delete(f"/api/jobs/{jid}")  # counters survive deleting the job (TTL cleanup)
    body = client.get("/metrics", headers={"Authorization": f"Bearer {TOKEN}"}).text
    assert float(re.search(r'translator_jobs_finished_total\{purpose="fast",status="done"\} (\S+)', body).group(1)) == done
