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
from .events import EventHub
from .gateway import CameraGateway
from .jobs import JobRegistry
from .live import HlsConsumer, camera_live_source
from .playback import make_runner as playback_runner
from .playback import playlist_url
from .recordings import Clip, Paths
from .thumbs import ThumbBackfill, extract_from_local
from . import themes as themes_mod
from .dvr import ALLOWED_INTERVALS, DvrService, days_between
from .version import __version__


log = logging.getLogger("tapo")


def _battery_summary(raw) -> tuple[int | float | None, bool]:
    """Normalize the nested D225 response without treating missing as zero."""
    status = raw if isinstance(raw, dict) else {}
    nested = status.get("battery", {})
    if isinstance(nested, dict):
        status = nested.get("status", status)
    if not isinstance(status, dict):
        return None, False
    percent = status.get("battery_percent")
    charging_raw = status.get("battery_charging", status.get("is_charging", False))
    charging = (
        charging_raw.strip().upper() in {"YES", "TRUE", "ON", "CHARGING", "1"}
        if isinstance(charging_raw, str)
        else bool(charging_raw)
    )
    return percent if isinstance(percent, (int, float)) else None, charging


def _nested(raw, *keys, default=None):
    value = raw
    for key in keys:
        if not isinstance(value, dict):
            return default
        value = value.get(key)
    return default if value is None else value


def _on(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"on", "yes", "true", "1", "enabled", "auto"}
    return None


class HlsStaticFiles(StaticFiles):
    """Serve manifests as always-fresh and completed segments as immutable."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if path.endswith(".m3u8"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        elif path.endswith(".ts"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def build_app(*, settings: Settings | None = None, tapo_factory=None) -> FastAPI:
    settings = settings or Settings.from_env()
    paths = Paths(settings.cache_root)
    paths.ensure()
    settings.load_persisted_config()
    settings.load_persisted_mac()
    settings.load_persisted_host()

    def _rediscover_ip():
        # Camera connection failed — find where the doorbell actually lives now
        # (ARP-by-MAC first, then an nmap port-8800 fingerprint sweep).
        return discovery_mod.discover_ip(settings.subnet, settings.mac)

    camera = CameraConnection(
        settings, tapo_factory=tapo_factory, rediscover=_rediscover_ip
    )
    hub = EventHub()

    async def _on_camera_error(exc):
        # A camera op in the gateway (live/thumb/download) failed. If it's a
        # connection error the doorbell likely slept and the cached client is
        # stale — drop it so the next attempt rebuilds and WoL-wakes the camera.
        if discovery_mod.looks_like_conn_error(exc):
            camera.invalidate()

    gateway = CameraGateway(
        get_tapo=camera.get,
        live_source=camera_live_source,
        on_event=hub.emit,
        on_camera_error=_on_camera_error,
    )
    backfill = ThumbBackfill(paths, gateway, on_event=hub.emit)
    jobs = JobRegistry(paths, gateway)
    dvr = DvrService(
        settings, paths, camera.with_retry, gateway, on_event=hub.emit,
    )
    hls_consumer = HlsConsumer(paths.stream, on_event=hub.emit)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await gateway.start()
        backfill.start()
        if settings.dvr_enabled:
            dvr.start()
        try:
            yield
        finally:
            await gateway.detach_live()
            await backfill.stop()
            await dvr.stop()
            await gateway.stop()

    root = Path(__file__).parent.parent
    static_dir = root / "static"
    template_dir = root / "templates"
    theme_dir = root / "themes"

    app = FastAPI(title="Tapo Enhance", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.paths = paths
    app.state.camera = camera
    app.state.gateway = gateway
    app.state.backfill = backfill
    app.state.jobs = jobs
    app.state.hls = hls_consumer
    app.state.hub = hub
    app.state.dvr = dvr

    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")
    app.mount("/recordings", StaticFiles(directory=paths.recordings), name="recordings")
    app.mount("/stream", HlsStaticFiles(directory=paths.stream), name="stream_files")
    app.mount("/thumbs", StaticFiles(directory=paths.thumbs), name="thumbs")
    app.mount("/previews", StaticFiles(directory=paths.previews), name="previews")
    app.mount("/playback", HlsStaticFiles(directory=paths.playback), name="playback")
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
        for f in ("style.css", "favicon.svg", "state.js", "api.js", "sfx.js", "ui.js", "main.js"):
            p = static_dir / f
            try:
                v = max(v, int(p.stat().st_mtime))
            except OSError:
                pass
        resp = templates.TemplateResponse(
            request, "index.html", {"asset_v": v, "app_version": __version__}
        )
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
        return resp

    @app.get("/api/themes")
    async def themes():
        return {"ok": True, "themes": themes_mod.load(theme_dir)}

    @app.get("/api/app")
    async def app_info():
        return {"ok": True, "name": "Tapo Enhance", "version": __version__,
                "configured": settings.configured(), "vault_exists": settings.vault_exists(),
                "locked": settings.vault_exists() and not settings.configured()}

    @app.get("/api/config")
    async def config_status():
        return {
            "ok": True, "configured": settings.configured(), "host": settings.host,
            "user": settings.user, "subnet": settings.subnet,
            "vault_exists": settings.vault_exists(),
        }

    @app.get("/api/dvr")
    async def dvr_status():
        camera_history_error = None
        if settings.configured():
            try:
                dates = await _to_thread(
                    camera.with_retry,
                    lambda t: rec_mod.parse_dates(t.getRecordingsList()),
                )
                dvr._camera_oldest = min(dates) if dates else None
            except Exception as exc:
                dvr._last_error = f"{type(exc).__name__}: {exc}"
                camera_history_error = str(exc) or type(exc).__name__
        result = dvr.config()
        if result["retention_days"] is None:
            result["retention_days"] = max(1, result["camera_available_days"])
        return {"ok": True, **result, "camera_history_error": camera_history_error}

    @app.post("/api/dvr")
    async def configure_dvr(req: Request):
        body = await req.json()
        enabled = body.get("enabled", False)
        keep_forever = body.get("keep_forever", False)
        interval = body.get("interval_minutes", 1440)
        daily_time = str(body.get("daily_time") or "00:10")
        days = body.get("retention_days")
        if not isinstance(enabled, bool) or not isinstance(keep_forever, bool):
            raise ApiError(Code.BAD_REQUEST, "DVR settings have invalid values", status=400)
        try:
            retention_days = int(days)
        except (TypeError, ValueError):
            raise ApiError(Code.BAD_REQUEST, "DVR history must be at least 1 day", status=400)
        if not 1 <= retention_days <= 36500:
            raise ApiError(Code.BAD_REQUEST, "DVR history must be between 1 and 36500 days", status=400)
        try:
            interval_minutes = int(interval)
            datetime.strptime(daily_time, "%H:%M")
        except (TypeError, ValueError):
            raise ApiError(Code.BAD_REQUEST, "DVR schedule is invalid", status=400)
        if interval_minutes not in ALLOWED_INTERVALS:
            raise ApiError(Code.BAD_REQUEST, "unsupported DVR check interval", status=400)
        try:
            return {"ok": True, **await dvr.configure(
                enabled=enabled, retention_days=retention_days,
                keep_forever=keep_forever, interval_minutes=interval_minutes,
                daily_time=daily_time,
            )}
        except OSError as exc:
            raise ApiError(Code.INTERNAL, "DVR settings could not be saved", status=500) from exc

    @app.post("/api/dvr/sync")
    async def sync_dvr_now():
        if not settings.dvr_enabled:
            raise ApiError(Code.BAD_REQUEST, "enable DVR Mode before syncing", status=400)
        started = dvr.trigger_sync()
        return {"ok": True, "started": started,
                "message": "sync started" if started else "sync already running"}

    @app.get("/api/discovery")
    async def discover(subnet: str = ""):
        try:
            networks = [discovery_mod.validate_subnet(subnet)] if subnet.strip() else discovery_mod.local_subnets()
        except ValueError as exc:
            raise ApiError(Code.BAD_REQUEST, str(exc), status=400)
        if not networks:
            raise ApiError(Code.BAD_REQUEST,
                           "could not determine the LAN subnet; enter it manually",
                           status=400)
        found = []
        seen = set()
        for network in networks:
            for candidate in await _to_thread(discovery_mod.discover_candidates, network):
                if candidate["ip"] not in seen:
                    candidate["known_camera"] = bool(
                        settings.mac and candidate.get("mac") == discovery_mod.normalize_mac(settings.mac)
                    )
                    found.append(candidate)
                    seen.add(candidate["ip"])
        return {"ok": True, "subnets": networks, "cameras": found}

    @app.post("/api/config")
    async def configure(req: Request):
        body = await req.json()
        host = str(body.get("host") or "").strip()
        user = str(body.get("user") or "admin").strip()
        password = str(body.get("password") or "")
        subnet = str(body.get("subnet") or settings.subnet or "").strip()
        mac = discovery_mod.normalize_mac(str(body.get("mac") or ""))
        if not host or not user or not password:
            raise ApiError(Code.BAD_REQUEST, "camera address, user, and password are required", status=400)
        if len(host) > 255 or len(user) > 255 or len(password) > 1024 or len(subnet) > 64:
            raise ApiError(Code.BAD_REQUEST, "configuration value is too long", status=400)
        old = (
            settings.host, settings.user, settings.password,
            settings.cloud_password, settings.subnet,
        )
        if mac:
            settings.remember_mac(mac)
        # Battery doorbells such as the D225 may authenticate local control as
        # `admin` with the TP-Link password even when Camera Account is disabled.
        # Media-session encryption requires that same password separately.
        users = list(dict.fromkeys((user, "admin")))
        last_error = None
        for candidate_user in users:
            settings.host, settings.user, settings.password = host, candidate_user, password
            settings.cloud_password, settings.subnet = password, subnet
            camera.invalidate()
            try:
                basic = await _to_thread(camera.with_retry, lambda t: t.getBasicInfo())
                history_error = None
                try:
                    dates = await _to_thread(
                        camera.with_retry,
                        lambda t: rec_mod.parse_dates(t.getRecordingsList()),
                    )
                    camera_oldest = min(dates) if dates else None
                    camera_days = days_between(camera_oldest)
                    dvr._camera_oldest = camera_oldest
                except Exception as exc:
                    camera_oldest, camera_days = None, 0
                    history_error = f"{type(exc).__name__}: {exc}"
                try:
                    settings.save_camera_config(
                        host=host, user=candidate_user, password=password,
                        cloud_password=password, subnet=subnet,
                    )
                except OSError as exc:
                    raise ApiError(
                        Code.INTERNAL,
                        "camera connected, but encrypted credential storage is not writable",
                        status=500,
                    ) from exc
                return {
                    "ok": True, "configured": True, "host": settings.host,
                    "user": candidate_user,
                    "alias": _nested(basic, "device_info", "basic_info", "device_alias", default="camera"),
                    "camera_oldest_date": camera_oldest,
                    "camera_available_days": camera_days,
                    "history_error": history_error,
                }
            except Exception as exc:
                last_error = exc
                if not discovery_mod.looks_like_auth_error(exc):
                    break
        (
            settings.host, settings.user, settings.password,
            settings.cloud_password, settings.subnet,
        ) = old
        camera.invalidate()
        raise _classify(last_error or RuntimeError("camera login failed"))

    # ── camera info ────────────────────────────────────────────────────

    async def _to_thread(fn, *args, **kwargs):
        return await asyncio.get_event_loop().run_in_executor(None, lambda: fn(*args, **kwargs))

    @app.get("/api/info")
    async def info():
        def op(t):
            basic = t.getBasicInfo()
            try:
                bat = t.getBatteryStatus()
            except Exception:
                bat = None
            mac = basic.get("device_info", {}).get("basic_info", {}).get("mac")
            if mac:
                settings.remember_mac(mac)
            return {"ok": True, "info": basic, "battery": bat, "host": settings.host}
        try:
            return await _to_thread(camera.with_retry, op)
        except ApiError:
            raise
        except Exception as e:
            raise _classify(e)

    @app.get("/api/camera/status")
    async def camera_status():
        def op(t):
            basic = t.getBasicInfo()
            di = basic.get("device_info", {}).get("basic_info", {})
            try:
                battery = t.getBatteryStatus() or {}
            except Exception:
                battery = {}
            battery_percent, is_charging = _battery_summary(battery)
            if di.get("mac"):
                settings.remember_mac(di["mac"])
            return {
                "ok": True,
                "alias": di.get("device_alias", "?"),
                "model": di.get("device_model", "?"),
                "sw": di.get("sw_version", "?"),
                "hw": di.get("hw_version", "?"),
                "mac": di.get("mac", "?"),
                "host": settings.host,
                "battery_percent": battery_percent,
                "is_charging": is_charging,
            }
        try:
            return await _to_thread(camera.with_retry, op)
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

    @app.get("/api/camera/features")
    async def camera_features():
        """Curated, read-only D225 capabilities; one failed getter cannot hide the rest."""
        def op(t):
            values, errors = {}, {}
            getters = {
                "led": "getLED", "privacy": "getPrivacyMode", "sd": "getSDCard",
                "record_plan": "getRecordPlan", "loop": "getCircularRecordingConfig",
                "motion": "getMotionDetection", "person": "getPersonDetection",
                "package": "getPackageDetection", "record_audio": "getAudioConfig",
                "video": "getVideoQualities", "day_night": "getDayNightMode",
                "ring": "getRingStatus", "battery_mode": "getBatteryOperatingMode",
                "firmware": "getFirmwareUpdateStatus",
            }
            for key, method in getters.items():
                try:
                    values[key] = getattr(t, method)()
                except Exception as exc:
                    errors[key] = f"{type(exc).__name__}: {exc}"
            sd_rows = values.get("sd") or []
            sd = sd_rows[0].get("hd_info_1", {}) if sd_rows and isinstance(sd_rows[0], dict) else {}
            audio = _nested(values.get("record_audio"), "audio_config", default={})
            video = _nested(values.get("video"), "video", "main", default={})
            return {
                "ok": True,
                "storage": {
                    "status": sd.get("status"), "total": sd.get("total_space"),
                    "free": sd.get("free_space"), "writable": sd.get("rw_attr") == "rw",
                    "loop_recording": _on(_nested(values.get("loop"), "loop")),
                    "recording_enabled": _on(_nested(values.get("record_plan"), "enabled")),
                },
                "video": {
                    "resolution": video.get("true_resolution") or video.get("resolution"),
                    "codec": video.get("encode_type"), "bitrate_kbps": video.get("bitrate"),
                    "record_audio": _on(_nested(audio, "record_audio", "enabled")),
                },
                "power": {
                    "mode": _nested(values.get("battery_mode"), "battery", "operating", "mode"),
                },
                "firmware": {
                    "state": _nested(values.get("firmware"), "cloud_config", "upgrade_status", "state"),
                    "last_upgrade_succeeded": _nested(values.get("firmware"), "cloud_config", "upgrade_status", "lastUpgradingSuccess"),
                },
                "controls": {
                    "led": _on(_nested(values.get("led"), "enabled", default=_nested(values.get("led"), "value"))),
                    "privacy": _on(_nested(values.get("privacy"), "enabled", default=_nested(values.get("privacy"), "value"))),
                    "motion": _on(_nested(values.get("motion"), "enabled")),
                    "person": _on(_nested(values.get("person"), "enabled")),
                    "package": _on(_nested(values.get("package"), "enabled")),
                    "record_audio": _on(_nested(audio, "record_audio", "enabled")),
                    "ring": _on(_nested(values.get("ring"), "ring", "status", "enabled")),
                    "day_night": values.get("day_night"),
                },
                "errors": errors,
            }
        try:
            return await _to_thread(camera.with_retry, op)
        except Exception as e:
            raise _classify(e)

    @app.post("/api/camera/feature/{name}")
    async def set_camera_feature(name: str, req: Request):
        body = await req.json()
        boolean_setters = {
            "led": ("setLEDEnabled", {}),
            "motion": ("setMotionDetection", {"enabled": None}),
            "person": ("setPersonDetection", {}),
            "package": ("setPackageDetection", {}),
            "record_audio": ("setRecordAudio", {}),
            "ring": ("setRingStatus", {}),
        }
        if name == "day_night":
            value = str(body.get("value") or "").lower()
            if value not in {"auto", "day", "night"}:
                raise ApiError(Code.BAD_REQUEST, "day/night must be auto, day, or night", status=400)
            call = lambda t: t.setDayNightMode(value)
        elif name in boolean_setters and isinstance(body.get("value"), bool):
            method, kwargs = boolean_setters[name]
            value = body["value"]
            call = lambda t: getattr(t, method)(**{"enabled": value}) if kwargs else getattr(t, method)(value)
        else:
            raise ApiError(Code.BAD_REQUEST, f"unsupported feature or value: {name}", status=400)
        try:
            await _to_thread(camera.with_retry, call)
            return {"ok": True, "feature": name, "value": value}
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

    @app.get("/api/events")
    async def events_sse():
        """Single push channel for UI state. Replaces the old gateway-status
        (2s) and per-thumbnail HEAD (1.5s) polling loops — clients receive an
        event only when state actually changes, so idle == no traffic."""
        q = hub.subscribe()

        async def gen():
            try:
                # Send the current gateway snapshot immediately so a freshly
                # connected UI is correct without waiting for the next change.
                yield f"data: {json.dumps(gateway.status())}\n\n"
                while True:
                    evt = await q.get()
                    if evt is EventHub.CLOSE:
                        return
                    yield f"data: {json.dumps(evt)}\n\n"
            finally:
                hub.unsubscribe(q)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ── recordings listing ─────────────────────────────────────────────

    @app.get("/api/recordings/dates")
    async def recording_dates(start: str = "", end: str = ""):
        if not start:
            start = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
        if not end:
            end = datetime.now().strftime("%Y%m%d")

        def op(t):
            raw = t.getRecordingsList(start_date=start, end_date=end)
            return {"ok": True, "dates": rec_mod.parse_dates(raw)}
        try:
            return await _to_thread(camera.with_retry, op)
        except Exception as e:
            raise _classify(e)

    @app.get("/api/recordings/all")
    async def all_recordings(days: int = 30):
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")

        def op(t):
            raw = t.getRecordingsList(start_date=start, end_date=end)
            dates = rec_mod.parse_dates(raw)
            clips: list[dict] = []
            for d in dates:
                try:
                    rraw = t.getRecordings(d)
                    clips.extend(c.to_json() for c in rec_mod.parse_clips(rraw, d))
                except Exception:
                    continue
            return {"ok": True, "clips": clips, "dates": dates}
        try:
            return await _to_thread(camera.with_retry, op)
        except Exception as e:
            raise _classify(e)

    @app.get("/api/recordings/local")
    async def local_recordings():
        return {"ok": True, "files": rec_mod.list_local(paths)}

    @app.get("/api/recordings/{date}")
    async def recordings_for_date(date: str):
        def op(t):
            raw = t.getRecordings(date)
            return {"ok": True, "clips": [c.to_json() for c in rec_mod.parse_clips(raw, date)]}
        try:
            return await _to_thread(camera.with_retry, op)
        except Exception as e:
            raise _classify(e)

    # ── single download / watch ────────────────────────────────────────

    @app.post("/api/recordings/download")
    async def download(req: Request):
        body = await req.json()
        clip = _parse_clip(body)
        out = paths.recording(clip)
        was_cached = out.exists() and out.stat().st_size > 0
        try:
            await gateway.submit_download(
                clip, download_runner(clip, paths, on_progress=hub.emit, operation="download")
            )
            return {"ok": True, "file": f"/recordings/{clip.date}/{out.name}",
                    "cached": was_cached}
        except ApiError:
            raise
        except Exception as e:
            raise _classify(e)

    @app.get("/api/recordings/stream/{date}/{start_time}/{end_time}")
    async def stream_recording(date: str, start_time: int, end_time: int):
        clip = Clip(date, start_time, end_time)
        archived = paths.recording(clip)
        out = archived if archived.exists() and archived.stat().st_size > 0 else paths.preview(clip)
        if out.exists() and out.stat().st_size > 0:
            return FileResponse(out, media_type="video/mp4")
        try:
            await gateway.submit_playback(
                clip,
                download_runner(
                    clip, paths, destination=out, on_progress=hub.emit, operation="playback"
                ),
            )
        except ApiError:
            raise
        except Exception as e:
            raise _classify(e)
        if not (out.exists() and out.stat().st_size > 0):
            raise ApiError(Code.DOWNLOAD_EMPTY, "pull completed but produced no file", status=502)
        return FileResponse(out, media_type="video/mp4")

    @app.post("/api/recordings/play")
    async def play_recording(req: Request):
        """Start a transient HLS stream and return after its first segment.

        Watching never writes into the recordings archive. The gateway keeps
        the camera session serialized in the background until this one event
        reaches its exact duration or the user cancels it.
        """
        body = await req.json()
        clip = _parse_clip(body)
        ready = asyncio.Event()
        future = gateway.submit_playback(
            clip,
            playback_runner(clip, paths, ready, on_progress=hub.emit),
        )
        # The request returns at first-segment readiness while this Future
        # intentionally continues. Always observe its eventual exception;
        # detailed failure state is simultaneously pushed over EventSource.
        future.add_done_callback(
            lambda completed: None
            if completed.cancelled()
            else completed.exception()
        )
        ready_task = asyncio.create_task(ready.wait())
        try:
            done, _pending = await asyncio.wait(
                (ready_task, future),
                timeout=30,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if future in done:
                # Raises the original structured failure, if any. A very short
                # clip may also finish before the scheduling turn sees ready.
                await future
            if ready.is_set():
                return {"ok": True, "url": playlist_url(clip)}
            gateway.cancel_playback()
            raise ApiError(
                Code.GATEWAY_TIMEOUT,
                "camera did not produce the first playback segment in 30 seconds",
                status=504,
            )
        except ApiError:
            raise
        except Exception as e:
            raise _classify(e)
        finally:
            if not ready_task.done():
                ready_task.cancel()

    @app.post("/api/recordings/play/stop")
    async def stop_recording_playback():
        pending = gateway.cancel_playback()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        return {"ok": True}

    # ── thumbs + previews ──────────────────────────────────────────────

    _PLACEHOLDER = _build_placeholder(paths.root.parent / ".placeholder.jpg")

    @app.api_route("/api/thumb/{date}/{start_time}/{end_time}", methods=["GET", "HEAD"])
    async def thumb_endpoint(date: str, start_time: int, end_time: int):
        clip = Clip(date, start_time, end_time)
        existing = paths.thumb(clip)
        if existing.exists() and existing.stat().st_size > 100:
            # Push readiness so the UI counts/badges update without polling —
            # disk-cached thumbs never go through the backfill queue's emit.
            hub.emit({"type": "thumb", "key": clip.key, "status": "ready", "error": None})
            return FileResponse(existing, media_type="image/jpeg",
                                headers={"X-Thumb-Status": "ready",
                                         "Cache-Control": "public, max-age=86400"})
        # Fast path: extract from cached recording without a camera pull.
        local = await extract_from_local(clip, paths)
        if local is not None:
            hub.emit({"type": "thumb", "key": clip.key, "status": "ready", "error": None})
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
        # Push the current status so a client that just loaded this <img>
        # learns it — including already-terminal (failed) thumbs, whose
        # backfill.request() early-returns without emitting. Without this, a
        # revisited date leaves those badges stuck at the default "queued".
        hub.emit({"type": "thumb", "key": clip.key, "status": status, "error": st.get("error")})
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
        previous_generation = hls_consumer.generation
        gateway.attach_live(hls_consumer)
        # A stale playlist from the prior run is not readiness. Wait until the
        # new generation has actually written a playable manifest.
        for _ in range(80):
            if hls_consumer.generation > previous_generation and hls_consumer.playable:
                return {"ok": True, "url": hls_consumer.playback_url}
            await asyncio.sleep(0.25)
        await gateway.detach_live()
        error = hls_consumer.errors[-1] if hls_consumer.errors else "camera produced no playable segments"
        raise ApiError(Code.GATEWAY_TIMEOUT, f"live start timed out: {error}", status=504)

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
        if target in ("playback", "all"):
            pending = gateway.cancel_playback()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
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
