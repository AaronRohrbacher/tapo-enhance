"""Stand up a real uvicorn server with FakeTapo wired in. Used by the
playwright suite so the browser hits a server, not a TestClient."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def _build():
    from srv.config import Settings
    from srv.web import build_app
    from tests.conftest import FakeTapo

    cache = Path(tempfile.mkdtemp(prefix="tapo-fake-"))
    fake = FakeTapo()
    settings = Settings(
        host="127.0.0.1",
        user="u",
        password="p",
        cache_root=cache,
    )
    app = build_app(settings=settings, tapo_factory=lambda _s: fake)
    app.state.fake = fake
    app.state.cache_root = cache
    return app


def run(host: str = "127.0.0.1", port: int = 8765):
    import uvicorn

    app = _build()
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    server.run()


def start_in_thread(port: int = 8765) -> threading.Thread:
    """Start a daemon thread; returns the thread. Caller waits for /api/recordings/local."""
    t = threading.Thread(target=run, kwargs={"port": port}, daemon=True, name="fakeserver")
    t.start()
    return t


if __name__ == "__main__":
    run()
