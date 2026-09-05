"""Authenticated Tapo client lifecycle.

Wraps `pytapo.Tapo`. Caches the constructed client; rebuilds it on
auth failure (which forces a fresh KLAP handshake). Exposes a single
`get()` method the gateway uses to lend a client to a runner.

This is also the single place the in-process IP can be updated when
discovery finds a new address, so a future call rebuilds against the
new endpoint."""

from __future__ import annotations

import logging
import time
from threading import RLock
from typing import Any

from .config import Settings
from .discovery import (
    find_arp_by_mac,
    looks_like_conn_error,
    read_arp_table,
    send_wol,
)
from .errors import ApiError, Code

log = logging.getLogger(__name__)


class CameraConnection:
    def __init__(self, settings: Settings, *, tapo_factory=None, rediscover=None):
        self.settings = settings
        self._client: Any | None = None
        self._lock = RLock()
        self._factory = tapo_factory or self._real_factory
        # Optional callable() -> str|None returning the camera's current IP.
        # Used to self-heal when the doorbell's DHCP lease moved and the
        # cached/configured host now points at a dead address.
        self._rediscover = rediscover

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
                    "Camera credential vault is not unlocked.",
                    status=503,
                )
            if self._client is not None:
                return self._client
            try:
                self._client = self._factory(self.settings)
                return self._client
            except Exception as exc:
                # Unreachable almost always means the battery doorbell is
                # asleep (it stops answering on the LAN entirely). Wake it the
                # same way the Tapo mobile app does — a Wake-on-LAN magic
                # packet to its MAC — then reconnect.
                if not looks_like_conn_error(exc):
                    raise
                self._client = self._recover(exc)
                return self._client

    def _recover(self, last_exc: BaseException) -> Any:
        """Bring a sleeping/moved camera back: WoL its MAC and retry the
        handshake; if there's no MAC, fall back to IP rediscovery."""
        mac = self.settings.mac
        if mac:
            try:
                return self._wake_and_connect(mac, last_exc)
            except Exception as e:
                last_exc = e
        if self._rediscover is not None:
            new_ip = self._rediscover()
            if new_ip and new_ip != self.settings.host:
                log.info("camera found at %s; reconnecting", new_ip)
                self.settings.host = new_ip
                self.settings.remember_host(new_ip)
                return self._factory(self.settings)
        raise last_exc

    def _wake_and_connect(self, mac: str, last_exc: BaseException) -> Any:
        """Send WoL to `mac` and poll the handshake until the camera answers.
        The doorbell may rejoin DHCP at a new address, so each attempt also
        re-resolves the IP from the (stable) MAC via ARP before retrying."""
        log.info("camera unreachable at %s; WoL -> %s", self.settings.host, mac)
        send_wol(mac, subnet=self.settings.subnet)
        for attempt in range(1, 16):  # ~30s of 2s polls
            time.sleep(2.0)
            if attempt % 5 == 0:
                send_wol(mac, subnet=self.settings.subnet)  # nudge again
            ip = find_arp_by_mac(read_arp_table(), mac)
            if ip and ip != self.settings.host:
                log.info("camera woke at new address %s", ip)
                self.settings.host = ip
                self.settings.remember_host(ip)
            try:
                client = self._factory(self.settings)
                log.info("camera connected at %s after WoL", self.settings.host)
                return client
            except Exception as e:
                if not looks_like_conn_error(e):
                    raise
                last_exc = e
        raise last_exc

    def with_retry(self, op):
        """Run `op(client)` against the camera. If it fails with a connection
        error, the cached client is stale — the doorbell almost certainly went
        back to sleep. Drop the client and retry once: the rebuild runs the
        WoL-wake path in get(), so a single request transparently wakes a
        sleeping camera instead of returning 'offline'."""
        try:
            return op(self.get())
        except Exception as e:
            if not looks_like_conn_error(e):
                raise
            self.invalidate()
            return op(self.get())

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
        # Persist the new address so a restart doesn't fall back to the stale
        # configured host (the camera's DHCP lease wanders between sleeps).
        self.settings.remember_host(ip)
        return True
