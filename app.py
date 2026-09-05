"""Tapo Enhance server entrypoint."""

from __future__ import annotations

import os

from srv.web import build_app  # noqa: E402

app = build_app()


if __name__ == "__main__":
    import uvicorn

    class ClosingServer(uvicorn.Server):
        """End application-owned streams before Uvicorn drains connections."""

        def handle_exit(self, sig, frame):
            app.state.hub.close()
            super().handle_exit(sig, frame)
            # Uvicorn normally re-raises captured signals after completing a
            # graceful shutdown. In a container that turns a successful stop
            # into exit 143; this server has handled the signal completely.
            self._captured_signals.clear()

    host = os.getenv("APP_HOST", "0.0.0.0")
    port = int(os.getenv("APP_PORT", "8000"))
    config = uvicorn.Config(
        app, host=host, port=port, timeout_graceful_shutdown=1,
    )
    try:
        ClosingServer(config).run()
    except KeyboardInterrupt:
        # Uvicorn re-raises the captured SIGINT after its clean shutdown.
        # The server is already stopped; do not turn an expected Ctrl-C into
        # a traceback or non-zero process exit.
        pass
