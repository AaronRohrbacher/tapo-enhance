"""FastAPI app + routes. Thin glue that calls into the modules above.

Every route returns either:
  - a structured success body (`{"ok": true, ...}`), or
  - `JSONResponse({"ok": false, "code": "...", "error": "...", "retryable": bool})`
    via the `ApiError` exception handler.

There is exactly one ApiError-emitting code path; routes never construct
ad-hoc `{ok: false, error: ...}` dicts. That keeps the frontend's error
classification simple."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import cache as cache_mod
from . import discovery as discovery_mod
from . import recordings as rec_mod
from . import recovery as recovery_mod
from . import snapshot as snap_mod
from .camera import CameraConnection
from .config import Settings
from .downloader import make_runner as download_runner
from .errors import ApiError, Code
from .gateway import CameraGateway
from .jobs import JobRegistry
from .live import HlsConsumer, camera_live_source
from .recordings import Clip, Paths
from .thumbs import ThumbBackfill, extract_from_local


log = logging.getLogger("tapo")


def build_app(*, settings: Settings | None = None, tapo_factory=None) -> FastAPI:
    settings = settings or Settings.from_env()
    paths = Paths(settings.cache_root)
    paths.ensure()
    camera = CameraConnection(settings, tapo_factory=tapo_factory)
    gateway = CameraGateway(get_tapo=camera.get, live_source=camera_live_source)
    backfill = ThumbBackfill(paths, gateway)
    jobs = JobRegistry(paths, gateway)
    hls_consumer = HlsConsumer(paths.stream)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await gateway.start()
        backfill.start()
        try:
            yield
        finally:
            await gateway.detach_live()
            await backfill.stop()
            await gateway.stop()

    root = Path(__file__).parent.parent
    static_dir = root / "static"
    template_dir = root / "templates"

    app = FastAPI(title="Tapo D225 Viewer", lifespan=lifespan)
    app.state.settings = settings
    app.state.paths = paths
    app.state.camera = camera
    app.state.gateway = gateway
    app.state.backfill = backfill
    app.state.jobs = jobs
    app.state.hls = hls_consumer

    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")
    app.mount("/recordings", StaticFiles(directory=paths.recordings), name="recordings")
    app.mount("/stream", StaticFiles(directory=paths.stream), name="stream_files")
    app.mount("/thumbs", StaticFiles(directory=paths.thumbs), name="thumbs")
    app.mount("/previews", StaticFiles(directory=paths.previews), name="previews")
    templates = Jinja2Templates(directory=template_dir) if template_dir.exists() else None

    # ── error handler ──────────────────────────────────────────────────

    @app.exception_handler(ApiError)
    async def _api_error_handler(_req: Request, exc: ApiError):
        return JSONResponse(status_code=exc.status, content=exc.body())

    # ── UI ─────────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        if templates is None:
            return HTMLResponse("<h1>tapo viewer</h1><p>templates not built</p>")
        # Cache-bust static assets by mtime — every file the template
        # references must be in this list, or browsers will serve stale JS.
        v = 0
        for f in ("style.css", "state.js", "api.js", "sfx.js", "ui.js", "main.js"):
            p = static_dir / f
            try:
                v = max(v, int(p.stat().st_mtime))
            except OSError:
                pass
        resp = templates.TemplateResponse(request, "index.html", {"asset_v": v})
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
        return resp

    # ── camera info ────────────────────────────────────────────────────

    async def _to_thread(fn, *args, **kwargs):
        return await asyncio.get_event_loop().run_in_executor(None, lambda: fn(*args, **kwargs))

    @app.get("/api/info")
    async def info():
        try:
            t = await _to_thread(camera.get)
            basic = await _to_thread(t.getBasicInfo)
            try:
                bat = await _to_thread(t.getBatteryStatus)
            except Exception:
                bat = None
            return {"ok": True, "info": basic, "battery": bat, "host": settings.host}
        except ApiError:
            raise
        except Exception as e:
            raise _classify(e)

    @app.get("/api/camera/status")
    async def camera_status():
        try:
            t = await _to_thread(camera.get)
            basic = await _to_thread(t.getBasicInfo)
            di = basic.get("device_info", {}).get("basic_info", {})
            if di.get("mac"):
                settings.mac = di["mac"]
            return {
                "ok": True,
                "alias": di.get("device_alias", "?"),
                "model": di.get("device_model", "?"),
                "sw": di.get("sw_version", "?"),
                "hw": di.get("hw_version", "?"),
                "mac": di.get("mac", "?"),
                "host": settings.host,
            }
        except ApiError:
            raise
        except Exception as e:
            raise _classify(e)

    @app.post("/api/camera/reconnect")
    async def reconnect():
        camera.invalidate()
        try:
            t = await _to_thread(camera.get)
            await _to_thread(t.getBasicInfo)
            return {"ok": True}
        except Exception as e:
            raise _classify(e)

    @app.get("/api/camera/recover")
    async def recover_sse():
        async def gen():
            async for evt in recovery_mod.recover(camera):
                yield f"data: {json.dumps(evt)}\n\n"
        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ── recordings listing ─────────────────────────────────────────────

    @app.get("/api/recordings/dates")
    async def recording_dates(start: str = "", end: str = ""):
        try:
            t = await _to_thread(camera.get)
            if not start:
                start = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
            if not end:
                end = datetime.now().strftime("%Y%m%d")
            raw = await _to_thread(t.getRecordingsList, start_date=start, end_date=end)
            return {"ok": True, "dates": rec_mod.parse_dates(raw)}
        except Exception as e:
            raise _classify(e)

    @app.get("/api/recordings/all")
    async def all_recordings(days: int = 30):
        try:
            t = await _to_thread(camera.get)
            end = datetime.now().strftime("%Y%m%d")
            start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
            raw = await _to_thread(t.getRecordingsList, start_date=start, end_date=end)
            dates = rec_mod.parse_dates(raw)
            clips: list[dict] = []
            for d in dates:
                try:
                    rraw = await _to_thread(t.getRecordings, d)
                    clips.extend(c.to_json() for c in rec_mod.parse_clips(rraw, d))
                except Exception:
                    continue
            return {"ok": True, "clips": clips, "dates": dates}
        except Exception as e:
            raise _classify(e)

    @app.get("/api/recordings/local")
    async def local_recordings():
        return {"ok": True, "files": rec_mod.list_local(paths)}

    @app.get("/api/recordings/{date}")
    async def recordings_for_date(date: str):
        try:
            t = await _to_thread(camera.get)
            raw = await _to_thread(t.getRecordings, date)
            clips = [c.to_json() for c in rec_mod.parse_clips(raw, date)]
            return {"ok": True, "clips": clips}
        except Exception as e:
            raise _classify(e)

    # ── single download / watch ────────────────────────────────────────

    @app.post("/api/recordings/download")
    async def download(req: Request):
        body = await req.json()
        clip = _parse_clip(body)
        out = paths.recording(clip)
        try:
            res_path = await gateway.submit_download(clip, download_runner(clip, paths))
            return {"ok": True, "file": f"/recordings/{clip.date}/{out.name}",
                    "cached": False if res_path else True}
        except ApiError:
            raise
        except Exception as e:
            raise _classify(e)

    @app.get("/api/recordings/stream/{date}/{start_time}/{end_time}")
    async def stream_recording(date: str, start_time: int, end_time: int):
        clip = Clip(date, start_time, end_time)
        out = paths.recording(clip)
        if out.exists() and out.stat().st_size > 0:
            return FileResponse(out, media_type="video/mp4")
        try:
            await gateway.submit_download(clip, download_runner(clip, paths))
        except ApiError:
            raise
        except Exception as e:
            raise _classify(e)
        if not (out.exists() and out.stat().st_size > 0):
            raise ApiError(Code.DOWNLOAD_EMPTY, "pull completed but produced no file", status=502)
        return FileResponse(out, media_type="video/mp4")

    # ── thumbs + previews ──────────────────────────────────────────────

    _PLACEHOLDER = _build_placeholder(paths.root.parent / ".placeholder.jpg")

    @app.api_route("/api/thumb/{date}/{start_time}/{end_time}", methods=["GET", "HEAD"])
    async def thumb_endpoint(date: str, start_time: int, end_time: int):
        clip = Clip(date, start_time, end_time)
        existing = paths.thumb(clip)
        if existing.exists() and existing.stat().st_size > 100:
            return FileResponse(existing, media_type="image/jpeg",
                                headers={"X-Thumb-Status": "ready",
                                         "Cache-Control": "public, max-age=86400"})
        # Fast path: extract from cached recording without a camera pull.
        local = await extract_from_local(clip, paths)
        if local is not None:
            return FileResponse(local, media_type="image/jpeg",
                                headers={"X-Thumb-Status": "ready",
                                         "Cache-Control": "public, max-age=86400"})
        # Otherwise enqueue background pull and serve a placeholder. Never
        # advertise "ready" with placeholder content — that desyncs the UI.
        status = backfill.request(clip)
        if status == "ready":
            # In-memory state is stale (thumb file got deleted). Re-queue.
            backfill.requeue(clip)
            status = "queued"
        st = backfill.state(clip.key) or {}
        return Response(
            content=_PLACEHOLDER,
            media_type="image/jpeg",
            headers={
                "X-Thumb-Status": status,
                **({"X-Thumb-Error": (st.get("error") or "")[:240]} if st.get("error") else {}),
                "Cache-Control": "no-store",
            },
        )

    @app.get("/api/thumb-status/{date}")
    async def thumb_status(date: str):
        rows = backfill.all_state_for_date(date)
        counts = {"queued": 0, "running": 0, "ready": 0, "failed": 0, "total": len(rows), "errors": []}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
            if r["status"] == "failed" and r.get("error"):
                counts["errors"].append({"key": r["key"], "error": r["error"]})
        return {"ok": True, "counts": counts}

    @app.post("/api/thumb-retry/{date}")
    async def thumb_retry(date: str):
        n = backfill.retry_failed_for_date(date)
        return {"ok": True, "requeued": n}

    @app.get("/api/preview/{date}/{start_time}/{end_time}")
    async def get_preview(date: str, start_time: int, end_time: int):
        clip = Clip(date, start_time, end_time)
        p = paths.preview(clip)
        if p.exists() and p.stat().st_size > 1024:
            return FileResponse(p, media_type="video/mp4")
        return Response(status_code=204)

    # ── live ───────────────────────────────────────────────────────────

    @app.post("/api/stream/start")
    async def stream_start():
        gateway.attach_live(hls_consumer)
        # Wait briefly for the playlist to land so the UI can attach HLS.
        for _ in range(60):
            if hls_consumer.playlist_path.exists():
                return {"ok": True, "url": "/stream/output.m3u8"}
            await asyncio.sleep(0.25)
        return {"ok": True, "url": "/stream/output.m3u8", "note": "playlist not yet ready"}

    @app.post("/api/stream/stop")
    async def stream_stop():
        await gateway.detach_live()
        return {"ok": True}

    @app.get("/api/stream/status")
    async def stream_status():
        return {"running": gateway.live_attached() and gateway.live_running()}

    @app.get("/api/snapshot")
    async def snapshot():
        data = await snap_mod.from_recent_segment(paths)
        return Response(content=data, media_type="image/jpeg")

    # ── bulk + jobs ────────────────────────────────────────────────────

    @app.post("/api/recordings/bulk")
    async def bulk(req: Request):
        body = await req.json()
        clips: list[Clip] = []
        if body.get("clips"):
            for c in body["clips"]:
                clips.append(_parse_clip(c))
        elif body.get("date"):
            try:
                t = await _to_thread(camera.get)
                raw = await _to_thread(t.getRecordings, body["date"])
                clips = rec_mod.parse_clips(raw, body["date"])
            except Exception as e:
                raise _classify(e)
        elif body.get("all"):
            days = int(body.get("days") or 30)
            try:
                t = await _to_thread(camera.get)
                end = datetime.now().strftime("%Y%m%d")
                start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
                raw = await _to_thread(t.getRecordingsList, start_date=start, end_date=end)
                for d in rec_mod.parse_dates(raw):
                    try:
                        rraw = await _to_thread(t.getRecordings, d)
                        clips.extend(rec_mod.parse_clips(rraw, d))
                    except Exception:
                        continue
            except Exception as e:
                raise _classify(e)
        if not clips:
            raise ApiError(Code.BAD_REQUEST, "no clips to download", status=400)
        job = jobs.submit_bulk(clips)
        return {"ok": True, "job_id": job.id, "total": job.total}

    @app.get("/api/jobs/{job_id}")
    async def job_status(job_id: str):
        j = jobs.get(job_id)
        if not j:
            raise ApiError(Code.NOT_FOUND, "unknown job", status=404)
        return {"ok": True, "job": j.to_dict()}

    @app.post("/api/jobs/{job_id}/cancel")
    async def job_cancel(job_id: str):
        if not jobs.cancel(job_id):
            raise ApiError(Code.NOT_FOUND, "unknown or finished job", status=404)
        return {"ok": True}

    # ── cache ──────────────────────────────────────────────────────────

    @app.get("/api/cache/usage")
    async def cache_usage_endpoint():
        return {"ok": True, "usage": rec_mod.cache_usage(paths)}

    @app.post("/api/cache/clear")
    async def cache_clear(req: Request):
        body = await req.json()
        target = (body.get("target") or "").lower()
        if target in ("stream", "all"):
            await gateway.detach_live()
        return {"ok": True, **cache_mod.clear(paths, target)}

    # ── settings (camera dump + setters) ───────────────────────────────

    @app.post("/api/camera/privacy")
    async def privacy(req: Request):
        body = await req.json()
        try:
            t = await _to_thread(camera.get)
            await _to_thread(t.setPrivacyMode, bool(body.get("enabled")))
            return {"ok": True}
        except Exception as e:
            raise _classify(e)

    @app.get("/api/camera/events")
    async def camera_events(hours: int = 24):
        import time as _t
        try:
            t = await _to_thread(camera.get)
            end = int(_t.time())
            start = end - hours * 3600
            ev = await _to_thread(t.getEvents, startTime=start, endTime=end)
            return {"ok": True, "events": ev}
        except Exception as e:
            raise _classify(e)

    @app.post("/api/camera/reboot")
    async def reboot():
        try:
            t = await _to_thread(camera.get)
            await _to_thread(t.reboot)
            return {"ok": True}
        except Exception as e:
            raise _classify(e)

    @app.get("/api/gateway/status")
    async def gateway_status():
        return {
            "ok": True,
            "live_attached": gateway.live_attached(),
            "live_running": gateway.live_running(),
            "queued": gateway.queued_count(),
            "busy": gateway.busy_kind(),
        }

    return app


# ── helpers ────────────────────────────────────────────────────────────────


def _parse_clip(body: dict) -> Clip:
    try:
        date = str(body.get("date") or "")
        start = int(body["startTime"])
        end = int(body["endTime"])
    except (KeyError, TypeError, ValueError) as e:
        raise ApiError(Code.BAD_REQUEST, f"bad clip body: {e}", status=400)
    if not date:
        date = datetime.fromtimestamp(start).strftime("%Y%m%d")
    return Clip(date, start, end)


def _classify(exc: BaseException) -> ApiError:
    if isinstance(exc, ApiError):
        return exc
    if discovery_mod.looks_like_auth_error(exc):
        return ApiError(Code.CAMERA_AUTH, str(exc), status=502)
    if discovery_mod.looks_like_conn_error(exc):
        return ApiError(Code.CAMERA_OFFLINE, str(exc), status=502)
    return ApiError(Code.INTERNAL, f"{type(exc).__name__}: {exc}", status=500)


def _build_placeholder(out: Path) -> bytes:
    """A real, browser-decodable JPEG. Hand-rolled hex blobs failed in
    Chromium; ffmpeg is the easy way to guarantee a valid file."""
    if not out.exists() or out.stat().st_size < 100:
        try:
            import subprocess
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error",
                 "-f", "lavfi", "-i", "color=c=0x101814:s=320x240:d=0.04",
                 "-frames:v", "1", "-q:v", "8", str(out)],
                check=True, capture_output=True,
            )
        except Exception:
            return b""
    try:
        return out.read_bytes()
    except OSError:
        return b""
