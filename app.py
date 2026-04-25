import asyncio
import functools
import os
import signal
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pytapo import Tapo

load_dotenv()

TAPO_HOST = os.getenv("TAPO_HOST", "")
TAPO_USER = os.getenv("TAPO_USER", "admin")
TAPO_PASSWORD = os.getenv("TAPO_PASSWORD", "")
TAPO_CLOUD_PASSWORD = os.getenv("TAPO_CLOUD_PASSWORD", "")
TAPO_CHILD_ID = os.getenv("TAPO_CHILD_ID") or None

RECORDINGS_DIR = Path(__file__).parent / "recordings"
STREAM_DIR = Path(__file__).parent / "stream"
RECORDINGS_DIR.mkdir(exist_ok=True)
STREAM_DIR.mkdir(exist_ok=True)

tapo: Tapo | None = None
ffmpeg_process: subprocess.Popen | None = None
stream_lock = asyncio.Lock()
_pool = ThreadPoolExecutor(max_workers=4)


async def run_in_thread(fn, *args, **kwargs):
    """Run a blocking pytapo call in a thread to avoid event loop conflicts."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_pool, functools.partial(fn, *args, **kwargs))


def rtsp_url(stream: int = 1) -> str:
    """Build RTSP URL. stream1=HD, stream2=SD."""
    user = quote(TAPO_USER, safe="")
    pwd = quote(TAPO_PASSWORD, safe="")
    return f"rtsp://{user}:{pwd}@{TAPO_HOST}:554/stream{stream}"


def get_tapo() -> Tapo:
    global tapo
    if tapo is None:
        if not TAPO_HOST or not TAPO_PASSWORD:
            raise HTTPException(
                status_code=503,
                detail="Camera not configured. Copy .env.example to .env and fill in credentials.",
            )
        tapo = Tapo(
            TAPO_HOST,
            TAPO_USER,
            TAPO_PASSWORD,
            cloudPassword=TAPO_CLOUD_PASSWORD,
            childID=TAPO_CHILD_ID,
        )
    return tapo


def reconnect_tapo() -> Tapo:
    global tapo
    tapo = None
    return get_tapo()


def _kill_ffmpeg():
    global ffmpeg_process
    if ffmpeg_process and ffmpeg_process.poll() is None:
        try:
            os.killpg(os.getpgid(ffmpeg_process.pid), signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        ffmpeg_process = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    _kill_ffmpeg()
    _kill_stream_worker()


app = FastAPI(title="Tapo D225 Viewer", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.mount("/recordings", StaticFiles(directory=RECORDINGS_DIR), name="recordings")
app.mount("/stream", StaticFiles(directory=STREAM_DIR), name="stream_files")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


# ── UI ──────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


# ── Camera Info ─────────────────────────────────────────────────────────────

@app.get("/api/info")
async def camera_info():
    try:
        t = await run_in_thread(get_tapo)
        info = await run_in_thread(t.getBasicInfo)
        return {"ok": True, "info": info}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Recordings ──────────────────────────────────────────────────────────────

@app.get("/api/recordings/dates")
async def recording_dates(start: str = "", end: str = ""):
    """List dates that have recordings (YYYYMMDD format)."""
    try:
        t = await run_in_thread(get_tapo)
        if not start:
            start = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
        if not end:
            end = datetime.now().strftime("%Y%m%d")
        result = await run_in_thread(t.getRecordingsList, start_date=start, end_date=end)
        return {"ok": True, "dates": result}
    except Exception as e:
        return {"ok": False, "error": str(e), "dates": []}


@app.get("/api/recordings/{date}")
async def recordings_for_date(date: str):
    """List recordings for a given date (YYYYMMDD)."""
    try:
        t = await run_in_thread(get_tapo)
        result = await run_in_thread(t.getRecordings, date)
        return {"ok": True, "recordings": result}
    except Exception as e:
        return {"ok": False, "error": str(e), "recordings": []}


@app.post("/api/recordings/download")
async def download_recording(request: Request):
    """Download a recording from the camera's SD card.

    Uses pytapo's proprietary downloader if cloud password is configured,
    otherwise falls back to RTSP playback capture via ffmpeg.

    Body: {"startTime": unix_ts, "endTime": unix_ts, "date": "YYYYMMDD"}
    """
    body = await request.json()
    start_time = int(body["startTime"])
    end_time = int(body["endTime"])
    date_str = body.get("date", datetime.fromtimestamp(start_time).strftime("%Y%m%d"))

    date_dir = RECORDINGS_DIR / date_str
    date_dir.mkdir(exist_ok=True)
    filename = f"{start_time}_{end_time}.mp4"
    filepath = date_dir / filename

    if filepath.exists():
        return {
            "ok": True,
            "file": f"/recordings/{date_str}/{filename}",
            "cached": True,
        }

    # Try pytapo proprietary download if cloud password is available
    if TAPO_CLOUD_PASSWORD:
        try:
            return await _download_via_pytapo(start_time, end_time, date_dir, date_str, filename, filepath)
        except Exception as e:
            # Clean up partial file and fall through to RTSP
            if filepath.exists():
                filepath.unlink()
            # If cloud password was provided but failed, report it
            return {"ok": False, "error": f"Pytapo download failed: {e}"}

    # Fall back to RTSP playback capture
    try:
        return await _download_via_rtsp(start_time, end_time, date_dir, date_str, filename, filepath)
    except Exception as e:
        if filepath.exists():
            filepath.unlink()
        return {"ok": False, "error": str(e)}


async def _download_via_pytapo(start_time, end_time, date_dir, date_str, filename, filepath):
    from pytapo.media_stream.downloader import Downloader

    t = await run_in_thread(get_tapo)
    time_correction = await run_in_thread(t.getTimeCorrection)

    downloader = Downloader(
        tapo=t,
        startTime=start_time,
        endTime=end_time,
        timeCorrection=time_correction,
        outputDirectory=str(date_dir) + "/",
        fileName=filename,
        window_size=200,
    )

    result = await downloader.downloadFile()
    return {
        "ok": True,
        "file": f"/recordings/{date_str}/{filename}",
        "cached": False,
        "method": "pytapo",
    }


async def _download_via_rtsp(start_time, end_time, date_dir, date_str, filename, filepath):
    """Download recording by capturing RTSP playback stream.

    Tapo cameras support RTSP playback via the starttime query parameter.
    Format: rtsp://user:pass@ip:554/stream1?starttime=YYYYMMDDTHHmmss
    """
    dt = datetime.fromtimestamp(start_time)
    start_str = dt.strftime("%Y%m%dT%H%M%S")
    duration = end_time - start_time

    playback_url = rtsp_url(stream=1) + f"?starttime={start_str}"
    tmp_path = filepath.with_suffix(".tmp.mp4")

    cmd = [
        "ffmpeg", "-y",
        "-rtsp_transport", "tcp",
        "-i", playback_url,
        "-t", str(duration),
        "-c", "copy",
        "-movflags", "+faststart",
        str(tmp_path),
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=duration + 60)

    if proc.returncode == 0 and tmp_path.exists() and tmp_path.stat().st_size > 0:
        tmp_path.rename(filepath)
        return {
            "ok": True,
            "file": f"/recordings/{date_str}/{filename}",
            "cached": False,
            "method": "rtsp",
        }
    else:
        if tmp_path.exists():
            tmp_path.unlink()
        err = stderr.decode(errors="replace")[-500:] if stderr else "Unknown error"
        raise RuntimeError(f"ffmpeg failed (exit {proc.returncode}): {err}")


@app.get("/api/recordings/local")
async def local_recordings():
    """List already-downloaded recordings."""
    files = []
    for date_dir in sorted(RECORDINGS_DIR.iterdir()):
        if not date_dir.is_dir():
            continue
        for f in sorted(date_dir.iterdir()):
            if f.suffix == ".mp4":
                files.append({
                    "date": date_dir.name,
                    "file": f.name,
                    "path": f"/recordings/{date_dir.name}/{f.name}",
                    "size_mb": round(f.stat().st_size / 1024 / 1024, 1),
                })
    return {"ok": True, "files": files}


# ── Live Stream (pytapo media session → ffmpeg → HLS) ──────────────────────

stream_worker_proc: subprocess.Popen | None = None


def _kill_stream_worker():
    global stream_worker_proc
    if stream_worker_proc and stream_worker_proc.poll() is None:
        try:
            os.killpg(os.getpgid(stream_worker_proc.pid), signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        stream_worker_proc = None
    # Also kill via the PID file / pkill as backup
    os.system("pkill -f 'stream_worker.py' 2>/dev/null")
    os.system("pkill -f 'ffmpeg.*seg_.*\\.ts' 2>/dev/null")


@app.post("/api/stream/start")
async def start_stream(request: Request):
    """Start HLS live stream via pytapo proprietary protocol (port 8800)."""
    global stream_worker_proc

    async with stream_lock:
        if stream_worker_proc and stream_worker_proc.poll() is None:
            return {"ok": True, "message": "Stream already running", "url": "/stream/output.m3u8"}

        # Clear old HLS segments
        for f in STREAM_DIR.iterdir():
            f.unlink()

        try:
            import sys
            venv_python = str(Path(__file__).parent / ".venv" / "bin" / "python")
            worker_script = str(Path(__file__).parent / "stream_worker.py")
            stream_worker_proc = subprocess.Popen(
                [venv_python, worker_script, "start"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid,
            )
            # Wait for HLS playlist to appear
            for _ in range(15):
                await asyncio.sleep(1)
                if (STREAM_DIR / "output.m3u8").exists():
                    return {"ok": True, "url": "/stream/output.m3u8"}
                if stream_worker_proc.poll() is not None:
                    return {"ok": False, "error": "Stream worker exited unexpectedly"}

            return {"ok": False, "error": "Timed out waiting for HLS segments"}
        except Exception as e:
            return {"ok": False, "error": str(e)}


@app.post("/api/stream/stop")
async def stop_stream():
    async with stream_lock:
        _kill_stream_worker()
        _kill_ffmpeg()
        return {"ok": True, "message": "Stream stopped"}


@app.get("/api/stream/status")
async def stream_status():
    running = stream_worker_proc is not None and stream_worker_proc.poll() is None
    return {"running": running}


# ── Camera Controls ─────────────────────────────────────────────────────────

@app.post("/api/camera/privacy")
async def toggle_privacy(request: Request):
    body = await request.json()
    try:
        t = await run_in_thread(get_tapo)
        await run_in_thread(t.setPrivacyMode, bool(body.get("enabled")))
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/camera/status")
async def camera_status():
    global TAPO_MAC
    try:
        t = await run_in_thread(get_tapo)
        info = await run_in_thread(t.getBasicInfo)
        device_info = info.get("device_info", {}).get("basic_info", {})
        if device_info.get("mac"):
            TAPO_MAC = device_info["mac"]
        has_cloud = bool(TAPO_CLOUD_PASSWORD)
        return {
            "ok": True,
            "device_alias": device_info.get("device_alias", "Unknown"),
            "device_model": device_info.get("device_model", "Unknown"),
            "sw_version": device_info.get("sw_version", "Unknown"),
            "hw_version": device_info.get("hw_version", "Unknown"),
            "mac": device_info.get("mac", "Unknown"),
            "has_cloud_password": has_cloud,
            "rtsp_url_hd": f"rtsp://{TAPO_USER}:***@{TAPO_HOST}:554/stream1",
            "rtsp_url_sd": f"rtsp://{TAPO_USER}:***@{TAPO_HOST}:554/stream2",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Wake-on-LAN ────────────────────────────────────────────────────────────

TAPO_MAC = None  # Populated on first successful connection


def send_wol(mac: str, broadcast: str = "255.255.255.255"):
    """Send a Wake-on-LAN magic packet."""
    import socket as sock
    mac_bytes = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    magic = b"\xff" * 6 + mac_bytes * 16
    s = sock.socket(sock.AF_INET, sock.SOCK_DGRAM)
    s.setsockopt(sock.SOL_SOCKET, sock.SO_BROADCAST, 1)
    s.sendto(magic, (broadcast, 9))
    # Also try the subnet broadcast
    s.sendto(magic, ("10.1.1.255", 9))
    s.close()


@app.post("/api/camera/wake")
async def wake_camera():
    """Send WoL magic packet to wake the camera, then wait for it to respond."""
    mac = TAPO_MAC or "78:20:51:1E:50:7A"
    send_wol(mac)

    # Poll for camera to come alive
    import socket as sock
    for i in range(20):
        await asyncio.sleep(1)
        try:
            s = sock.socket()
            s.settimeout(1)
            if s.connect_ex((TAPO_HOST, 443)) == 0:
                s.close()
                return {"ok": True, "message": f"Camera awake after {i+1}s", "seconds": i + 1}
            s.close()
        except Exception:
            pass
        # Re-send WoL every 3 seconds
        if i % 3 == 2:
            send_wol(mac)

    return {"ok": False, "error": "Camera did not wake after 20s. Try ringing the doorbell."}


@app.post("/api/camera/reconnect")
async def camera_reconnect():
    _kill_ffmpeg()
    try:
        t = await run_in_thread(reconnect_tapo)
        await run_in_thread(t.getBasicInfo)
        return {"ok": True, "message": "Reconnected"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    import uvicorn
    host = os.getenv("APP_HOST", "0.0.0.0")
    port = int(os.getenv("APP_PORT", "8000"))
    uvicorn.run("app:app", host=host, port=port, reload=True)
