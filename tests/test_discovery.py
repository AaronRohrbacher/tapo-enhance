"""Discovery, ARP scanning, WoL packet building. Subprocess-free —
each helper is testable as a pure function over fake input."""

from __future__ import annotations

import pytest

from srv.discovery import (
    ArpEntry,
    broadcast_for_subnet,
    build_wol_packet,
    discover_ip,
    find_arp_by_mac,
    find_arp_by_oui,
    looks_like_auth_error,
    looks_like_conn_error,
    normalize_mac,
    parse_nmap_grepable,
    validate_subnet,
    discover_candidates,
)


# ── normalize_mac ──────────────────────────────────────────────────────────


def test_normalize_mac_lowercases_and_replaces_dashes():
    assert normalize_mac("AA-BB-CC-DD-EE-FF") == "aa:bb:cc:dd:ee:ff"


def test_normalize_mac_passes_through_correct_form():
    assert normalize_mac("aa:bb:cc:dd:ee:ff") == "aa:bb:cc:dd:ee:ff"


def test_normalize_mac_handles_none():
    assert normalize_mac(None) is None
    assert normalize_mac("") is None


@pytest.mark.parametrize("message", [
    "Remote end closed connection without response",
    "Connection aborted by peer",
    "NewConnectionError: failed to establish a new connection",
])
def test_sleeping_camera_disconnects_are_connection_errors(message):
    assert looks_like_conn_error(message)


# ── arp helpers ────────────────────────────────────────────────────────────


def _rows(*pairs):
    return [ArpEntry(ip, mac.lower()) for ip, mac in pairs]


def test_find_arp_by_mac_exact_match():
    rows = _rows(("10.1.1.5", "AA:BB:CC:DD:EE:FF"), ("10.1.1.6", "11:22:33:44:55:66"))
    assert find_arp_by_mac(rows, "AA:BB:CC:DD:EE:FF") == "10.1.1.5"


def test_find_arp_by_mac_returns_none_when_absent():
    rows = _rows(("10.1.1.5", "11:22:33:44:55:66"))
    assert find_arp_by_mac(rows, "AA:BB:CC:DD:EE:FF") is None


def test_find_arp_by_oui_filters_to_tapo_prefixes():
    rows = _rows(
        ("10.1.1.5", "78:20:51:00:00:01"),  # Tapo OUI
        ("10.1.1.6", "11:22:33:44:55:66"),  # not Tapo
        ("10.1.1.7", "5C:62:8B:11:22:33"),  # Tapo OUI
    )
    matches = find_arp_by_oui(rows)
    assert {r.ip for r in matches} == {"10.1.1.5", "10.1.1.7"}


# ── nmap parser ────────────────────────────────────────────────────────────


def test_parse_nmap_grepable_picks_hosts_with_camera_port():
    out = (
        "# Nmap scan\n"
        "Host: 10.1.1.5 ()\tPorts: 443/open/tcp//https///, 8800/open/tcp//unknown///\tIgnored\n"
        "Host: 10.1.1.6 ()\tPorts: 443/open/tcp//https///\n"
        "Host: 10.1.1.7 ()\tPorts: 8800/open/tcp//unknown///\n"
        "Host: 10.1.1.8 ()\tPorts: 443/open/tcp//https///, 8800/open/tcp//unknown///\n"
    )
    # 443-only hosts (.6) are excluded; the camera signal is 8800.
    assert parse_nmap_grepable(out) == ["10.1.1.5", "10.1.1.7", "10.1.1.8"]


def test_parse_nmap_grepable_empty_when_no_match():
    out = "Host: 10.1.1.5 ()\tPorts: 22/open/tcp//ssh///\n"
    assert parse_nmap_grepable(out) == []


def test_validate_subnet_normalizes_host_address_and_rejects_unsafe_range():
    assert validate_subnet("192.168.50.22/24") == "192.168.50.0/24"
    with pytest.raises(ValueError):
        validate_subnet("--script=bad")
    with pytest.raises(ValueError):
        validate_subnet("10.0.0.0/8")


def test_discover_candidates_lists_only_camera_port_hits(monkeypatch):
    monkeypatch.setattr("srv.discovery.run_nmap_scan", lambda subnet: ["192.168.1.20"])
    monkeypatch.setattr("srv.discovery.read_arp_table", lambda: _rows(
        ("192.168.1.20", "78:20:51:00:00:01"),
        ("192.168.1.21", "5c:62:8b:00:00:02"),
        ("10.0.0.8", "78:20:51:00:00:03"),
    ))
    assert discover_candidates("192.168.1.0/24") == [
        {"ip": "192.168.1.20", "mac": "78:20:51:00:00:01", "source": "camera port 8800"},
    ]


def test_discover_candidates_does_not_list_arp_only_tapo_device(monkeypatch):
    monkeypatch.setattr("srv.discovery.run_nmap_scan", lambda subnet: [])
    monkeypatch.setattr("srv.discovery.read_arp_table", lambda: _rows(
        ("192.168.1.21", "5c:62:8b:00:00:02"),  # Tapo chime / non-camera
    ))
    assert discover_candidates("192.168.1.0/24") == []


# ── discover_ip orchestration ──────────────────────────────────────────────


def test_discover_ip_prefers_arp_match_when_reachable():
    rows = _rows(("10.1.1.5", "AA:BB:CC:DD:EE:FF"))
    ip = discover_ip(
        "10.1.1.0/24",
        "AA:BB:CC:DD:EE:FF",
        arp_rows=rows,
        nmap_hits=[],
        probe=lambda _ip, port=443, timeout=1.0: True,
    )
    assert ip == "10.1.1.5"


def test_discover_ip_falls_back_to_nmap_when_arp_unreachable():
    rows = _rows(("10.1.1.5", "AA:BB:CC:DD:EE:FF"))
    ip = discover_ip(
        "10.1.1.0/24",
        "AA:BB:CC:DD:EE:FF",
        arp_rows=rows,
        nmap_hits=["10.1.1.99"],
        probe=lambda _ip, port=443, timeout=1.0: False,
    )
    assert ip == "10.1.1.99"


def test_discover_ip_with_mac_picks_matching_nmap_hit():
    rows = _rows(
        ("10.1.1.5", "11:22:33:44:55:66"),
        ("10.1.1.99", "AA:BB:CC:DD:EE:FF"),
    )
    ip = discover_ip(
        "10.1.1.0/24",
        "AA:BB:CC:DD:EE:FF",
        arp_rows=rows,
        nmap_hits=["10.1.1.5", "10.1.1.99"],  # both fingerprint-match
        probe=lambda *a, **kw: False,
    )
    assert ip == "10.1.1.99"


def test_discover_ip_returns_none_when_nothing_matches():
    ip = discover_ip(
        "10.1.1.0/24",
        None,
        arp_rows=[],
        nmap_hits=[],
        probe=lambda *a, **kw: False,
    )
    assert ip is None


# ── WoL ────────────────────────────────────────────────────────────────────


def test_build_wol_packet_has_correct_structure():
    pkt = build_wol_packet("AA:BB:CC:DD:EE:FF")
    assert pkt[:6] == b"\xff" * 6
    mac_bytes = bytes.fromhex("aabbccddeeff")
    assert pkt[6:] == mac_bytes * 16
    assert len(pkt) == 6 + 6 * 16


def test_build_wol_packet_handles_dash_separated():
    pkt = build_wol_packet("AA-BB-CC-DD-EE-FF")
    assert len(pkt) == 102


def test_broadcast_for_24():
    assert broadcast_for_subnet("10.1.1.0/24") == "10.1.1.255"


def test_broadcast_falls_back_on_garbage():
    assert broadcast_for_subnet("garbage") == "255.255.255.255"


# ── error classification ──────────────────────────────────────────────────


def test_conn_error_recognises_common_python_messages():
    assert looks_like_conn_error("Max retries exceeded with url:")
    assert looks_like_conn_error("No route to host")
    assert looks_like_conn_error(ConnectionResetError("Connection reset by peer"))
    assert not looks_like_conn_error("Invalid authentication")


def test_auth_error_recognises_tapo_codes():
    assert looks_like_auth_error("Invalid authentication: -40401")
    assert looks_like_auth_error("HTTP 401 Unauthorized")
    assert not looks_like_auth_error("connection refused")


# ── MAC persistence (deep-sleep WoL survival) ──────────────────────────────
# When the camera is deep-asleep its ARP/neigh entry is gone, so there is
# nothing to auto-learn from — Wake-on-LAN only works if we kept the MAC the
# camera reported while it was last awake. These lock that round-trip in.


def test_remember_mac_persists_and_normalises(tmp_path):
    from srv.config import Settings

    s = Settings(host="10.1.1.143", password="x", cache_root=tmp_path)
    s.remember_mac("78:20:51:1E:50:7A")  # camera reports uppercase
    assert s.mac == "78:20:51:1e:50:7a"
    assert s.mac_file().read_text().strip() == "78:20:51:1e:50:7a"


def test_persisted_mac_survives_restart(tmp_path):
    from srv.config import Settings

    Settings(host="10.1.1.143", password="x", cache_root=tmp_path).remember_mac(
        "78:20:51:1e:50:7a"
    )
    # Fresh process, no TAPO_MAC in the environment.
    fresh = Settings(host="10.1.1.143", password="x", cache_root=tmp_path)
    assert fresh.mac is None
    fresh.load_persisted_mac()
    assert fresh.mac == "78:20:51:1e:50:7a"
    # And it's still a valid WoL target.
    assert len(build_wol_packet(fresh.mac)) == 102


def test_explicit_env_mac_wins_over_persisted(tmp_path):
    from srv.config import Settings

    (tmp_path / "camera.mac").write_text("78:20:51:1e:50:7a\n")
    s = Settings(host="10.1.1.143", password="x", mac="AA:BB:CC:DD:EE:FF", cache_root=tmp_path)
    s.load_persisted_mac()
    assert s.mac == "AA:BB:CC:DD:EE:FF"


def test_remember_mac_ignores_blank_and_zero(tmp_path):
    from srv.config import Settings

    s = Settings(host="10.1.1.143", password="x", cache_root=tmp_path)
    s.remember_mac("")
    s.remember_mac("00:00:00:00:00:00")
    assert s.mac is None
    assert not s.mac_file().exists()


# ── IP persistence (DHCP lease wanders between sleeps) ──────────────────────


def test_persisted_host_survives_restart_and_overrides_stale_env(tmp_path):
    from srv.config import Settings

    # Discovery moved us to a new lease while running.
    live = Settings(host="10.1.1.143", password="x", cache_root=tmp_path)
    live.remember_host("10.1.1.144")
    assert live.host == "10.1.1.144"
    assert live.host_file().read_text().strip() == "10.1.1.144"

    # Restart: env still points at the old (now dead) address; the persisted
    # last-known-good wins so we don't strand on it.
    fresh = Settings(host="10.1.1.143", password="x", cache_root=tmp_path)
    fresh.load_persisted_host()
    assert fresh.host == "10.1.1.144"


def test_load_persisted_host_noop_when_absent(tmp_path):
    from srv.config import Settings

    s = Settings(host="10.1.1.143", password="x", cache_root=tmp_path)
    s.load_persisted_host()
    assert s.host == "10.1.1.143"


def test_camera_update_host_persists_new_ip(tmp_path):
    from srv.camera import CameraConnection
    from srv.config import Settings

    s = Settings(host="10.1.1.143", password="x", cache_root=tmp_path)
    cam = CameraConnection(s, tapo_factory=lambda _s: object())
    assert cam.update_host("10.1.1.144") is True
    assert s.host_file().read_text().strip() == "10.1.1.144"
    # Unchanged IP is a no-op (no churn).
    assert cam.update_host("10.1.1.144") is False


# ── self-healing connect when the lease moved ("doesn't connect even awake") ─


def test_camera_get_self_heals_on_connection_failure(tmp_path):
    from srv.camera import CameraConnection
    from srv.config import Settings

    s = Settings(host="10.1.1.144", password="x", cache_root=tmp_path)
    built: list[str] = []

    def factory(settings):
        # Camera isn't at the stale .144; it's reachable at .143.
        if settings.host == "10.1.1.144":
            raise ConnectionError("Max retries exceeded with url: /")
        built.append(settings.host)
        return object()

    cam = CameraConnection(s, tapo_factory=factory, rediscover=lambda: "10.1.1.143")
    assert cam.get() is not None
    assert s.host == "10.1.1.143"
    assert s.host_file().read_text().strip() == "10.1.1.143"
    assert built == ["10.1.1.143"]


def test_camera_get_reraises_when_rediscovery_finds_nothing(tmp_path):
    from srv.camera import CameraConnection
    from srv.config import Settings

    s = Settings(host="10.1.1.144", password="x", cache_root=tmp_path)

    def factory(settings):
        raise ConnectionError("No route to host")

    cam = CameraConnection(s, tapo_factory=factory, rediscover=lambda: None)
    with pytest.raises(ConnectionError):
        cam.get()


def test_camera_get_sends_wol_to_mac_then_reconnects(tmp_path, monkeypatch):
    """The doorbell sleeps and stops answering on the LAN. get() must send a
    WoL magic packet to its MAC (what the mobile app does) and retry the
    handshake until it wakes — NOT scan with nmap."""
    import srv.camera as camera_mod
    from srv.camera import CameraConnection
    from srv.config import Settings

    waks: list[str] = []
    monkeypatch.setattr(camera_mod, "send_wol", lambda mac, subnet=None: waks.append(mac))
    monkeypatch.setattr(camera_mod.time, "sleep", lambda _s: None)
    # ARP can't see the asleep camera; the wake loop relies on retrying the host.
    monkeypatch.setattr(camera_mod, "read_arp_table", lambda: [])

    s = Settings(host="10.1.1.143", password="x", mac="78:20:51:1e:50:7a", cache_root=tmp_path)
    calls = {"n": 0}

    def factory(settings):
        calls["n"] += 1
        if calls["n"] < 3:  # asleep for the first two handshake attempts
            raise ConnectionError("No route to host")
        return object()  # woke up

    cam = CameraConnection(s, tapo_factory=factory)
    assert cam.get() is not None
    assert waks == ["78:20:51:1e:50:7a"]  # WoL fired at the real MAC
    assert calls["n"] == 3                 # retried the handshake until it woke


def test_camera_get_wol_picks_up_new_dhcp_lease(tmp_path, monkeypatch):
    """If the camera rejoins at a new DHCP address after waking, the wake loop
    re-resolves the IP from its (stable) MAC via ARP and connects there."""
    import srv.camera as camera_mod
    from srv.camera import CameraConnection
    from srv.config import Settings
    from srv.discovery import ArpEntry

    monkeypatch.setattr(camera_mod, "send_wol", lambda mac, subnet=None: None)
    monkeypatch.setattr(camera_mod.time, "sleep", lambda _s: None)
    # After WoL the camera shows up in ARP at a *different* address.
    monkeypatch.setattr(
        camera_mod, "read_arp_table",
        lambda: [ArpEntry("10.1.1.150", "78:20:51:1e:50:7a")],
    )

    s = Settings(host="10.1.1.143", password="x", mac="78:20:51:1e:50:7a", cache_root=tmp_path)

    def factory(settings):
        if settings.host == "10.1.1.143":
            raise ConnectionError("No route to host")  # old lease is dead
        return object()

    cam = CameraConnection(s, tapo_factory=factory)
    assert cam.get() is not None
    assert s.host == "10.1.1.150"
    assert s.host_file().read_text().strip() == "10.1.1.150"


def test_camera_with_retry_invalidates_stale_client_and_retries(tmp_path, monkeypatch):
    """After the camera goes back to sleep, the cached client is stale and the
    op fails mid-call. with_retry must drop it and rebuild (re-running the
    wake path) so a single request recovers — not return 'offline'."""
    import srv.camera as camera_mod
    from srv.camera import CameraConnection
    from srv.config import Settings

    monkeypatch.setattr(camera_mod, "send_wol", lambda mac, subnet=None: None)
    monkeypatch.setattr(camera_mod.time, "sleep", lambda _s: None)
    monkeypatch.setattr(camera_mod, "read_arp_table", lambda: [])

    s = Settings(host="10.1.1.143", password="x", mac="78:20:51:1e:50:7a", cache_root=tmp_path)
    clients: list[object] = []

    def factory(settings):
        c = object()
        clients.append(c)
        return c

    cam = CameraConnection(s, tapo_factory=factory)
    calls = {"n": 0}

    def op(client):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("No route to host")  # stale client mid-call
        return "ok"

    assert cam.with_retry(op) == "ok"
    assert calls["n"] == 2        # retried after invalidation
    assert len(clients) == 2      # a fresh client was built for the retry


def test_camera_with_retry_does_not_retry_non_conn_errors(tmp_path):
    from srv.camera import CameraConnection
    from srv.config import Settings

    s = Settings(host="10.1.1.143", password="x", cache_root=tmp_path)
    cam = CameraConnection(s, tapo_factory=lambda _s: object())
    calls = {"n": 0}

    def op(client):
        calls["n"] += 1
        raise ValueError("not a connection problem")

    with pytest.raises(ValueError):
        cam.with_retry(op)
    assert calls["n"] == 1  # a logic error must not trigger a wake/retry


def test_camera_get_does_not_rediscover_on_auth_error(tmp_path):
    from srv.camera import CameraConnection
    from srv.config import Settings

    s = Settings(host="10.1.1.144", password="x", cache_root=tmp_path)
    tried: list[int] = []

    def factory(settings):
        raise Exception("Invalid authentication: -40401")

    cam = CameraConnection(
        s, tapo_factory=factory, rediscover=lambda: tried.append(1) or "10.1.1.143"
    )
    with pytest.raises(Exception):
        cam.get()
    assert tried == []  # auth failures must not kick off an nmap sweep
