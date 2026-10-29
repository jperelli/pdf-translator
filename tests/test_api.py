import time

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def wait(client, job_id, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.2)
    raise AssertionError("job did not finish")


def test_config_and_samples(client):
    cfg = client.get("/api/config").json()
    assert cfg["backend"] == "pseudo" and any(lang["code"] == "ar" for lang in cfg["languages"])
    assert any(s["name"].startswith("09-stark") for s in client.get("/api/samples").json())


@pytest.mark.parametrize("name,msg", [("not-a-pdf.pdf", "Unsupported file type"), ("encrypted.pdf", "password")])
def test_invalid_uploads(client, samples, name, msg):
    r = client.post("/api/jobs", data={"target": "es"}, files={"file": (name, (samples / "invalid" / name).read_bytes())})
    assert r.status_code == 422 and msg in r.json()["error"]


def test_bad_requests(client):
    assert client.post("/api/jobs", data={"target": "xx", "sample": "01-simple-en.pdf"}).status_code == 422
    assert client.post("/api/jobs", data={"target": "es", "sample": "../../etc/passwd"}).status_code == 404
    assert client.post("/api/jobs", data={"target": "es"}).status_code == 422
    assert client.get("/api/jobs/not-a-job").status_code == 404


def test_full_job(client):
    r = client.post("/api/jobs", data={"target": "de", "sample": "01-simple-en.pdf"})
    assert r.status_code == 202
    job = wait(client, r.json()["id"])
    assert job["status"] == "done", job["error"]
    jid = job["id"]
    pdf = client.get(f"/api/jobs/{jid}/download")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    png = client.get(f"/api/jobs/{jid}/pages/1.png?which=translated")
    assert png.headers["content-type"] == "image/png"
    events = client.get(f"/api/jobs/{jid}/events").text
    for kind in ('"type": "analysis"', '"type": "page"', '"span": "agent"', '"type": "done"'):
        assert kind in events
    assert "event: end" in events


def test_history_versions_and_redo(client):
    r = client.post("/api/jobs", data={"target": "es", "sample": "08-jargon-en.pdf", "purpose": "fast"})
    job = wait(client, r.json()["id"])
    assert job["status"] == "done" and job["purpose"] == "fast"
    jid = job["id"]
    assert client.get("/api/jobs").status_code == 401  # the history is admin-only
    assert client.post("/api/admin/login", json={"token": "test-admin-token"}).status_code == 200
    listed = client.get("/api/jobs").json()
    assert any(j["id"] == jid and j["versions"] == 1 for j in listed["jobs"])
    est = client.post(f"/api/jobs/{jid}/estimate", json={"terms": {"keep_original": ["holeshot"]}}).json()
    assert est["tokens"] > 0
    assert client.post(f"/api/jobs/{jid}/redo", json={"terms": {"keep_original": ["holeshot"]}}).status_code == 202
    job2 = wait(client, jid)
    assert job2["status"] == "done" and job2["version"] == 2 and len(job2["versions"]) == 2
    v1 = client.get(f"/api/jobs/{jid}?version=1").json()
    assert v1["shown_version"] == 1 and v1["result"]["version"] == 1
    for v in (1, 2):
        pdf = client.get(f"/api/jobs/{jid}/download?version={v}")
        assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    assert client.post("/api/jobs", data={"target": "es", "sample": "01-simple-en.pdf", "purpose": "publish"}).status_code == 422
    assert client.delete(f"/api/jobs/{jid}").status_code == 200
    assert client.get(f"/api/jobs/{jid}").status_code == 404


def test_landing_page_and_app_routes(client):
    home = client.get("/")
    assert home.status_code == 200 and "Keep the design" in home.text and 'href="/app"' in home.text
    app_page = client.get("/app")
    assert app_page.status_code == 200 and 'id="drop"' in app_page.text
    assert 'id="architecture"' in home.text
    for asset in ("/landing.css", "/landing.js", "/landing/p1-de.webp", "/landing/architecture.svg", "/app.js"):
        assert client.get(asset).status_code == 200, asset
    assert client.get("/landing/p1-de.webp").headers["content-type"] == "image/webp"
