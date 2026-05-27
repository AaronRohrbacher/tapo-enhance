"""Discovery, ARP scanning, WoL packet building. Subprocess-free —
each helper is testable as a pure function over fake input."""

from __future__ import annotations

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
)


# ── normalize_mac ──────────────────────────────────────────────────────────


def test_normalize_mac_lowercases_and_replaces_dashes():
    assert normalize_mac("AA-BB-CC-DD-EE-FF") == "aa:bb:cc:dd:ee:ff"


def test_normalize_mac_passes_through_correct_form():
    assert normalize_mac("aa:bb:cc:dd:ee:ff") == "aa:bb:cc:dd:ee:ff"


def test_normalize_mac_handles_none():
    assert normalize_mac(None) is None
    assert normalize_mac("") is None


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
