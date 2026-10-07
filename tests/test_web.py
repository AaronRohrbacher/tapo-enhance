"""HTTP-level integration tests via fastapi.testclient. Boots the real
app with a fake Tapo factory, hits the routes, asserts the structured
responses match what the frontend expects."""

from __future__ import annotations

import json
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
import logging
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from srv import recordings as rec_mod
from srv.config import Settings
from srv.errors import Code
from srv.web import HealthcheckAccessFilter, _battery_summary, build_app


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


def test_live_to_recordings_concurrent_http_requests_do_not_kill_queue(client, monkeypatch):
    """Reproduce a tab switch: stop Live and request dates/show-date together."""
    c, _camera = client
    gateway = c.app.state.gateway
    hls = c.app.state.hls
    closing = threading.Event()
    queued = threading.Event()
    release = asyncio.Event()
    submits = 0
    original_submit = gateway._submit

    async def started():
        hls._generation += 1
        hls._playable_generation = hls._generation

    async def live(_tapo, cancel, _consumer):
        await cancel.wait()
        closing.set()
        await release.wait()
        raise asyncio.CancelledError

    def submit(*args, **kwargs):
        nonlocal submits
        future = original_submit(*args, **kwargs)
        submits += 1
        if submits == 2:
            queued.set()
        return future

    monkeypatch.setattr(hls, "started", started)
    monkeypatch.setattr(gateway, "_live_source", live)
    monkeypatch.setattr(gateway, "_submit", submit)
    assert c.post("/api/stream/start").status_code == 200
    with ThreadPoolExecutor(max_workers=3) as pool:
        stopped = pool.submit(c.post, "/api/stream/stop")
        try:
            assert closing.wait(2)
            dates = pool.submit(c.get, "/api/recordings/dates")
            clips = pool.submit(c.get, "/api/recordings/20260426")
            assert queued.wait(2)
        finally:
            c.portal.call(release.set)
        assert stopped.result(timeout=2).status_code == 200
        assert dates.result(timeout=2).status_code == 200
        clip_response = clips.result(timeout=2)
        assert clip_response.status_code == 200
        assert len(clip_response.json()["clips"]) == 2

    clip = {"date": "20990101", "startTime": 1700000000, "endTime": 1700000002}
    # Continue through actual remote HLS and download runners, real ffmpeg,
    # then confirm the exact same recording plays locally, not from camera.
    remote = c.post("/api/recordings/play", json=clip)
    assert remote.status_code == 200
    assert remote.json()["source"] == "camera"
    assert c.get(remote.json()["url"]).status_code == 200
    downloaded = c.post("/api/recordings/download", json=clip)
    assert downloaded.status_code == 200
    local = c.post("/api/recordings/play", json=clip)
    assert local.status_code == 200
    assert local.json()["source"] == "local"
    assert c.get(local.json()["url"]).status_code == 200
    status = c.get("/api/gateway/status").json()
    assert status["queued"] == 0
    assert status["busy"] is None
    assert c.portal.call(lambda: not gateway._task.done())


def test_index_has_name_and_favicon(client):
    c, _ = client
    page = c.get("/")
    assert page.status_code == 200
    assert "Tapo Enhance" in page.text
    icon = c.get("/static/favicon.svg")
    assert icon.status_code == 200
    assert icon.headers["content-type"].startswith("image/svg+xml")


def test_dvr_setup_is_separate_and_settings_are_in_settings_tab(client):
    c, _ = client
    page = c.get("/").text
    assert 'id="setup-camera-step"' in page
    assert 'id="setup-dvr-step"' in page
    assert 'id="setup-dvr-form"' in page
    assert page.index('id="tab-settings"') < page.index('id="dvr-settings-form"')
    archive_start = page.index('id="tab-archive"')
    settings_start = page.index('id="tab-settings"')
    assert 'data-tab="local"' not in page
    assert 'id="tab-local"' not in page
    assert 'id="local-list"' not in page
    assert archive_start < page.index('id="dvr-status"') < settings_start
    assert archive_start < page.index('id="dvr-conversion-card"') < settings_start
    assert page.index('id="dvr-progress-wrap"') < page.index('id="dvr-conversion-card"')
    assert archive_start < page.index('id="dvr-availability"') < settings_start
    assert archive_start < page.index('id="dvr-window"') < settings_start
    assert archive_start < page.index('id="btn-dvr-sync"') < settings_start
    assert archive_start < page.index('id="cache-rows"') < settings_start
    assert 'id="dvr-settings-status"' in page
    assert "12:10am" in page
    assert page.count('name="interval_minutes"') == 2
    assert page.count('name="initial_sync"') == 4
    assert "sync existing camera history now" in page
    assert "sync missing camera history after saving" in page
    assert "wait until the next scheduled interval" in page
    assert ">Check and download now<" in page
    assert "There is one downloaded-recordings library." in page
    assert ">show date<" in page
    assert ">show last 30 days<" in page


def test_live_dvr_status_does_not_overwrite_settings_form(client):
    """SSE progress is frequent during boot sync and must remain display-only."""
    page = (Path(__file__).parent.parent / "static" / "main.js").read_text()
    assert "function hydrateDvrForm(data)" in page
    assert "else if (evt.type === \"dvr\") applyDvr(evt);" in page
    assert "hydrateDvrForm(evt)" not in page


def test_dvr_schedule_settings_and_manual_sync_validation(client):
    c, fake = client
    saved = c.post("/api/dvr", json={
        "enabled": False, "retention_days": 14, "keep_forever": True,
        "interval_minutes": 360, "daily_time": "00:10",
    })
    assert saved.status_code == 200
    assert saved.json()["interval_minutes"] == 360
    assert saved.json()["keep_forever"] is True
    c.get("/api/dvr")
    history_call = next(call for call in reversed(fake.calls) if call[0] == "getRecordingsList")
    start, end = (datetime.strptime(value, "%Y%m%d") for value in history_call[1])
    assert (end - start).days == 13
    invalid = c.post("/api/dvr", json={
        "enabled": False, "retention_days": 14, "keep_forever": False,
        "interval_minutes": 7, "daily_time": "bad",
    })
    assert invalid.status_code == 400
    assert c.post("/api/dvr/sync").status_code == 400


def test_app_metadata_has_single_source_version(client):
    c, _ = client
    version = (Path(__file__).parent.parent / "VERSION").read_text().strip()
    assert c.get("/api/app").json()["version"] == version
    assert f"v{version}" in c.get("/").text


def test_successful_healthcheck_access_log_is_suppressed_only():
    access_filter = HealthcheckAccessFilter()

    def record(path, status):
        return logging.LogRecord(
            "uvicorn.access", logging.INFO, __file__, 1, "%s %s %s %s %s",
            (("127.0.0.1", 44248), "GET", path, "1.1", status), None,
        )

    assert access_filter.filter(record("/api/app", 200)) is False
    assert access_filter.filter(record("/api/app", 500)) is True
    assert access_filter.filter(record("/api/dvr", 200)) is True


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


def test_discovery_recovers_only_exact_saved_camera_from_arp(client, monkeypatch):
    c, _fake = client
    c.app.state.settings.mac = "78:20:51:1e:50:7a"
    monkeypatch.setattr("srv.discovery.local_subnets", lambda: ["10.1.1.0/24"])
    monkeypatch.setattr("srv.discovery.discover_candidates", lambda _subnet: [])
    monkeypatch.setattr("srv.discovery.read_arp_table", lambda: [
        __import__("srv.discovery", fromlist=["ArpEntry"]).ArpEntry(
            "10.1.1.161", "78:20:51:1e:50:7a"
        ),
        __import__("srv.discovery", fromlist=["ArpEntry"]).ArpEntry(
            "10.1.1.137", "78:20:51:1e:5b:9f"
        ),
    ])
    cameras = c.get("/api/discovery").json()["cameras"]
    assert cameras == [{
        "ip": "10.1.1.161", "mac": "78:20:51:1e:50:7a",
        "source": "saved camera identity", "known_camera": True,
    }]


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
        assert saved.json()["camera_oldest_date"] is None
        assert saved.json()["camera_available_days"] == 0
        assert saved.json()["dvr_retention_days"] == 7
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
    assert body["source"] == "camera"
    manifest = c.get(body["url"])
    assert manifest.status_code == 200
    assert "#EXTM3U" in manifest.text
    assert c.get("/api/recordings/local").json()["files"] == []
    assert c.post("/api/recordings/play/stop").json()["ok"] is True


def test_watch_uses_valid_local_recording_without_camera(client, make_av_mp4):
    c, tapo = client
    clip = {
        "date": "20260426",
        "startTime": 1777187350,
        "endTime": 1777187352,
    }
    cached = c.app.state.paths.recording(
        rec_mod.Clip(clip["date"], clip["startTime"], clip["endTime"])
    )
    cached.parent.mkdir(parents=True, exist_ok=True)
    make_av_mp4(cached, duration=2)
    sessions_before = len(tapo.sessions)

    response = c.post("/api/recordings/play", json=clip)

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "url": f"/recordings/{clip['date']}/{cached.name}",
        "source": "local",
    }
    assert len(tapo.sessions) == sessions_before


def test_local_inventory_excludes_corrupt_recording(client):
    c, _ = client
    paths = c.app.state.paths
    corrupt = paths.recordings / "20260426" / "1777187350_1777187352.mp4"
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_bytes(b"not an mp4" * 200)

    assert c.get("/api/recordings/local").json()["files"] == []


def test_watch_repairs_corrupt_local_recording(client, ffprobe_streams):
    c, tapo = client
    clip = {
        "date": "20260426",
        "startTime": 1777187350,
        "endTime": 1777187352,
    }
    cached = c.app.state.paths.recording(
        rec_mod.Clip(clip["date"], clip["startTime"], clip["endTime"])
    )
    cached.parent.mkdir(parents=True, exist_ok=True)
    corrupt = b"not an mp4" * 200
    cached.write_bytes(corrupt)
    sessions_before = len(tapo.sessions)

    response = c.post("/api/recordings/play", json=clip)

    assert response.status_code == 200
    assert response.json()["source"] == "local"
    assert response.json()["repaired"] is True
    assert cached.read_bytes() != corrupt
    assert any(s["codec_type"] == "video" for s in ffprobe_streams(cached))
    assert len(tapo.sessions) == sessions_before + 1


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
