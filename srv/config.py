"""Runtime configuration. Loaded once from environment; mutable in-process
when the discoverer finds a new IP for the camera."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .vault import decrypt, load_or_create_key, write


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
    key_root: Path | None = None
    dvr_enabled: bool = False
    dvr_retention_days: int | None = None
    dvr_keep_forever: bool = False
    dvr_interval_minutes: int = 1440
    dvr_daily_time: str = "00:10"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            host=os.getenv("TAPO_HOST", ""),
            user=os.getenv("TAPO_USER", "admin"),
            password="",
            cloud_password="",
            child_id=os.getenv("TAPO_CHILD_ID") or None,
            mac=(os.getenv("TAPO_MAC") or "").upper().replace("-", ":") or None,
            subnet=os.getenv("TAPO_SUBNET", "10.1.1.0/24"),
            app_host=os.getenv("APP_HOST", "0.0.0.0"),
            app_port=int(os.getenv("APP_PORT", "8000")),
            cache_root=Path(os.getenv("TAPO_CACHE_ROOT", Path(__file__).parent.parent / "cache")),
            key_root=Path(os.getenv("TAPO_KEY_ROOT", Path(__file__).parent.parent / ".keys")),
        )

    def vault_file(self) -> Path:
        return self.cache_root / "credentials.vault"

    def key_file(self) -> Path:
        return (self.key_root or self.cache_root / ".keys") / "credentials.key"

    def load_persisted_config(self) -> None:
        if not self.vault_exists():
            return
        payload = decrypt(self.vault_file().read_bytes(), load_or_create_key(self.key_file()))
        for name in (
            "host", "user", "password", "cloud_password", "subnet", "child_id",
            "dvr_enabled", "dvr_retention_days", "dvr_keep_forever",
            "dvr_interval_minutes", "dvr_daily_time",
        ):
            if name in payload:
                setattr(self, name, payload[name])

    def save_camera_config(
        self, *, host: str, user: str, password: str, subnet: str,
        cloud_password: str = "",
    ) -> None:
        payload = {
            "host": host, "user": user, "password": password,
            "cloud_password": cloud_password, "subnet": subnet,
            "dvr_enabled": self.dvr_enabled,
            "dvr_retention_days": self.dvr_retention_days,
            "dvr_keep_forever": self.dvr_keep_forever,
            "dvr_interval_minutes": self.dvr_interval_minutes,
            "dvr_daily_time": self.dvr_daily_time,
        }
        write(self.vault_file(), payload, load_or_create_key(self.key_file()))
        self.host, self.user, self.password = host, user, password
        self.cloud_password, self.subnet = cloud_password, subnet

    def save_dvr_config(
        self, *, enabled: bool, retention_days: int, keep_forever: bool,
        interval_minutes: int, daily_time: str,
    ) -> None:
        """Update the encrypted vault without exposing camera credentials."""
        payload = {
            "host": self.host, "user": self.user, "password": self.password,
            "cloud_password": self.cloud_password, "subnet": self.subnet,
            "dvr_enabled": enabled, "dvr_retention_days": retention_days,
            "dvr_keep_forever": keep_forever,
            "dvr_interval_minutes": interval_minutes,
            "dvr_daily_time": daily_time,
        }
        write(self.vault_file(), payload, load_or_create_key(self.key_file()))
        self.dvr_enabled = enabled
        self.dvr_retention_days = retention_days
        self.dvr_keep_forever = keep_forever
        self.dvr_interval_minutes = interval_minutes
        self.dvr_daily_time = daily_time

    def vault_exists(self) -> bool:
        return self.vault_file().is_file()

    def configured(self) -> bool:
        return bool(self.host and self.password)

    def mac_file(self) -> Path:
        return self.cache_root / "camera.mac"

    def load_persisted_mac(self) -> None:
        """Restore the last MAC the camera reported, unless TAPO_MAC was set
        explicitly (which wins). This is the piece that makes Wake-on-LAN work
        when the doorbell is *deep* asleep: the kernel ARP/neigh entry has long
        since aged out, so there's nothing to learn from — but the camera's MAC
        never changes, so a value captured while it was awake is still valid to
        target with a magic packet."""
        if self.mac:
            return
        try:
            val = self.mac_file().read_text().strip()
        except OSError:
            return
        if val:
            self.mac = val

    def remember_mac(self, mac: str | None) -> None:
        """Record the camera's MAC (from getBasicInfo or ARP) in memory and on
        disk so it survives restarts and deep sleep. No-op if empty/unchanged.
        The user never types this — it's learned automatically from the camera."""
        norm = (mac or "").strip().lower().replace("-", ":")
        if not norm or norm == "00:00:00:00:00:00":
            return
        if norm == (self.mac or "").lower():
            return
        self.mac = norm
        try:
            self.mac_file().parent.mkdir(parents=True, exist_ok=True)
            self.mac_file().write_text(norm + "\n")
        except OSError:
            pass

    def host_file(self) -> Path:
        return self.cache_root / "camera.host"

    def load_persisted_host(self) -> None:
        """The doorbell's IP is handed out by DHCP and wanders between leases.
        TAPO_HOST is only a first-run hint; once we've discovered where the
        camera actually lives we persist it and prefer that on the next start,
        so a restart doesn't strand us on a stale address. If the camera has
        moved again since, the nmap rediscovery in recover() still finds it —
        this just spares the user a manual reconnect in the common case."""
        try:
            val = self.host_file().read_text().strip()
        except OSError:
            return
        if val:
            self.host = val

    def remember_host(self, ip: str | None) -> None:
        """Persist the camera's current IP. Called whenever discovery moves us
        to a new address, so the last-known-good survives restarts."""
        norm = (ip or "").strip()
        if not norm:
            return
        self.host = norm
        try:
            self.host_file().parent.mkdir(parents=True, exist_ok=True)
            self.host_file().write_text(norm + "\n")
        except OSError:
            pass
