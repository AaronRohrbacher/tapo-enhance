"""Authenticated Tapo client lifecycle.

Wraps `pytapo.Tapo`. Caches the constructed client; rebuilds it on
auth failure (which forces a fresh KLAP handshake). Exposes a single
`get()` method the gateway uses to lend a client to a runner.

This is also the single place the in-process IP can be updated when
discovery finds a new address, so a future call rebuilds against the
new endpoint."""

from __future__ import annotations

import logging
from threading import RLock
from typing import Any

from .config import Settings
from .errors import ApiError, Code

log = logging.getLogger(__name__)


class CameraConnection:
    def __init__(self, settings: Settings, *, tapo_factory=None):
        self.settings = settings
        self._client: Any | None = None
        self._lock = RLock()
        self._factory = tapo_factory or self._real_factory

    @staticmethod
    def _real_factory(settings: Settings):
        from pytapo import Tapo
        return Tapo(
            settings.host,
            settings.user,
            settings.password,
            cloudPassword=settings.cloud_password,
            childID=settings.child_id,
        )

    def get(self) -> Any:
        with self._lock:
            if not self.settings.configured():
                raise ApiError(
                    Code.CAMERA_NOT_CONFIGURED,
                    "Camera not configured. Set TAPO_HOST and TAPO_PASSWORD in .env.",
                    status=503,
                )
            if self._client is None:
                self._client = self._factory(self.settings)
            return self._client

    def invalidate(self) -> None:
        """Drop the cached client so the next get() rebuilds it. Use after
        an auth-state failure (forces fresh handshake)."""
        with self._lock:
            self._client = None

    def update_host(self, ip: str) -> bool:
        if ip == self.settings.host:
            return False
        with self._lock:
            self.settings.host = ip
            self._client = None
        return True
