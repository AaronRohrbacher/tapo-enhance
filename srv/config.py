"""Runtime configuration. Loaded once from environment; mutable in-process
when the discoverer finds a new IP for the camera."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    host: str = ""
    user: str = "admin"
    password: str = ""
    cloud_password: str = ""
    child_id: str | None = None
    mac: str | None = None
    subnet: str = "10.1.1.0/24"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    cache_root: Path = field(default_factory=lambda: Path(__file__).parent.parent / "cache")

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            host=os.getenv("TAPO_HOST", ""),
            user=os.getenv("TAPO_USER", "admin"),
            password=os.getenv("TAPO_PASSWORD", ""),
            cloud_password=os.getenv("TAPO_CLOUD_PASSWORD", ""),
            child_id=os.getenv("TAPO_CHILD_ID") or None,
            mac=(os.getenv("TAPO_MAC") or "").upper().replace("-", ":") or None,
            subnet=os.getenv("TAPO_SUBNET", "10.1.1.0/24"),
            app_host=os.getenv("APP_HOST", "0.0.0.0"),
            app_port=int(os.getenv("APP_PORT", "8000")),
        )

    def configured(self) -> bool:
        return bool(self.host and self.password)
