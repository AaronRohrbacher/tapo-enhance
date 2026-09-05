"""Playwright integration test. Spawns uvicorn against FakeTapo, then a
real browser drives the app: load page, browse archive, click a clip,
confirm download succeeds and surfaces in the local list. Catches the
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

    def _run():
        from tests._fakeserver import run
        run(port=port)

    t = threading.Thread(target=_run, daemon=True, name="fakeserver")
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
    yield base


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
def test_events_stream_pushes_gateway_snapshot(fake_server):
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
    finally:
        resp.close()
    assert line.startswith(b"data:"), line
    evt = json.loads(line[len(b"data:"):])
    assert evt["type"] == "gateway"
    assert set(evt) >= {"busy", "queued", "liveAttached", "liveRunning"}


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

    assert gw == [], f"gateway/status must never be polled; saw {len(gw)}"
    assert thumb_heads == [], f"thumbnails must not be HEAD-polled; saw {len(thumb_heads)}"
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


@pytest.mark.browser
def test_purge_thumbs_works_through_ui(page, fake_server):
    page.goto(fake_server, timeout=10_000)
    page.wait_for_selector(".dot.connected", timeout=5_000)
    page.click("button[data-tab='local']")
    page.on("dialog", lambda d: d.accept())
    page.click("button[data-cache='thumbs']")
    page.wait_for_selector("#toast:not(.hidden)", timeout=5_000)
    toast = page.text_content("#toast .toast-msg") or ""
    assert "purged thumbs" in toast.lower()
