"""Exercise silent camera cancellation through actual runners and ffmpeg."""

import asyncio

import pytest

from srv.downloader import make_runner, make_fetch_runner
from srv.gateway import CameraGateway
from srv.errors import ApiError
from srv.discovery import looks_like_conn_error
from srv.live import camera_live_source
from srv.media import media_wait
from srv.playback import make_runner as playback_runner
from srv.recordings import Clip


CLIP = Clip("20990101", 1700000000, 1700000002)


@pytest.mark.parametrize("kind", ["playback", "download", "dvr", "live"])
@pytest.mark.parametrize("stall", ["connect", "responses"])
@pytest.mark.parametrize("interrupt", ["cancel", "timeout"])
async def test_silent_camera_cancel_closes_session_and_queue_continues(
    monkeypatch, paths, kind, stall, interrupt,
):
    entered = asyncio.Event()
    closed = asyncio.Event()
    never = asyncio.Event()
    connection_failures = []

    if interrupt == "timeout":
        async def short_wait(awaitable, cancel, *, timeout):
            return await media_wait(awaitable, cancel, timeout=0.02)

        for module in ("playback", "downloader", "live"):
            monkeypatch.setattr(f"srv.{module}.media_wait", short_wait)

    class SilentStream:
        def __init__(self, *args, **kwargs):
            pass

        async def start(self):
            if stall == "connect":
                entered.set()
                await never.wait()

        async def responses(self):
            entered.set()
            await never.wait()
            yield None

        async def close(self):
            closed.set()

    monkeypatch.setattr("pytapo.media_stream.recording_stream.RecordingStream", SilentStream)
    monkeypatch.setattr("pytapo.media_stream.preview_stream.PreviewStream", SilentStream)
    if kind == "playback":
        runner = playback_runner(CLIP, paths, asyncio.Event())
    elif kind == "download":
        runner = make_runner(CLIP, paths)
    elif kind == "dvr":
        runner = make_fetch_runner(CLIP, paths)
    else:
        runner = lambda t, c: camera_live_source(t, c, None)

    async def camera_error(exc):
        connection_failures.append(looks_like_conn_error(exc))

    g = CameraGateway(get_tapo=lambda: object(), on_camera_error=camera_error)
    await g.start()
    try:
        async def run(tapo, _cancel):
            return await runner(tapo, _cancel)

        future = g.submit_playback(CLIP, run)
        await asyncio.wait_for(entered.wait(), 2)
        cleanup = g.cancel_playback() if interrupt == "cancel" else []
        expected = asyncio.CancelledError if interrupt == "cancel" else (TimeoutError, ApiError)
        with pytest.raises(expected):
            await asyncio.wait_for(asyncio.shield(future), 2)
        assert closed.is_set()
        await asyncio.wait_for(asyncio.gather(*cleanup), 2)
        if interrupt == "timeout":
            assert connection_failures == [True]

        async def next_job(_t, _c):
            assert closed.is_set()
            return "queue recovered"

        # The gateway must recognise cooperative cancellation, not shutdown.
        assert await asyncio.wait_for(g.submit_query("next", next_job), 2) == "queue recovered"
    finally:
        await g.stop()


async def test_media_timeout_reaps_pending_io():
    cleaned = asyncio.Event()

    async def stalled():
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    with pytest.raises(TimeoutError):
        await media_wait(stalled(), asyncio.Event(), timeout=0.01)
    assert cleaned.is_set()


@pytest.mark.parametrize("operation", ["probe", "snapshot", "thumbnail"])
async def test_cancelled_media_helpers_reap_their_subprocess(paths, monkeypatch, operation):
    """Cancellation must reap the actual OS process, not just its Python task."""
    from srv import downloader, snapshot, thumbs
    entered = asyncio.Event()
    processes = []
    spawn = asyncio.create_subprocess_exec

    async def long_running_process(*args, **kwargs):
        proc = await spawn("ffmpeg", "-hide_banner", "-loglevel", "error", "-re",
                           "-f", "lavfi", "-i", "testsrc=s=16x16:r=1", "-f", "null", "-",
                           stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        processes.append(proc)
        entered.set()
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", long_running_process)
    if operation == "probe":
        work = downloader._probe(paths.recording(CLIP))
    elif operation == "snapshot":
        (paths.stream / "seg_00001.ts").write_bytes(b"x" * 8192)
        work = snapshot.from_recent_segment(paths)
    else:
        source = paths.recording(CLIP)
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"x" * 2048)
        work = thumbs.extract_from_local(CLIP, paths)
    task = asyncio.create_task(work)
    await asyncio.wait_for(entered.wait(), 2)
    assert processes[0].returncode is None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert processes[0].returncode is not None
