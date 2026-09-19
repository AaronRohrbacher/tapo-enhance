"""Application version from the release image or the repository VERSION file."""

from __future__ import annotations

import os
from pathlib import Path


def _version() -> str:
    injected = os.getenv("APP_VERSION", "").strip()
    if injected:
        return injected.removeprefix("v")
    try:
        return (Path(__file__).parent.parent / "VERSION").read_text().strip()
    except OSError:
        return "0.0.0-dev"


__version__ = _version()
