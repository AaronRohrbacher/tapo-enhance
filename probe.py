#!/usr/bin/env python3
"""
Tapo D225 Camera Probe Tool

Finds your Tapo camera on the local network and tries every known way in:
- Network scan for Tapo devices (ports 443, 554, 2020, 8800)
- Default/hardcoded credentials from TP-Link firmware
- KLAP handshake with blank and default creds
- RTSP with common credential combos
- ONVIF discovery on port 2020
- Unauthenticated API endpoint probing
"""

import asyncio
import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

# ── Known default credentials ──────────────────────────────────────────────
# From python-kasa source (kasa/credentials.py) - hardcoded in TP-Link firmware

DEFAULT_CREDS = [
    # TAPOCAMERA defaults (most likely for Tapo cameras)
    ("admin", "admin"),
    # TAPO defaults
    ("test@tp-link.net", "test"),
    # KASA defaults
    ("kasa@tp-link.net", "kasaSetup"),
    # KASACAMERA - password is MD5 of "admin"
    ("admin", "21232f297a57a5a743894a0e4a801fc3"),
    # Common defaults
    ("admin", ""),
    ("admin", "password"),
    ("admin", "12345"),
    ("admin", "123456"),
    ("", ""),
]

TAPO_PORTS = {
    443: "Control API (HTTPS)",
    554: "RTSP",
    2020: "ONVIF",
    8800: "Media Stream",
    80: "HTTP",
}


def banner(msg):
    print(f"\n{'='*60}")
    print(f"  {msg}")
    print(f"{'='*60}")


def status(msg, level="info"):
    symbols = {"info": "[*]", "ok": "[+]", "fail": "[-]", "warn": "[!]"}
    print(f"  {symbols.get(level, '[*]')} {msg}")


# ── Network Discovery ──────────────────────────────────────────────────────

def get_local_subnet():
    """Get the local subnet in CIDR notation."""
    try:
        result = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.strip().split("\n"):
            parts = line.split()
            if "src" in parts:
                src_idx = parts.index("src")
                ip = parts[src_idx + 1]
                # Assume /24 subnet
                return ".".join(ip.split(".")[:3]) + ".0/24", ip
    except Exception:
        pass
    return None, None


def scan_port(ip, port, timeout=1.0):
    """Check if a port is open on an IP."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((ip, port))
        sock.close()
        return result == 0
    except Exception:
        return False


def find_tapo_devices(subnet_base):
    """Scan the local /24 subnet for devices with Tapo-like open ports."""
    banner("Network Discovery")
    status(f"Scanning {subnet_base}...")

    candidates = []
    base = ".".join(subnet_base.split(".")[:3])

    def check_host(i):
        ip = f"{base}.{i}"
        # Quick check on port 443 first (all Tapo cameras have this)
        if scan_port(ip, 443, timeout=0.5):
            open_ports = {443}
            for port in [554, 2020, 8800]:
                if scan_port(ip, port, timeout=0.5):
                    open_ports.add(port)
            return ip, open_ports
        return None

    with ThreadPoolExecutor(max_workers=50) as executor:
        results = list(executor.map(check_host, range(1, 255)))

    for r in results:
        if r:
            ip, ports = r
            candidates.append((ip, ports))
            port_str = ", ".join(f"{p} ({TAPO_PORTS.get(p, '?')})" for p in sorted(ports))
            status(f"Found: {ip} - ports: {port_str}", "ok")

    if not candidates:
        status("No Tapo-like devices found on the network.", "fail")
        status("Make sure your camera is powered on and connected to the same network.", "warn")

    return candidates


# ── Probe HTTPS / Control API ──────────────────────────────────────────────

def probe_https(ip):
    """Probe the HTTPS control API for info and auth behavior."""
    banner(f"Probing HTTPS Control API ({ip}:443)")

    # 1. Check if KLAP (unauthenticated GET)
    import requests
    requests.packages.urllib3.disable_warnings()

    try:
        r = requests.get(f"https://{ip}:443", timeout=5, verify=False)
        status(f"GET / -> status {r.status_code}, body length {len(r.text)}")
        is_klap = "200 OK" in r.text
        status(f"KLAP protocol: {'Yes' if is_klap else 'No (legacy stok)'}")
        if r.text and len(r.text) < 500:
            status(f"Response: {r.text[:200]}")
    except Exception as e:
        status(f"GET / failed: {e}", "fail")
        is_klap = None

    # 2. Try HTTP (non-SSL) for unauthenticated info
    try:
        r = requests.get(f"http://{ip}:443", timeout=3, verify=False)
        if r.status_code == 200:
            status(f"HTTP (non-SSL) responded: {r.text[:200]}", "ok")
    except Exception:
        pass

    # 3. Try unauthenticated stok login with each credential set
    status("Trying login with known default credentials...")
    working_creds = []

    for user, password in DEFAULT_CREDS:
        try:
            result = try_stok_login(ip, user, password)
            if result:
                status(f"LOGIN SUCCESS: user={user!r} pass={password!r}", "ok")
                working_creds.append((user, password, result))
            else:
                status(f"  {user!r}/{password!r} - failed")
        except Exception as e:
            status(f"  {user!r}/{password!r} - error: {e}")

    return working_creds, is_klap


def try_stok_login(ip, user, password):
    """Try to get a stok token via the legacy login method."""
    import requests
    requests.packages.urllib3.disable_warnings()

    hashed_pw = hashlib.md5(password.encode("utf8")).hexdigest().upper()

    # Try insecure login (hashed password)
    data = {
        "method": "login",
        "params": {
            "hashed": True,
            "password": hashed_pw,
            "username": user,
        },
    }

    try:
        r = requests.post(
            f"https://{ip}:443",
            json=data,
            timeout=5,
            verify=False,
            headers={
                "Host": f"{ip}:443",
                "Referer": f"https://{ip}",
                "User-Agent": "Tapo CameraClient Android",
                "requestByApp": "true",
                "Content-Type": "application/json; charset=UTF-8",
            },
        )
        resp = r.json()
        if resp.get("error_code", -1) == 0 and "result" in resp:
            stok = resp["result"].get("stok")
            if stok:
                return {"stok": stok, "response": resp}
        return None
    except Exception:
        return None


# ── Probe RTSP ─────────────────────────────────────────────────────────────

def probe_rtsp(ip):
    """Try RTSP with various credentials."""
    banner(f"Probing RTSP ({ip}:554)")

    if not scan_port(ip, 554, timeout=2):
        status("Port 554 not open - RTSP may not be enabled.", "fail")
        status("Enable in Tapo app: Tapo Lab > Third-Party Compatibility", "warn")
        return []

    status("Port 554 open, trying credentials...")
    working = []

    for user, password in DEFAULT_CREDS:
        if try_rtsp(ip, user, password):
            status(f"RTSP SUCCESS: user={user!r} pass={password!r}", "ok")
            working.append((user, password))
        else:
            status(f"  {user!r}/{password!r} - failed")

    # Also try no auth at all
    if try_rtsp(ip, None, None):
        status("RTSP SUCCESS: No authentication required!", "ok")
        working.append((None, None))

    return working


def try_rtsp(ip, user, password):
    """Try to connect to RTSP and get a response."""
    try:
        if user is not None:
            u = quote(user, safe="")
            p = quote(password, safe="")
            url = f"rtsp://{u}:{p}@{ip}:554/stream2"
        else:
            url = f"rtsp://{ip}:554/stream2"

        # Use ffprobe to test the stream
        result = subprocess.run(
            [
                "ffprobe",
                "-rtsp_transport", "tcp",
                "-v", "error",
                "-show_entries", "stream=codec_name,width,height",
                "-of", "json",
                url,
            ],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and "codec_name" in result.stdout:
            return True
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        pass
    return False


# ── Probe ONVIF ────────────────────────────────────────────────────────────

def probe_onvif(ip):
    """Try ONVIF on port 2020."""
    banner(f"Probing ONVIF ({ip}:2020)")

    if not scan_port(ip, 2020, timeout=2):
        status("Port 2020 not open - ONVIF not enabled.", "fail")
        return []

    status("Port 2020 open, trying ONVIF GetDeviceInformation...")

    # Try without auth first
    working = []
    info = try_onvif_get_info(ip, None, None)
    if info:
        status("ONVIF responds WITHOUT authentication!", "ok")
        status(f"  Device info: {info}", "ok")
        working.append((None, None, info))

    for user, password in DEFAULT_CREDS:
        info = try_onvif_get_info(ip, user, password)
        if info:
            status(f"ONVIF SUCCESS: user={user!r} pass={password!r}", "ok")
            status(f"  Device info: {info}", "ok")
            working.append((user, password, info))
            break

    return working


def try_onvif_get_info(ip, user, password):
    """Send ONVIF GetDeviceInformation request."""
    # Build WS-Security header if credentials provided
    if user and password:
        import datetime
        import secrets
        nonce = secrets.token_bytes(16)
        nonce_b64 = base64.b64encode(nonce).decode()
        created = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        digest_input = nonce + created.encode() + password.encode()
        digest = base64.b64encode(hashlib.sha1(digest_input).digest()).decode()

        security = f"""
        <s:Header>
          <Security xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
            <UsernameToken>
              <Username>{user}</Username>
              <Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</Password>
              <Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{nonce_b64}</Nonce>
              <Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</Created>
            </UsernameToken>
          </Security>
        </s:Header>"""
    else:
        security = ""

    soap = f"""<?xml version="1.0" encoding="utf-8"?>
    <s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
                xmlns:tds="http://www.onvif.org/ver10/device/wsdl">
      {security}
      <s:Body>
        <tds:GetDeviceInformation/>
      </s:Body>
    </s:Envelope>"""

    try:
        import requests
        r = requests.post(
            f"http://{ip}:2020/onvif/device_service",
            data=soap,
            headers={"Content-Type": "application/soap+xml; charset=utf-8"},
            timeout=5,
        )
        if r.status_code == 200 and "Manufacturer" in r.text:
            # Parse out useful info
            try:
                root = ET.fromstring(r.text)
                ns = {"tds": "http://www.onvif.org/ver10/device/wsdl"}
                info_el = root.find(".//{http://www.onvif.org/ver10/device/wsdl}GetDeviceInformationResponse")
                if info_el is not None:
                    info = {}
                    for child in info_el:
                        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                        info[tag] = child.text
                    return info
            except Exception:
                return {"raw": r.text[:300]}
        return None
    except Exception:
        return None


# ── Probe Media Port ───────────────────────────────────────────────────────

def probe_media(ip):
    """Check if port 8800 is open (proprietary streaming)."""
    banner(f"Probing Media Stream ({ip}:8800)")

    if scan_port(ip, 8800, timeout=2):
        status("Port 8800 open - proprietary media stream available.", "ok")
        status("This port handles video download/streaming (needs cloud password for AES).", "warn")
        return True
    else:
        status("Port 8800 not open.", "fail")
        return False


# ── KLAP Handshake Probe ──────────────────────────────────────────────────

async def probe_klap(ip):
    """Try KLAP handshake with default credentials."""
    banner(f"Probing KLAP Protocol ({ip})")

    try:
        from kasa import DeviceConfig, Credentials
        from kasa.transports import KlapTransportV2, KlapTransport
    except ImportError:
        status("python-kasa not available for KLAP probe", "warn")
        return []

    working = []

    cred_sets = [
        ("blank", Credentials("", "")),
        ("TAPOCAMERA", Credentials("admin", "admin")),
        ("TAPO", Credentials("test@tp-link.net", "test")),
        ("KASA", Credentials("kasa@tp-link.net", "kasaSetup")),
    ]

    for name, creds in cred_sets:
        for version, Transport in [(2, KlapTransportV2), (1, KlapTransport)]:
            try:
                config = DeviceConfig(ip, port_override=443, credentials=creds)
                transport = Transport(config=config)
                await transport.perform_handshake()
                status(f"KLAP v{version} SUCCESS with {name}: user={creds.username!r} pass={creds.password!r}", "ok")
                working.append((creds.username, creds.password, version))
                await transport.close()
                break  # Don't try v1 if v2 worked
            except Exception as e:
                err = str(e)
                if "authentication" in err.lower():
                    pass  # Expected
                else:
                    status(f"  KLAP v{version} {name}: {err[:80]}")
            finally:
                try:
                    await transport.close()
                except Exception:
                    pass

    return working


# ── pytapo Full Connection Test ────────────────────────────────────────────

def try_pytapo(ip, user, password, cloud_password=""):
    """Try a full pytapo connection."""
    try:
        from pytapo import Tapo
        t = Tapo(
            ip, user, password,
            cloudPassword=cloud_password,
            printDebugInformation=False,
        )
        info = t.getBasicInfo()
        return info
    except Exception as e:
        return None


# ── Main ───────────────────────────────────────────────────────────────────

async def main():
    banner("Tapo D225 Camera Probe Tool")
    print("  Scanning your network for the camera and trying every way in.")
    print("  This is YOUR device - we're just finding the door that's open.")

    # Check for specific IP from env or args
    target_ip = os.getenv("TAPO_HOST", "")
    if len(sys.argv) > 1:
        target_ip = sys.argv[1]

    if target_ip:
        status(f"Target IP specified: {target_ip}")
        candidates = [(target_ip, set())]
        # Check which ports are open
        for port in TAPO_PORTS:
            if scan_port(target_ip, port, timeout=2):
                candidates[0][1].add(port)
                status(f"  Port {port} ({TAPO_PORTS[port]}): OPEN", "ok")
            else:
                status(f"  Port {port} ({TAPO_PORTS[port]}): closed", "fail")
    else:
        subnet, local_ip = get_local_subnet()
        if not subnet:
            status("Could not determine local subnet. Specify IP: python probe.py <ip>", "fail")
            return
        status(f"Local IP: {local_ip}, Subnet: {subnet}")
        candidates = find_tapo_devices(subnet)

    if not candidates:
        status("No candidates found. Try specifying the IP directly:", "warn")
        status("  .venv/bin/python probe.py 192.168.1.xxx", "warn")
        return

    # Probe each candidate
    all_results = {}

    for ip, ports in candidates:
        banner(f"Full probe of {ip}")
        results = {
            "ip": ip,
            "open_ports": sorted(ports) if ports else [],
            "https_creds": [],
            "rtsp_creds": [],
            "onvif_creds": [],
            "klap_creds": [],
            "pytapo_creds": [],
            "media_port": False,
        }

        # HTTPS control API
        https_creds, is_klap = probe_https(ip)
        results["https_creds"] = https_creds
        results["is_klap"] = is_klap

        # RTSP
        rtsp_creds = probe_rtsp(ip)
        results["rtsp_creds"] = rtsp_creds

        # ONVIF
        onvif_results = probe_onvif(ip)
        results["onvif_creds"] = onvif_results

        # Media port
        results["media_port"] = probe_media(ip)

        # KLAP
        klap_results = await probe_klap(ip)
        results["klap_creds"] = klap_results

        # If we got any HTTPS or KLAP creds, try full pytapo
        all_creds_to_try = set()
        for user, pw, *_ in https_creds:
            all_creds_to_try.add((user, pw))
        for user, pw, *_ in klap_results:
            all_creds_to_try.add((user, pw))
        # Also try all defaults through pytapo directly
        for user, pw in DEFAULT_CREDS:
            all_creds_to_try.add((user, pw))

        if all_creds_to_try:
            banner(f"Trying pytapo full connection ({ip})")
            for user, pw in all_creds_to_try:
                info = try_pytapo(ip, user, pw)
                if info:
                    status(f"PYTAPO SUCCESS: user={user!r} pass={pw!r}", "ok")
                    results["pytapo_creds"].append((user, pw, info))
                    # Also try as cloud password for media access
                    info2 = try_pytapo(ip, user, pw, cloud_password=pw)
                    if info2:
                        results["pytapo_creds"].append((user, pw, info2))

        all_results[ip] = results

    # ── Summary ────────────────────────────────────────────────────────────
    banner("RESULTS SUMMARY")

    for ip, r in all_results.items():
        print(f"\n  Camera: {ip}")
        print(f"  Open ports: {r['open_ports']}")

        any_success = False

        if r["pytapo_creds"]:
            any_success = True
            user, pw, info = r["pytapo_creds"][0]
            print(f"\n  >>> FULL CONTROL via pytapo <<<")
            print(f"  Username: {user}")
            print(f"  Password: {pw}")
            device_info = info.get("device_info", {}).get("basic_info", {})
            print(f"  Device: {device_info.get('device_alias', '?')} ({device_info.get('device_model', '?')})")
            print(f"  Firmware: {device_info.get('sw_version', '?')}")
            print(f"\n  To use the app:")
            print(f"  1. cp .env.example .env")
            print(f"  2. Set TAPO_HOST={ip}")
            print(f"  3. Set TAPO_USER={user}")
            print(f"  4. Set TAPO_PASSWORD={pw}")
            if pw:
                print(f"  5. Set TAPO_CLOUD_PASSWORD={pw}")
            print(f"  6. .venv/bin/python app.py")

        if r["rtsp_creds"]:
            any_success = True
            user, pw = r["rtsp_creds"][0]
            if user is None:
                print(f"\n  >>> RTSP OPEN (no auth!) <<<")
                print(f"  URL: rtsp://{ip}:554/stream1")
            else:
                print(f"\n  >>> RTSP ACCESS <<<")
                print(f"  Username: {user}")
                print(f"  Password: {pw}")
                u, p = quote(user, safe=""), quote(pw, safe="")
                print(f"  HD: rtsp://{u}:{p}@{ip}:554/stream1")
                print(f"  SD: rtsp://{u}:{p}@{ip}:554/stream2")

        if r["klap_creds"]:
            any_success = True
            user, pw, ver = r["klap_creds"][0]
            print(f"\n  >>> KLAP v{ver} ACCESS <<<")
            print(f"  Username: {user}")
            print(f"  Password: {pw}")

        if r["onvif_creds"]:
            any_success = True
            for user, pw, info in r["onvif_creds"]:
                auth_str = f"{user}/{pw}" if user else "NO AUTH"
                print(f"\n  >>> ONVIF ACCESS ({auth_str}) <<<")
                if isinstance(info, dict):
                    for k, v in info.items():
                        if k != "raw":
                            print(f"    {k}: {v}")

        if not any_success:
            print(f"\n  No working credentials found.")
            print(f"  Suggestions:")
            if 554 not in (r.get("open_ports") or []):
                print(f"    - RTSP port 554 is closed. In Tapo app: Tapo Lab > Third-Party Compatibility > ON")
            if 2020 not in (r.get("open_ports") or []):
                print(f"    - ONVIF port 2020 is closed. Same toggle as above.")
            print(f"    - Factory reset: hold the reset button on the D225 for 5+ seconds.")
            print(f"      After reset, the camera enters setup mode with default credentials.")
            print(f"      Re-pair with Tapo app, then run this probe again.")
            print(f"    - If you can see the camera in the Tapo app, your TP-Link account")
            print(f"      password IS the cloud password. Try resetting it if forgotten.")

    # Save results
    results_path = Path(__file__).parent / "probe_results.json"
    serializable = {}
    for ip, r in all_results.items():
        sr = dict(r)
        # Clean up non-serializable items
        sr["pytapo_creds"] = [(u, p) for u, p, _ in sr.get("pytapo_creds", [])]
        sr["onvif_creds"] = [(u, p, str(i)) for u, p, i in sr.get("onvif_creds", [])]
        sr["klap_creds"] = [(u, p, v) for u, p, v in sr.get("klap_creds", [])]
        serializable[ip] = sr

    results_path.write_text(json.dumps(serializable, indent=2))
    status(f"\nResults saved to {results_path}", "ok")


if __name__ == "__main__":
    asyncio.run(main())
