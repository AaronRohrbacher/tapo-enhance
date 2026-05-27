"""Entrypoint. Loads .env, builds the FastAPI app, and runs uvicorn."""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

from srv.web import build_app  # noqa: E402

app = build_app()


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("APP_HOST", "0.0.0.0")
    port = int(os.getenv("APP_PORT", "8000"))
    uvicorn.run("app:app", host=host, port=port, reload=False)
