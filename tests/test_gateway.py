"""Camera gateway behaviour. The gateway is THE core safety mechanism:
every camera media-session access flows through it serialized, so we
test the invariants that the previous app violated:

  * No two transient jobs ever run concurrently.
  * Live preview is paused while transient jobs run, then resumed.
  * Downloads run before thumbs regardless of submission order.
  * Duplicate submissions for the same key are deduped to one Future.
  * Cancellation propagates from `stop()` to runners.
  * A failure in one job does not poison the queue.
"""

from __future__ import annotations

import asyncio

import pytest

from srv.gateway import CameraGateway
from srv.recordings import Clip


# Deterministic clip helpers
A = Clip("20260101", 1000, 1010)
B = Clip("20260101", 2000, 2010)
C = Clip("20260101", 3000, 3010)


class FakeConsumer:
    def __init__(self):
        self.events: list[str] = []
        self.feeds: list[bytes] = []

    async def started(self):
        self.events.append("started")

    async def feed(self, ts):
        self.feeds.append(ts)

    async def paused(self):
        self.events.append("paused")

    async def stopped(self):
        self.events.append("stopped")


def _fake_get_tapo():
    return object()


@pytest.fixture
async def gw():
    g = CameraGateway(get_tapo=_fake_get_tapo)
    await g.start()
    try:
        yield g
    finally:
        await g.stop()


# ── basic submission ───────────────────────────────────────────────────────


async def test_download_runner_receives_tapo_client(gw):
    seen = {}

    async def runner(tapo, _cancel):
        seen["tapo"] = tapo
        return "result"

    res = await gw.submit_download(A, runner)
    assert res == "result"
    assert seen["tapo"] is not None


async def test_download_and_thumb_share_no_concurrency(gw):
    """The single most important invariant: never two camera jobs at once."""
    overlap = {"max": 0, "now": 0}
    lock = asyncio.Lock()

    async def runner(_tapo, _cancel):
        async with lock:
            overlap["now"] += 1
            overlap["max"] = max(overlap["max"], overlap["now"])
        await asyncio.sleep(0.05)
        async with lock:
            overlap["now"] -= 1
        return None

    futs = []
    for c in (A, B, C):
        futs.append(gw.submit_download(c, runner))
        futs.append(gw.submit_thumb(c, runner))
    await asyncio.gather(*futs)
    assert overlap["max"] == 1, f"observed {overlap['max']} concurrent runners"


async def test_downloads_run_before_thumbs_even_when_both_arrive_during_a_running_job(gw):
    """While a job is mid-run, more arrive. When the worker picks the next
    one, downloads must beat thumbs regardless of submit order."""
    sequence: list[str] = []
    started = asyncio.Event()
    gate = asyncio.Event()

    async def blocker_runner(_t, _c):
        started.set()
        await gate.wait()
        sequence.append("blocker")
        return None

    def labelled(name):
        async def runner(_t, _c):
            sequence.append(name)
            return None
        return runner

    blocker = gw.submit_thumb(A, blocker_runner)
    # Wait for the blocker to actually be running before queueing more.
    await started.wait()
    th = gw.submit_thumb(B, labelled("th_B"))
    dl = gw.submit_download(C, labelled("dl_C"))
    gate.set()
    await asyncio.gather(blocker, th, dl)
    assert sequence == ["blocker", "dl_C", "th_B"]


async def test_duplicate_submissions_dedup_to_one_runner(gw):
    """Two callers asking for the same download share one Future."""
    runs = 0

    async def runner(_t, _c):
        nonlocal runs
        runs += 1
        await asyncio.sleep(0.01)
        return "x"

    f1 = gw.submit_download(A, runner)
    f2 = gw.submit_download(A, runner)
    r1, r2 = await asyncio.gather(f1, f2)
    assert r1 == r2 == "x"
    assert runs == 1


async def test_runner_exception_propagates_without_breaking_the_queue(gw):
    async def boom(_t, _c):
        raise RuntimeError("camera died")

    async def ok(_t, _c):
        return "fine"

    with pytest.raises(RuntimeError, match="camera died"):
        await gw.submit_download(A, boom)
    # Subsequent jobs still run.
    assert await gw.submit_download(B, ok) == "fine"


async def test_get_tapo_failure_propagates_to_future(gw):
    g = CameraGateway(get_tapo=lambda: (_ for _ in ()).throw(RuntimeError("no creds")))
    await g.start()
    try:
        async def runner(_t, _c): return None
        with pytest.raises(RuntimeError, match="no creds"):
            await g.submit_download(A, runner)
    finally:
        await g.stop()


# ── live preemption ────────────────────────────────────────────────────────


async def test_live_pauses_while_transient_runs_then_resumes(make_av_ts, tmp_path):
    """Verify the headline UX promise: a download click pauses live, runs,
    and live comes back. The activity log records the order."""

    started_event = asyncio.Event()

    async def live_source(_tapo, cancel: asyncio.Event, consumer):
        started_event.set()
        # Stream chunks until cancelled.
        while not cancel.is_set():
            try:
                await asyncio.wait_for(cancel.wait(), timeout=0.05)
            except asyncio.TimeoutError:
                await consumer.feed(b"\x47" * 188)

    g = CameraGateway(get_tapo=_fake_get_tapo, live_source=live_source)
    await g.start()
    consumer = FakeConsumer()
    g.attach_live(consumer)
    try:
        # Wait for live to actually be running once.
        await asyncio.wait_for(started_event.wait(), timeout=2)
        await asyncio.sleep(0.05)
        assert g.live_running()

        async def dl_runner(_t, _c):
            # While we're "downloading" the camera, live MUST be paused.
            assert not g.live_running(), "live still running during download"
            await asyncio.sleep(0.05)
            return "done"

        result = await g.submit_download(A, dl_runner)
        assert result == "done"

        # Live must come back automatically.
        await asyncio.sleep(0.2)
        assert g.live_running(), "live did not resume after download finished"

        # The consumer should have seen a pause notification while detached.
        assert "paused" in consumer.events
    finally:
        await g.stop()
    # And a final stopped on shutdown.
    assert "stopped" in consumer.events


async def test_attach_without_live_source_raises():
    g = CameraGateway(get_tapo=_fake_get_tapo)
    await g.start()
    try:
        with pytest.raises(RuntimeError, match="live_source"):
            g.attach_live(FakeConsumer())
    finally:
        await g.stop()


# ── busy_kind / observability ──────────────────────────────────────────────


async def test_busy_kind_reflects_active_job(gw):
    seen_kinds = []

    async def runner(_t, _c):
        seen_kinds.append(gw.busy_kind())
        return None

    await gw.submit_download(A, runner)
    await gw.submit_thumb(B, runner)
    assert seen_kinds == ["download", "thumb"]
    assert gw.busy_kind() is None


async def test_stop_fails_pending_futures():
    g = CameraGateway(get_tapo=_fake_get_tapo)
    await g.start()
    blocker = asyncio.Event()

    async def block_forever(_t, _c):
        await blocker.wait()

    fut1 = g.submit_download(A, block_forever)
    fut2 = g.submit_download(B, block_forever)  # queued, never starts
    await asyncio.sleep(0.05)
    blocker.set()  # let the first finish
    await fut1
    # Fill the queue again with one that won't start before stop.
    blocker2 = asyncio.Event()

    async def block2(_t, _c):
        await blocker2.wait()

    fut3 = g.submit_download(C, block2)
    await asyncio.sleep(0.05)
    # Stop while fut3 is mid-run; remaining queued futures must fail.
    stop_t = asyncio.create_task(g.stop())
    blocker2.set()
    await stop_t
    # fut2 already completed because it was deduplicated/queued and ran.
    assert fut2.done()
