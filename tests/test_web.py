"""HTTP-level integration tests via fastapi.testclient. Boots the real
app with a fake Tapo factory, hits the routes, asserts the structured
responses match what the frontend expects."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from srv.config import Settings
from srv.errors import Code
from srv.web import _battery_summary, build_app


@pytest.fixture
def client(fake_media_tapo, cache_dir, monkeypatch):
    settings = Settings(
        host="127.0.0.1",
        user="admin",
        password="dummy",
        cache_root=cache_dir,
    )
    app = build_app(settings=settings, tapo_factory=lambda _s: fake_media_tapo)
    with TestClient(app) as c:
        yield c, fake_media_tapo


# ── camera info ────────────────────────────────────────────────────────────


def test_index_has_name_and_favicon(client):
    c, _ = client
    page = c.get("/")
    assert page.status_code == 200
    assert "Tapo Enhance" in page.text
    icon = c.get("/static/favicon.svg")
    assert icon.status_code == 200
    assert icon.headers["content-type"].startswith("image/svg+xml")


def test_app_metadata_has_single_source_version(client):
    c, _ = client
    assert c.get("/api/app").json()["version"] == "1.0b"
    assert "v1.0b" in c.get("/").text


def test_themes_endpoint_returns_yaml_themes(client):
    c, _ = client
    themes = c.get("/api/themes").json()["themes"]
    assert {theme["id"] for theme in themes} >= {"phosphor", "amber", "blue", "light"}


def test_camera_status_returns_structured_info(client):
    c, _ = client
    r = c.get("/api/camera/status")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert j["alias"] == "Front Door"
    assert j["model"] == "D225"
    assert j["mac"] == "AA:BB:CC:DD:EE:FF"
    assert j["battery_percent"] == 73
    assert j["is_charging"] is False


def test_info_includes_battery_when_available(client):
    c, _ = client
    j = c.get("/api/info").json()
    assert j["ok"] is True
    assert j["battery"]["battery"]["status"]["battery_percent"] == 73


def test_camera_features_are_curated_and_setters_are_allowlisted(client):
    c, fake = client
    data = c.get("/api/camera/features").json()
    assert data["ok"] is True
    assert set(data) >= {"storage", "video", "power", "firmware", "controls", "errors"}
    changed = c.post("/api/camera/feature/motion", json={"value": False})
    assert changed.status_code == 200
    assert any(call[1][0] == "setMotionDetection" for call in fake.calls if call[0] == "__getattr__")
    rejected = c.post("/api/camera/feature/format_sd", json={"value": True})
    assert rejected.status_code == 400


def test_first_run_discovery_uses_detected_or_user_subnet(client, monkeypatch):
    c, _fake = client
    scanned = []
    monkeypatch.setattr("srv.discovery.local_subnets", lambda: ["192.168.7.0/24"])
    monkeypatch.setattr("srv.discovery.discover_candidates", lambda subnet: scanned.append(subnet) or [
        {"ip": "192.168.7.9", "mac": "78:20:51:00:00:01", "source": "camera port 8800"}
    ])
    automatic = c.get("/api/discovery").json()
    assert automatic["subnets"] == ["192.168.7.0/24"]
    assert automatic["cameras"][0]["ip"] == "192.168.7.9"
    assert automatic["cameras"][0]["known_camera"] is False
    manual = c.get("/api/discovery?subnet=10.44.3.9/24").json()
    assert manual["subnets"] == ["10.44.3.0/24"]
    assert scanned == ["192.168.7.0/24", "10.44.3.0/24"]


def test_web_configuration_encrypts_and_never_returns_password(tmp_path, fake_media_tapo):
    settings = Settings(cache_root=tmp_path / "private", key_root=tmp_path / "keys")
    app = build_app(settings=settings, tapo_factory=lambda _s: fake_media_tapo)
    with TestClient(app) as c:
        before = c.get("/api/config").json()
        assert before["configured"] is False
        saved = c.post("/api/config", json={
            "host": "10.0.0.8", "user": "admin", "password": "very-secret",
            "subnet": "10.0.0.0/24", "mac": "78:20:51:aa:bb:cc",
        })
        assert saved.status_code == 200
        after = c.get("/api/config").json()
        assert "password" not in after
        assert "password_set" not in after
    assert settings.vault_file().stat().st_mode & 0o777 == 0o600
    assert settings.key_file().stat().st_mode & 0o777 == 0o600
    assert "very-secret" not in settings.vault_file().read_text()
    fresh = Settings(cache_root=settings.cache_root, key_root=settings.key_root)
    fresh.load_persisted_config()
    assert fresh.password == "very-secret"
    assert fresh.cloud_password == "very-secret"
    assert settings.mac == "78:20:51:aa:bb:cc"


def test_battery_summary_handles_real_d225_shape_and_missing_values():
    assert _battery_summary({
        "battery": {"status": {"battery_percent": 78, "battery_charging": "YES"}}
    }) == (78, True)
    assert _battery_summary({}) == (None, False)


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


def test_watch_returns_progressive_hls_without_archiving(client):
    c, _ = client
    clip = {
        "date": "20260426",
        "startTime": 1777187350,
        "endTime": 1777187352,
    }
    response = c.post("/api/recordings/play", json=clip)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    manifest = c.get(body["url"])
    assert manifest.status_code == 200
    assert "#EXTM3U" in manifest.text
    assert c.get("/api/recordings/local").json()["files"] == []
    assert c.post("/api/recordings/play/stop").json()["ok"] is True


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
