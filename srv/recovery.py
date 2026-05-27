"""Reconnect flow. Runs in three phases:

  1. nmap port-fingerprint scan on the configured subnet.
  2. If not found and we have a MAC (auto-learned from ARP for the
     configured host), send a WoL magic packet, wait, rescan.
  3. Update the in-process IP and invalidate the cached pytapo client
     so the next call gets a fresh KLAP handshake.

Returns a sequence of progress events the SSE endpoint streams to the UI.

The MAC is never something the user has to provide — it's auto-discovered
from the system ARP table for the configured TAPO_HOST, refreshed with a
TCP poke if the cache is stale.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

from . import discovery
from .camera import CameraConnection


async def _to_thread(fn, *args, **kwargs):
    return await asyncio.get_event_loop().run_in_executor(None, lambda: fn(*args, **kwargs))


def _learn_mac_for_host(host: str) -> str | None:
    """Best-effort ARP lookup for `host`. Pokes the host first to refresh
    the kernel ARP cache, then reads /proc/net/arp."""
    if not host:
        return None
    discovery.refresh_arp(host)
    rows = discovery.read_arp_table()
    return discovery.find_arp_by_ip(rows, host)


async def recover(camera: CameraConnection) -> AsyncIterator[dict]:
    cfg = camera.settings
    yield {"phase": "scanning", "message": "scanning LAN with nmap (port 8800)"}

    ip = await _to_thread(discovery.discover_ip, cfg.subnet, cfg.mac)
    if ip:
        if not camera.update_host(ip):
            camera.invalidate()
        mac = await _to_thread(_learn_mac_for_host, ip)
        if mac:
            cfg.mac = mac
        yield {"phase": "found", "ip": ip, "mac": cfg.mac, "message": f"camera at {ip}"}
        yield {"phase": "done", "ok": True, "ip": ip, "mac": cfg.mac, "message": "uplink restored"}
        return

    yield {"phase": "no_response", "message": "camera silent on LAN — trying WoL"}

    # Auto-learn MAC from ARP if we don't have it. The configured host's
    # MAC usually lingers in /proc/net/arp from prior traffic; refresh_arp
    # nudges the kernel to re-resolve if stale.
    if not cfg.mac:
        learned = await _to_thread(_learn_mac_for_host, cfg.host)
        if learned:
            cfg.mac = learned
            yield {"phase": "mac_learned", "mac": cfg.mac,
                   "message": f"learned MAC {cfg.mac} from ARP for {cfg.host}"}

    if not cfg.mac:
        yield {
            "phase": "done",
            "ok": False,
            "message": f"camera offline at {cfg.host} and MAC not in ARP cache",
        }
        return

    yield {"phase": "wol", "mac": cfg.mac, "message": f"sending WoL magic packet to {cfg.mac}"}
    try:
        await _to_thread(discovery.send_wol, cfg.mac, subnet=cfg.subnet)
    except Exception as e:
        yield {"phase": "done", "ok": False, "message": f"WoL failed: {e}"}
        return

    elapsed = 0
    for target in (3, 6, 10, 15, 20):
        await asyncio.sleep(target - elapsed)
        elapsed = target
        yield {"phase": "wol_wait", "seconds": elapsed, "message": f"waiting for camera ({elapsed}s)"}

    yield {"phase": "rescanning", "message": "re-scanning LAN"}
    ip = await _to_thread(discovery.discover_ip, cfg.subnet, cfg.mac)
    if ip:
        if not camera.update_host(ip):
            camera.invalidate()
        yield {"phase": "done", "ok": True, "ip": ip, "mac": cfg.mac,
               "message": f"uplink restored at {ip}"}
    else:
        yield {
            "phase": "done", "ok": False,
            "message": "camera didn't answer after WoL — try waking the device manually",
        }
