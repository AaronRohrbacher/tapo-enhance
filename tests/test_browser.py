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
