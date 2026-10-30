"""Playwright integration test. Spawns uvicorn against FakeTapo, then a
real browser drives the app: load page, browse archive, click a clip,
confirm download succeeds and remains marked in Archive. Catches the
class of bug where unit tests pass but the JS doesn't actually wire to
the route.

Uses pytest-playwright's `page` fixture so the event loop hand-off back
to pytest-asyncio is clean (we previously hit "Cannot run the event loop
while another loop is running" using sync_playwright directly)."""

from __future__ import annotations

import socket
import threading
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent

# pytest-playwright provides a top-level `page` fixture; importing the
# package gives us a hard skip if it's missing and the marker hooks.
playwright = pytest.importorskip("playwright")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def fake_server():
    """Boot uvicorn against FakeTapo on a free port. Daemonised; the
    server dies when the test process does."""
    port = _free_port()

    import uvicorn
    from tests._fakeserver import _build
    app = _build()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", loop="asyncio"))
    t = threading.Thread(target=server.run, daemon=True, name="fakeserver")
    t.start()
    deadline = time.time() + 10
    base = f"http://127.0.0.1:{port}"
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"{base}/api/recordings/local", timeout=1).read()
            break
        except Exception:
            time.sleep(0.1)
    else:
        pytest.fail("fake server didn't come up in 10s")
    try:
        yield base
    finally:
        server.should_exit = True
        t.join(timeout=5)
        assert not t.is_alive(), "test server did not shut down"


@pytest.mark.browser
def test_index_renders_and_status_pill_connects(page, fake_server):
    page.goto(fake_server, timeout=10_000)
    page.wait_for_selector("#status-text", timeout=5_000)
    page.wait_for_function(
        "() => document.querySelector('#status-dot').classList.contains('connected')",
        timeout=5_000,
    )
    text = page.text_content("#status-text")
    assert "Front Door" in text
    assert page.text_content("#battery-status") == "73%"


@pytest.mark.browser
def test_yaml_themes_load_and_persist(page, fake_server):
    page.goto(fake_server, timeout=10_000)
    page.wait_for_function("() => document.querySelectorAll('#theme-select option').length >= 4")
    assert {o.get_attribute("value") for o in page.query_selector_all("#theme-select option")} >= {
        "phosphor", "amber", "blue", "light"
    }
    page.select_option("#theme-select", "amber")
    assert page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--primary').trim()") == "#ffad22"
    page.reload()
    page.wait_for_function("() => document.querySelector('#theme-select').value === 'amber'")


@pytest.mark.browser
def test_settings_loads_feature_dashboard_and_reconfiguration_form(page, fake_server):
    page.goto(fake_server, timeout=10_000)
    page.wait_for_selector(".dot.connected", timeout=5_000)
    page.click("button[data-tab='settings']")
    page.wait_for_selector(".feature-row", timeout=5_000)
    assert "video" in page.text_content("#feature-status")
    page.click("#btn-configure")
    page.wait_for_selector("#setup-panel", state="visible", timeout=5_000)
    assert page.input_value("#camera-config-form input[name='password']") == ""


@pytest.mark.browser
def test_dvr_window_displays_server_setting_and_updates_after_save(page, fake_server):
    page.goto(fake_server, timeout=10_000)
    page.wait_for_function("() => document.querySelector('#dvr-window').textContent.includes('7 days')")
    page.click("button[data-tab='settings']")
    page.check("#dvr-settings-form input[name='enabled']")
    page.fill("#dvr-settings-form input[name='retention_days']", "12")
    page.click("#dvr-settings-form button[type='submit']")
    page.wait_for_function("() => document.querySelector('#dvr-window').textContent.includes('12 days')")
    assert page.input_value("#dvr-settings-form input[name='retention_days']") == "12"


@pytest.mark.browser
def test_cache_labels_explain_temporary_playback_storage(page, fake_server):
    page.goto(fake_server, timeout=10_000)
    page.click("button[data-tab='settings']")
    assert page.locator("#tab-settings #cache-rows").is_visible()
    assert page.locator("#tab-archive #cache-rows").count() == 0
    page.wait_for_function("() => document.querySelector('#cache-rows').textContent.includes('undownloaded-recording HLS files')")
    text = page.text_content("#cache-rows")
    assert "Local DVR MP4s; playback uses these files directly." in text
    assert "Browser-playable MP4 copies fetched from the camera without adding them to the downloaded DVR archive." in text
    assert "HLS playlists and two-second video segments generated while watching a camera recording that has not been downloaded." in text
    assert "Temporary HLS segments used by the Live tab." in text


@pytest.mark.browser
def test_first_run_automatically_discovers_and_selects_camera(page, fake_server):
    page.route("**/api/config", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body='{"ok":true,"configured":false,"host":"","user":"admin","subnet":""}',
    ))
    page.route("**/api/discovery", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body='{"ok":true,"subnets":["192.168.42.0/24"],"cameras":['
             '{"ip":"192.168.42.18","mac":"78:20:51:aa:bb:cc","source":"camera port 8800"}]}',
    ))
    page.goto(fake_server, timeout=10_000)
    page.wait_for_function("() => document.querySelector('input[name=host]').value === '192.168.42.18'")
    assert page.input_value("input[name='subnet']") == "192.168.42.0/24"
    assert page.input_value("input[name='host']") == "192.168.42.18"


@pytest.mark.browser
def test_leaving_live_stops_the_server_stream(page, fake_server):
    stops = []

    def stop_route(route):
        stops.append(route.request.url)
        route.fulfill(status=200, content_type="application/json", body='{"ok":true}')

    page.route("**/api/stream/stop", stop_route)
    page.goto(fake_server, timeout=10_000)
    page.wait_for_selector(".dot.connected", timeout=5_000)
    page.evaluate("S.set(s => { s.live.status = 'running'; s.gateway.liveAttached = true; })")
    page.click("button[data-tab='archive']")
    page.wait_for_function("() => S.state.live.status === 'idle'", timeout=5_000)
    assert len(stops) == 1


@pytest.mark.browser
def test_archive_loads_clips_for_a_date(page, fake_server):
    page.goto(fake_server, timeout=10_000)
    page.wait_for_selector(".dot.connected", timeout=5_000)
    page.click("button[data-tab='archive']")
    page.fill("#arch-date", "2026-04-26")
    page.click("#btn-arch-load")
    page.wait_for_selector(".clip-card", timeout=5_000)
    cards = page.query_selector_all(".clip-card")
    assert len(cards) == 2


@pytest.mark.browser
def test_events_stream_pushes_gateway_and_cache_snapshots(fake_server):
    """The SSE push channel must open and immediately deliver a gateway
    snapshot. Read against the real uvicorn server (ASGI test transports
    can't stream an infinite body)."""
    import json
    import socket

    resp = urllib.request.urlopen(f"{fake_server}/api/events", timeout=5)
    try:
        assert resp.headers.get("content-type", "").startswith("text/event-stream")
        resp.fp.raw._sock.settimeout(5)  # bound the readline
        line = resp.readline()
        resp.readline()  # blank SSE separator
        cache_line = resp.readline()
    finally:
        resp.close()
    assert line.startswith(b"data:"), line
    evt = json.loads(line[len(b"data:"):])
    assert evt["type"] == "gateway"
    assert set(evt) >= {"busy", "queued", "liveAttached", "liveRunning"}
    cache_evt = json.loads(cache_line[len(b"data:"):])
    assert cache_evt["type"] == "cache"
    assert set(cache_evt["usage"]) >= {"recordings", "thumbs", "total_bytes"}


@pytest.mark.browser
def test_no_polling_and_thumb_status_pushed_over_sse(page, fake_server):
    """The core of the polling rewrite: loading the archive must NOT spin up
    a gateway-status timer or a per-thumbnail HEAD loop, and thumbnail status
    must arrive over the single SSE stream instead."""
    reqs: list[tuple[str, str]] = []
    page.on("request", lambda r: reqs.append((r.method, r.url)))

    page.goto(fake_server, timeout=10_000)
    page.wait_for_selector(".dot.connected", timeout=5_000)
    page.click("button[data-tab='archive']")
    # 2026-04-25 is untouched by the other tests in this module, so its thumbs
    # transition live (queued→running→failed) rather than replaying a cached
    # terminal state — exercising the push path end to end.
    page.fill("#arch-date", "2026-04-25")
    page.click("#btn-arch-load")
    page.wait_for_selector(".clip-card", timeout=5_000)

    # Proof the push works: a thumbnail badge must move off "queued" on its
    # own (no HEAD poll drives it — only the SSE thumb events do).
    page.wait_for_function(
        "() => Array.from(document.querySelectorAll('.clip-card .badge'))"
        ".some(b => b.textContent && b.textContent !== 'queued')",
        timeout=20_000,
    )
    # Give any stray polling loop several cycles to betray itself.
    page.wait_for_timeout(5000)

    gw = [u for (m, u) in reqs if "/api/gateway/status" in u]
    thumb_heads = [u for (m, u) in reqs if m == "HEAD" and "/api/thumb/" in u]
    events = [u for (m, u) in reqs if "/api/events" in u]
    thumb_all = [u for (m, u) in reqs if "/api/thumb/" in u]
    cache_usage = [u for (m, u) in reqs if "/api/cache/usage" in u]

    assert gw == [], f"gateway/status must never be polled; saw {len(gw)}"
    assert thumb_heads == [], f"thumbnails must not be HEAD-polled; saw {len(thumb_heads)}"
    assert cache_usage == [], f"cache usage must be pushed, not polled; saw {len(cache_usage)}"
    assert len(events) >= 1, "the SSE event stream was never opened"
    # Bounded by status transitions (≈3 per clip × 2 clips), NOT by a timer ×
    # elapsed seconds — the old loop would have fired ~15+ HEADs in this window.
    assert len(thumb_all) < 20, f"thumb requests look like a storm: {len(thumb_all)}"


@pytest.mark.browser
def test_thumb_endpoint_returns_decodable_jpeg(fake_server):
    """The previous app shipped a hand-crafted hex blob that Chromium refused
    to decode; this asserts the placeholder is real enough to render."""
    r = urllib.request.urlopen(f"{fake_server}/api/thumb/20260426/1777187350/1777187366").read()
    assert r[:2] == b"\xff\xd8", "thumb response not a JPEG"
    assert r[-2:] == b"\xff\xd9", "thumb JPEG missing EOI marker"


@pytest.mark.browser
def test_download_button_surfaces_a_result_toast(page, fake_server):
    page.goto(fake_server, timeout=10_000)
    page.wait_for_selector(".dot.connected", timeout=5_000)
    page.click("button[data-tab='archive']")
    page.fill("#arch-date", "2026-04-26")
    page.click("#btn-arch-load")
    page.wait_for_selector(".clip-card", timeout=5_000)
    page.click(".clip-card:first-child button[data-action='download']")
    # Either succeeds or fails — both surface a toast quickly.
    page.wait_for_selector("#toast:not(.hidden)", timeout=15_000)
    cls = page.get_attribute("#toast", "class") or ""
    assert "success" in cls or "error" in cls
    if "success" in cls:
        page.reload(timeout=10_000)
        page.click("button[data-tab='archive']")
        page.fill("#arch-date", "2026-04-26")
        page.click("#btn-arch-load")
        page.wait_for_selector(".clip-card.downloaded", timeout=5_000)
        assert page.text_content(".clip-card.downloaded .local-badge") == "DOWNLOADED ✓"
        assert "downloaded" in (page.text_content(".clip-card.downloaded .meta-dur") or "")
        page.click(".clip-card.downloaded button[data-action='watch']")
        page.wait_for_function(
            "() => document.querySelector('#player-video').src.includes('/recordings/')",
            timeout=5_000,
        )


@pytest.mark.browser
def test_purge_thumbs_works_through_ui(page, fake_server):
    page.goto(fake_server, timeout=10_000)
    page.wait_for_selector(".dot.connected", timeout=5_000)
    page.click("button[data-tab='settings']")
    page.on("dialog", lambda d: d.accept())
    page.click("button[data-cache='thumbs']")
    page.wait_for_selector("#toast:not(.hidden)", timeout=5_000)
    toast = page.text_content("#toast .toast-msg") or ""
    assert "purged thumbs" in toast.lower()


@pytest.fixture
def media_server(page, fake_media_tapo, cache_dir):
    """Real local HTTP server, simulated camera bytes, real FFmpeg and browser."""
    import asyncio
    import json
    import uvicorn
    from srv.config import Settings
    from srv.web import build_app

    fake_media_tapo.DEFAULT_CLIPS = {"20260426": [
        {"startTime": 1777187350, "endTime": 1777187352},
        {"startTime": 1777192872, "endTime": 1777192874},
    ]}
    original = fake_media_tapo.getMediaSession
    pulls = []

    def session(*args, **kwargs):
        result = original(*args, **kwargs)
        transceive = result.transceive
        async def slow(request, *a, **kw):
            params = json.loads(request).get("params", {})
            if "playback" in params:
                pulls.append(request)
            async for response in transceive(request, *a, **kw):
                await asyncio.sleep(.08)
                yield response
        result.transceive = slow
        return result

    fake_media_tapo.getMediaSession = session
    app = build_app(settings=Settings(host="127.0.0.1", user="u", password="p", cache_root=cache_dir),
                    tapo_factory=lambda _s: fake_media_tapo)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                          log_level="warning", loop="asyncio"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(.02)
    assert server.started
    try:
        yield base, app, fake_media_tapo, pulls
    finally:
        page.close()
        server.should_exit = True
        thread.join(timeout=5)
        assert not thread.is_alive(), "test server did not shut down"


@pytest.mark.browser
def test_manual_sync_pushes_progress_without_job_gets_and_survives_reload(page, media_server):
    base, app, camera, pulls = media_server
    requests, errors = [], []
    page.on("request", lambda r: requests.append((r.method, r.url)))
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script("""window.syncEvents = [];
      const OriginalEventSource = window.EventSource;
      window.EventSource = class extends OriginalEventSource {
        constructor(...args) {
          super(...args);
          this.addEventListener('message', e => {
            const event = JSON.parse(e.data);
            if (event.type === 'job') window.syncEvents.push(event.job);
          });
        }
      };""")
    page.goto(base)
    page.click("button[data-tab='archive']")
    page.fill("#arch-date", "2026-04-26")
    page.click("#btn-arch-load")
    page.wait_for_selector(".clip-card")
    page.click("#btn-arch-select-all")
    page.click("#btn-arch-bulk")
    page.wait_for_function("() => Object.values(S.state.jobs).some(j => j.status === 'running' && j.progress > 0 && j.progress < 1)")
    assert page.locator("#arch-status").is_visible()
    page.screenshot(path="/tmp/tapo-enhance-manual-sync-running.png", full_page=True)
    page.wait_for_function("() => Object.values(S.state.jobs).some(j => j.status === 'done')", timeout=20_000)
    snapshots = page.evaluate("window.syncEvents")
    assert any(0 < j["progress"] < 1 and j["status"] == "running" for j in snapshots)
    assert any(j["conversion_current"] for j in snapshots)
    assert any(j["done"] == 1 and j["status"] == "running" for j in snapshots)
    final = page.evaluate("Object.values(S.state.jobs)[0]")
    assert (final["done"], final["failed"], final["progress"]) == (2, 0, 1)
    assert page.locator(".clip-card.downloaded").count() == 2
    assert "Sync complete" in page.locator("#arch-status").inner_text()
    assert not app.state.settings.dvr_enabled
    assert len(pulls) == 2
    page.screenshot(path="/tmp/tapo-enhance-manual-sync-complete.png", full_page=True)
    # Refresh restores the server's job snapshot via SSE, without a job GET.
    page.reload()
    page.click("button[data-tab='archive']")
    page.wait_for_function("() => Object.values(S.state.jobs).some(j => j.status === 'done' && j.done === 2)")
    assert page.locator("#arch-status [role='progressbar']").get_attribute("aria-valuenow") == "100"
    assert not [u for method, u in requests if method == "GET" and "/api/jobs/" in u]
    assert len([u for method, u in requests if "/api/events" in u]) == 2
    assert errors == []
    page.fill("#arch-date", "2026-04-26")
    page.click("#btn-arch-load")
    page.wait_for_selector(".clip-card.downloaded")
    page.click(".clip-card.downloaded button[data-action='watch']")
    page.wait_for_function("() => document.querySelector('#player-video').readyState >= 2")
    assert "/recordings/" in page.locator("#player-video").get_attribute("src")
    assert page.evaluate("document.querySelector('#player-video').videoWidth") > 0
    assert len(pulls) == 2, "watching a downloaded clip contacted the camera"


@pytest.mark.browser
def test_manual_sync_failures_are_visible_and_do_not_poll(page, media_server):
    base, _app, camera, _pulls = media_server
    gets = []
    page.on("request", lambda r: gets.append(r.url) if r.method == "GET" and "/api/jobs/" in r.url else None)
    page.goto(base)
    page.click("button[data-tab='archive']")
    page.fill("#arch-date", "2026-04-26")
    page.click("#btn-arch-load")
    page.wait_for_selector(".clip-card")
    page.wait_for_function("() => S.state.archive.thumbCounts.ready === 2")
    def fail(*args, **kwargs):
        raise RuntimeError("camera media connection failed")
    camera.getMediaSession = fail
    page.click("#btn-arch-select-all")
    page.click("#btn-arch-bulk")
    page.wait_for_function("() => Object.values(S.state.jobs).some(j => j.status === 'done')")
    text = page.locator("#arch-status").inner_text()
    assert "2 failed" in text
    assert "camera media connection failed" in text
    assert "finished with errors" in text
    assert gets == []


@pytest.mark.browser
def test_slow_archive_response_does_not_replace_newer_date(page, fake_server):
    import json
    pending = []
    def delayed(route):
        pending.append(route)
    page.route("**/api/recordings/20260424", delayed)
    page.goto(fake_server)
    page.click("button[data-tab='archive']")
    page.fill("#arch-date", "2026-04-24")
    page.click("#btn-arch-load")
    page.wait_for_timeout(100)
    assert pending
    page.fill("#arch-date", "2026-04-26")
    page.click("#btn-arch-load")
    page.wait_for_selector(".clip-card")
    pending[0].fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "clips": []}))
    page.wait_for_timeout(100)
    assert page.locator(".clip-card").count() == 2
    assert page.evaluate("S.state.archive.date") == "20260426"


@pytest.mark.browser
def test_unsaved_dvr_settings_survive_navigation(page, fake_server):
    page.goto(fake_server)
    page.click("button[data-tab='settings']")
    page.wait_for_function("() => document.querySelector('#dvr-settings-form input[name=retention_days]').value !== ''")
    page.check("#dvr-settings-form input[name='enabled']")
    page.fill("#dvr-settings-form input[name='retention_days']", "23")
    page.click("button[data-tab='archive']")
    page.click("button[data-tab='settings']")
    page.wait_for_timeout(200)
    assert page.input_value("#dvr-settings-form input[name='retention_days']") == "23"


@pytest.mark.browser
def test_switching_tabs_before_live_attach_does_not_restart_player(page, fake_server):
    page.goto(fake_server)
    page.wait_for_selector(".dot.connected")
    page.route("**/api/stream/start", lambda r: r.fulfill(status=200, content_type="application/json", body='{"ok":true,"url":"/stream/fake.m3u8"}'))
    page.route("**/api/stream/stop", lambda r: r.fulfill(status=200, content_type="application/json", body='{"ok":true}'))
    page.evaluate("""() => { window.hlsLoads = 0;
      window.Hls = class {
        static isSupported() { return true; }
        static Events = { MANIFEST_PARSED: 'manifest', LEVEL_UPDATED: 'level', ERROR: 'error' };
        loadSource() { window.hlsLoads++; }
        attachMedia() {} on() {} destroy() {}
      }; }""")
    page.click("#btn-live-start")
    page.wait_for_function("() => S.state.live.status === 'running'")
    page.click("button[data-tab='archive']")
    page.wait_for_timeout(400)
    assert page.evaluate("window.hlsLoads") == 0
    assert page.evaluate("S.state.live.status") == "idle"


@pytest.mark.browser
def test_state_updates_preserve_thumbnail_and_feature_dom(page, fake_server):
    page.goto(fake_server)
    page.click("button[data-tab='archive']")
    page.fill("#arch-date", "2026-04-26")
    page.click("#btn-arch-load")
    page.wait_for_selector(".clip-card")
    page.evaluate("window.originalThumb = document.querySelector('.clip-card img')")
    page.click("#btn-arch-select-all")
    assert page.evaluate("window.originalThumb === document.querySelector('.clip-card img')")
    page.click("button[data-tab='settings']")
    page.wait_for_selector(".feature-row")
    page.evaluate("window.originalControls = document.querySelector('#feature-controls').firstElementChild")
    page.evaluate("S.set(s => { s.camera.alias = 'updated'; })")
    assert page.evaluate("window.originalControls === document.querySelector('#feature-controls').firstElementChild")


@pytest.mark.browser
def test_single_download_has_visible_progress_and_local_playback(page, media_server):
    base, _app, _camera, pulls = media_server
    page.goto(base)
    page.click("button[data-tab='archive']")
    page.fill("#arch-date", "2026-04-26")
    page.click("#btn-arch-load")
    page.wait_for_selector(".clip-card")
    page.click(".clip-card:first-child [data-action='download']")
    page.wait_for_function("() => Object.values(S.state.operations).some(e => e.operation === 'download' && !['complete', 'error'].includes(e.phase))")
    assert page.locator("#manual-sync-card").is_visible()
    page.wait_for_selector(".clip-card.downloaded", timeout=15_000)
    assert "1/1 ready" in page.locator("#arch-status").inner_text()
    assert "100%" in page.locator("#arch-status").inner_text()
    page.click(".clip-card.downloaded [data-action='watch']")
    page.wait_for_function("() => document.querySelector('#player-video').readyState >= 2")
    assert "/recordings/" in page.locator("#player-video").get_attribute("src")
    assert len(pulls) == 1


@pytest.mark.browser
def test_reload_during_manual_sync_restores_active_job(page, media_server):
    base, app, _camera, pulls = media_server
    page.goto(base)
    page.click("button[data-tab='archive']")
    page.fill("#arch-date", "2026-04-26")
    page.click("#btn-arch-load")
    page.wait_for_selector(".clip-card")
    page.click("#btn-arch-select-all")
    page.click("#btn-arch-bulk")
    page.wait_for_function("() => Object.values(S.state.jobs).some(j => j.status === 'running')")
    job_id = page.evaluate("Object.keys(S.state.jobs)[0]")
    assert app.state.jobs.get(job_id).status == "running"
    page.reload()
    page.click("button[data-tab='archive']")
    page.wait_for_function("id => !!S.state.jobs[id]", arg=job_id)
    assert page.locator("#manual-sync-card").is_visible()
    page.wait_for_function("id => S.state.jobs[id].status === 'done'", arg=job_id, timeout=20_000)
    assert page.evaluate("id => S.state.jobs[id].done", job_id) == 2
    assert len(pulls) == 2
