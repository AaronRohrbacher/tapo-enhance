#!/usr/bin/env python3
"""Test Wake-on-LAN for the Tapo D225."""

import socket
import time

MAC = "78:20:51:1E:50:7A"
HOST = "10.1.1.153"


def send_wol(mac):
    mac_bytes = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    magic = b"\xff" * 6 + mac_bytes * 16
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.sendto(magic, ("255.255.255.255", 9))
    s.sendto(magic, ("10.1.1.255", 9))
    s.sendto(magic, ("10.1.1.255", 7))
    s.close()


def is_awake(host, port=443, timeout=1.5):
    s = socket.socket()
    s.settimeout(timeout)
    r = s.connect_ex((host, port))
    s.close()
    return r == 0


# Step 1: Wait for camera to sleep
print(f"Checking if {HOST} is awake...")
if is_awake(HOST):
    print("Camera is awake. Waiting for it to sleep...")
    print("(Close the Tapo app if it's open)")
    while is_awake(HOST):
        time.sleep(2)
        print("  still awake...", end="\r")
    print("\nCamera is asleep.")
else:
    print("Camera is already asleep.")

time.sleep(3)

# Step 2: Send WoL
print(f"\nSending WoL to {MAC}...")
send_wol(MAC)

# Step 3: Wait for it to wake
print("Waiting for camera to respond...")
for i in range(30):
    time.sleep(1)
    if is_awake(HOST):
        print(f"\n  CAMERA WOKE UP after {i+1}s!")
        # Check which ports are open
        for port in [443, 8800, 554, 2020]:
            s = socket.socket(); s.settimeout(2)
            r = s.connect_ex((HOST, port)); s.close()
            print(f"    {port}: {'OPEN' if r == 0 else 'closed'}")
        break
    if i % 5 == 4:
        print(f"  [{i+1}s] re-sending WoL...")
        send_wol(MAC)
else:
    print("\n  Camera did not wake after 30s. WoL may not be supported.")
    print("  The earlier wake might have been coincidence (motion/cloud push).")
