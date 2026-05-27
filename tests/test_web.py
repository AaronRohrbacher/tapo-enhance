"""HTTP-level integration tests via fastapi.testclient. Boots the real
app with a fake Tapo factory, hits the routes, asserts the structured
responses match what the frontend expects."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from srv.config import Settings
from srv.errors import Code
from srv.web import build_app


@pytest.fixture
def client(fake_tapo, cache_dir, monkeypatch):
    settings = Settings(
        host="127.0.0.1",
        user="admin",
        password="dummy",
        cache_root=cache_dir,
    )
    app = build_app(settings=settings, tapo_factory=lambda _s: fake_tapo)
    with TestClient(app) as c:
        yield c, fake_tapo


# ── camera info ────────────────────────────────────────────────────────────


def test_camera_status_returns_structured_info(client):
    c, _ = client
    r = c.get("/api/camera/status")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert j["alias"] == "Front Door"
    assert j["model"] == "D225"
    assert j["mac"] == "AA:BB:CC:DD:EE:FF"


def test_info_includes_battery_when_available(client):
    c, _ = client
    j = c.get("/api/info").json()
    assert j["ok"] is True
    assert j["battery"]["battery_percent"] == 73


# ── recordings listing ────────────────────────────────────────────────────


def test_recordings_dates_returns_sorted_strings(client):
    c, _ = client
    j = c.get("/api/recordings/dates").json()
    assert j["ok"] is True
    assert j["dates"] == ["20260426", "20260425", "20260424"]


def test_recordings_for_date_parses_clips(client):
    c, _ = client
    j = c.get("/api/recordings/20260426").json()
    assert j["ok"] is True
    assert len(j["clips"]) == 2
    first = j["clips"][0]
    assert set(first.keys()) == {"date", "startTime", "endTime"}


def test_recordings_local_starts_empty(client):
    c, _ = client
    j = c.get("/api/recordings/local").json()
    assert j["ok"] is True
    assert j["files"] == []


# ── thumb endpoint shape ───────────────────────────────────────────────────


def test_thumb_endpoint_serves_placeholder_with_status_header(client):
    c, _ = client
    r = c.get("/api/thumb/20260426/1777187350/1777187366")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.headers["x-thumb-status"] in ("queued", "running", "ready")


def test_thumb_head_returns_status_without_body(client):
    """The frontend polls thumbs via HEAD. Must work or polling stalls."""
    c, _ = client
    r = c.head("/api/thumb/20260426/1777187350/1777187366")
    assert r.status_code == 200
    assert r.headers.get("x-thumb-status")


def test_thumb_status_aggregates_correctly(client):
    c, _ = client
    # Trigger some enqueues.
    for path in [
        "/api/thumb/20260426/1777187350/1777187366",
        "/api/thumb/20260426/1777192872/1777192887",
    ]:
        c.head(path)
    j = c.get("/api/thumb-status/20260426").json()
    assert j["ok"] is True
    assert j["counts"]["total"] >= 2


# ── cache management ──────────────────────────────────────────────────────


def test_cache_usage_starts_empty(client):
    c, _ = client
    j = c.get("/api/cache/usage").json()
    assert j["ok"] is True
    assert j["usage"]["total_bytes"] == 0


def test_cache_clear_rejects_unknown_target(client):
    c, _ = client
    r = c.post("/api/cache/clear", json={"target": "garbage"})
    assert r.status_code == 400
    j = r.json()
    assert j["code"] == Code.BAD_REQUEST.value
    assert "garbage" in j["error"]


def test_cache_clear_succeeds_for_each_bucket(client, paths, make_av_mp4):
    c, _ = client
    # Drop a fake recording, then wipe.
    rec_dir = paths.recordings / "20260101"
    rec_dir.mkdir(parents=True)
    (rec_dir / "x.mp4").write_bytes(b"x" * 1024)
    j = c.post("/api/cache/clear", json={"target": "recordings"}).json()
    assert j["ok"] is True
    assert j["target"] == "recordings"
    assert j["removed_files"] >= 1


# ── error model ───────────────────────────────────────────────────────────


def test_unknown_clip_in_download_returns_structured_error(client):
    c, _ = client
    r = c.post("/api/recordings/download", json={"date": "20260101", "startTime": "abc"})
    assert r.status_code == 400
    j = r.json()
    assert j["ok"] is False
    assert j["code"] == Code.BAD_REQUEST.value
    assert "retryable" in j


def test_unknown_job_id_returns_404(client):
    c, _ = client
    r = c.get("/api/jobs/deadbeef")
    assert r.status_code == 404
    assert r.json()["code"] == Code.NOT_FOUND.value


# ── stream lifecycle ──────────────────────────────────────────────────────


def test_stream_status_reports_idle_initially(client):
    c, _ = client
    assert c.get("/api/stream/status").json()["running"] is False


def test_gateway_status_endpoint_reports_state(client):
    c, _ = client
    j = c.get("/api/gateway/status").json()
    assert j["ok"] is True
    assert j["queued"] == 0
    assert j["live_attached"] is False
    assert j["busy"] is None


# ── snapshot ──────────────────────────────────────────────────────────────


def test_snapshot_503_when_live_not_running(client):
    c, _ = client
    r = c.get("/api/snapshot")
    assert r.status_code == 503
    assert r.json()["code"] == Code.CAMERA_OFFLINE.value
