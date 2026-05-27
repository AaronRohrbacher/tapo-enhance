"""LAN discovery + Wake-on-LAN for the camera. Used for two things:
  - first-time discovery (user doesn't know the IP)
  - auto-recovery (camera went to sleep / changed IP).

All subprocess and socket calls are wrapped in module-level functions so
tests can monkeypatch them with deterministic fakes."""

from __future__ import annotations

import os
import socket
import subprocess
from dataclasses import dataclass

# Tapo OUI prefixes used as a fallback when the exact MAC isn't known yet.
TAPO_OUIS = ("78:20:51", "5c:62:8b", "30:de:4b", "a4:2b:b0", "9c:53:22", "1c:61:b4")


@dataclass(frozen=True, slots=True)
class ArpEntry:
    ip: str
    mac: str  # lowercase, colon-delimited


def normalize_mac(mac: str | None) -> str | None:
    if not mac:
        return None
    return mac.lower().replace("-", ":").strip()


def read_arp_table(path: str = "/proc/net/arp") -> list[ArpEntry]:
    try:
        with open(path) as f:
            next(f, None)  # header
            out = []
            for line in f:
                parts = line.split()
                if len(parts) >= 4 and parts[3] != "00:00:00:00:00:00":
                    out.append(ArpEntry(parts[0], parts[3].lower()))
            return out
    except OSError:
        return []


def find_arp_by_mac(rows: list[ArpEntry], mac: str) -> str | None:
    needle = normalize_mac(mac)
    if not needle:
        return None
    for r in rows:
        if r.mac == needle:
            return r.ip
    return None


def find_arp_by_ip(rows: list[ArpEntry], ip: str) -> str | None:
    if not ip:
        return None
    for r in rows:
        if r.ip == ip:
            return r.mac
    return None


def find_arp_by_oui(rows: list[ArpEntry]) -> list[ArpEntry]:
    return [r for r in rows if any(r.mac.startswith(o.lower()) for o in TAPO_OUIS)]


def parse_nmap_grepable(output: str, want_port: int = 8800) -> list[str]:
    """Pull IPs from nmap -oG output that have `want_port` open.
    Tapo cameras expose port 8800 (proprietary media protocol); plugs/bulbs
    don't. KLAP on 443 is sometimes filtered when the camera is sleepy,
    so we don't gate on it."""
    out: list[str] = []
    for line in output.splitlines():
        if not line.startswith("Host:") or "Ports:" not in line:
            continue
        if f"{want_port}/open" not in line:
            continue
        try:
            ip = line.split()[1]
            out.append(ip)
        except IndexError:
            continue
    return out


def run_nmap_scan(subnet: str, *, timeout: int = 60) -> list[str]:
    """Scan the subnet for the Tapo camera fingerprint. Returns [] if nmap
    isn't installed or nothing matches."""
    try:
        proc = subprocess.run(
            [
                "nmap", "-p", "8800", "-n", "--open",
                "--max-retries", "1", "--host-timeout", "8s",
                "-oG", "-", subnet,
            ],
            capture_output=True, text=True, timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    return parse_nmap_grepable(proc.stdout or "")


def tcp_probe(ip: str, port: int = 443, *, timeout: float = 1.5) -> bool:
    try:
        s = socket.socket()
        s.settimeout(timeout)
        ok = s.connect_ex((ip, port)) == 0
        s.close()
        return ok
    except OSError:
        return False


def refresh_arp(ip: str, *, ports: tuple[int, ...] = (8800, 443, 80), timeout: float = 1.0) -> None:
    """Poke `ip` so the kernel populates/refreshes its ARP entry. The
    target doesn't have to accept the connection — even an aborted SYN
    triggers an ARP resolution. Used before reading /proc/net/arp when
    we want the MAC for a host that might not be in cache yet."""
    for port in ports:
        try:
            s = socket.socket()
            s.settimeout(timeout)
            s.connect_ex((ip, port))
            s.close()
        except OSError:
            pass


def discover_ip(
    subnet: str,
    mac: str | None,
    *,
    arp_rows: list[ArpEntry] | None = None,
    nmap_hits: list[str] | None = None,
    probe=tcp_probe,
) -> str | None:
    """Pure orchestration: prefer ARP-by-MAC if reachable, fall back to
    the nmap fingerprint scan. Tests inject `arp_rows` / `nmap_hits`."""
    rows = arp_rows if arp_rows is not None else read_arp_table()
    if mac:
        hit = find_arp_by_mac(rows, mac)
        if hit and probe(hit):
            return hit
    hits = nmap_hits if nmap_hits is not None else run_nmap_scan(subnet)
    if not hits:
        return None
    if mac:
        needle = normalize_mac(mac)
        arp_map = {r.ip: r.mac for r in rows}
        for ip in hits:
            if arp_map.get(ip) == needle:
                return ip
    return hits[0]


def broadcast_for_subnet(subnet: str) -> str:
    """Crude /24 broadcast: 10.1.1.0/24 -> 10.1.1.255."""
    try:
        net = subnet.split("/")[0]
        parts = net.split(".")
        if len(parts) == 4 and all(p.isdigit() for p in parts):
            parts[3] = "255"
            return ".".join(parts)
    except Exception:
        pass
    return "255.255.255.255"


def build_wol_packet(mac: str) -> bytes:
    raw = bytes.fromhex(normalize_mac(mac).replace(":", ""))
    if len(raw) != 6:
        raise ValueError(f"bad mac: {mac!r}")
    return b"\xff" * 6 + raw * 16


def send_wol(mac: str, *, subnet: str = "10.1.1.0/24") -> None:
    pkt = build_wol_packet(mac)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        s.sendto(pkt, ("255.255.255.255", 9))
        s.sendto(pkt, (broadcast_for_subnet(subnet), 9))
    finally:
        s.close()


_CONN_MARKERS = (
    "max retries", "no route to host", "connection refused",
    "timed out", "connection reset", "network is unreachable",
    "name or service not known", "httpsconnectionpool",
)
_AUTH_MARKERS = (
    "invalid authentication", "authentication failed", "401",
    "-40401", "unauthorized",
)


def looks_like_conn_error(exc: BaseException | str) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in _CONN_MARKERS)


def looks_like_auth_error(exc: BaseException | str) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in _AUTH_MARKERS)
