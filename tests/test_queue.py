import shutil
import time
import uuid

import os
import subprocess

from app.worker import job_dir, recover, run_job, store, worker_alive, worker_token


def make_job(samples, name="01-simple-en.pdf", target="de"):
    job_id = uuid.uuid4().hex
    job_dir(job_id).mkdir(parents=True)
    shutil.copy(samples / name, job_dir(job_id) / "source.pdf")
    store().create_job(job_id, name, target, "quality")
    return job_id


def test_worker_writes_events_and_result(samples):
    job_id = make_job(samples)
    run_job(job_id, 1, 1)
    db = store()
    row = db.job(job_id)
    assert row["status"] == "done" and row["score"] is not None
    events = [e for _, e in db.events(job_id, 1, 1)]
    assert events[-1]["type"] == "done" and any(e["type"] == "page" for e in events)
    assert db.version(job_id, 1)["result"]["report"]["score"] == row["score"]


def test_lost_worker_is_retried_then_failed(samples):
    db = store()
    job_id = make_job(samples)
    dead = subprocess.Popen(["true"])
    dead.wait()
    assert db.start(job_id, 1, 1, worker_token(dead.pid))  # a worker process that no longer exists
    db.add_event(job_id, 1, 1, {"type": "stage", "stage": "translate", "percent": 30})
    # nobody heartbeats: the worker died. Recovery requeues it as attempt 2 (immediate mode runs it right away)
    assert recover(time.time() + 3600) == [job_id]
    row = db.job(job_id)
    assert row["status"] == "done" and row["attempt"] == 2
    events = [e for _, e in db.events(job_id, 1, 2)]
    assert events[0]["type"] == "warning" and events[-1]["type"] == "done"
    assert db.version(job_id, 1)["events"] == events  # the log shows the attempt that finished

    job2 = make_job(samples)
    db.start(job2, 1, 1, worker_token(dead.pid))
    db.requeue(job2, 1)
    db.start(job2, 1, 2, worker_token(dead.pid))  # second attempt dies too: no third try
    assert recover(time.time() + 3600) == []
    assert db.job(job2)["status"] == "error" and "stopped twice" in db.job(job2)["error"]


def test_superseded_attempt_cannot_overwrite(samples):
    db = store()
    job_id = make_job(samples)
    assert db.start(job_id, 1, 1)
    assert db.requeue(job_id, 1) == 2
    assert not db.start(job_id, 1, 1)  # the old task cannot claim it again
    assert not db.finish(job_id, 1, 1, None, "late", {"type": "error", "message": "late"})
    assert db.job(job_id)["status"] == "queued"


def test_sse_streams_events_from_the_worker(samples):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        jid = client.post("/api/jobs", data={"target": "es", "sample": "01-simple-en.pdf"}).json()["id"]
        with client.stream("GET", f"/api/jobs/{jid}/events") as r:
            body = "".join(r.iter_text())
    assert '"stage": "queued"' in body and '"type": "done"' in body and body.rstrip().endswith("data: {}")


def test_busy_worker_is_not_mistaken_for_dead(samples):
    db = store()
    job_id = make_job(samples)
    assert db.start(job_id, 1, 1, worker_token())  # this test process: alive, even with an ancient heartbeat
    assert worker_alive(worker_token()) and worker_alive(f"other-host:{os.getpid()}:1") is None
    assert recover(time.time() + 3600) == []
    assert db.job(job_id)["status"] == "running"
    db.fail(job_id, 1, "cleanup")
