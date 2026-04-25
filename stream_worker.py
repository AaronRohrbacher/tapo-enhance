"""
Custom stream worker for Tapo D225.
Connects via pytapo media session, pipes MPEG-TS to ffmpeg → HLS.
Runs in its own process to avoid event loop conflicts.

Usage: .venv/bin/python stream_worker.py [start|stop]
"""

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

STREAM_DIR = Path(__file__).parent / "stream"
PID_FILE = STREAM_DIR / ".worker.pid"


async def stream(tapo_instance):
    from pytapo.media_stream._utils import StreamType

    t = tapo_instance
    session = t.getMediaSession(StreamType.Stream)
    await session.start()
    print("Media session started.", flush=True)

    # Start ffmpeg: reads MPEG-TS from stdin, outputs HLS
    STREAM_DIR.mkdir(exist_ok=True)
    for f in STREAM_DIR.iterdir():
        if f.suffix in (".ts", ".m3u8"):
            f.unlink()

    ffmpeg = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y",
        "-fflags", "+genpts+discardcorrupt",
        "-f", "mpegts",
        "-i", "pipe:0",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "64k",
        "-f", "hls",
        "-hls_time", "2",
        "-hls_list_size", "6",
        "-hls_flags", "delete_segments+append_list",
        "-hls_segment_filename", str(STREAM_DIR / "seg_%04d.ts"),
        str(STREAM_DIR / "output.m3u8"),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    print(f"ffmpeg started (pid {ffmpeg.pid}).", flush=True)

    # Send preview request
    req = json.dumps({
        "type": "request",
        "seq": 1,
        "params": {
            "preview": {
                "audio": ["default"],
                "channels": [0],
                "resolutions": ["HD"],
            },
            "method": "get",
        },
    })

    chunks = 0
    try:
        async for response in session.transceive(req, "application/json"):
            if response.mimetype == "video/mp2t" and response.plaintext:
                ffmpeg.stdin.write(response.plaintext)
                await ffmpeg.stdin.drain()
                chunks += 1
                if chunks % 50 == 0:
                    print(f"  {chunks} chunks written", flush=True)
            elif response.mimetype == "application/json":
                try:
                    j = json.loads(response.plaintext)
                    if j.get("type") == "notification":
                        print(f"  Camera notification: {j}", flush=True)
                except Exception:
                    pass

            if ffmpeg.returncode is not None:
                print(f"ffmpeg exited with code {ffmpeg.returncode}", flush=True)
                stderr = await ffmpeg.stderr.read()
                print(f"ffmpeg stderr: {stderr.decode(errors='replace')[-500:]}", flush=True)
                break
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"Stream error: {e}", flush=True)
    finally:
        print(f"Stopping after {chunks} chunks.", flush=True)
        try:
            ffmpeg.stdin.close()
            await ffmpeg.wait()
        except Exception:
            pass
        try:
            await session.close()
        except Exception:
            pass


def start():
    STREAM_DIR.mkdir(exist_ok=True)

    # Check if already running
    if PID_FILE.exists():
        pid = int(PID_FILE.read_text().strip())
        try:
            os.kill(pid, 0)
            print(f"Worker already running (pid {pid})")
            return
        except ProcessLookupError:
            PID_FILE.unlink()

    PID_FILE.write_text(str(os.getpid()))

    def cleanup(sig, frame):
        print("\nShutting down...", flush=True)
        PID_FILE.unlink(missing_ok=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    # Create Tapo instance OUTSIDE asyncio.run() to avoid event loop conflict
    import warnings
    warnings.filterwarnings("ignore")
    from pytapo import Tapo
    from dotenv import load_dotenv
    load_dotenv()

    host = os.getenv("TAPO_HOST")
    user = os.getenv("TAPO_USER", "admin")
    password = os.getenv("TAPO_PASSWORD")
    cloud_pw = os.getenv("TAPO_CLOUD_PASSWORD", "")

    print(f"Connecting to {host}...", flush=True)
    t = Tapo(host, user, password, cloudPassword=cloud_pw)
    print(f"Connected: {t.basicInfo.get('device_info', {}).get('basic_info', {}).get('device_alias', '?')}", flush=True)

    try:
        asyncio.run(stream(t))
    finally:
        PID_FILE.unlink(missing_ok=True)


def stop():
    if PID_FILE.exists():
        pid = int(PID_FILE.read_text().strip())
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"Sent SIGTERM to worker (pid {pid})")
        except ProcessLookupError:
            print("Worker not running")
        PID_FILE.unlink(missing_ok=True)
    else:
        print("No worker running")
    # Kill any stale ffmpeg
    os.system("pkill -f 'ffmpeg.*seg_.*\\.ts' 2>/dev/null")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "start"
    if cmd == "start":
        start()
    elif cmd == "stop":
        stop()
