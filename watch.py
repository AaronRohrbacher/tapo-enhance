#!/usr/bin/env python3
"""
Watch for the Tapo D225 to wake up, then probe it instantly.

Usage: .venv/bin/python watch.py 10.1.1.153
       Then ring the doorbell or open live view in the Tapo app.
"""

import socket
import subprocess
import sys
import time

TARGET = sys.argv[1] if len(sys.argv) > 1 else "10.1.1.153"
PORTS = [443, 80, 554, 2020, 8800]


def check_port(ip, port, timeout=0.3):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        result = s.connect_ex((ip, port))
        s.close()
        return result == 0
    except Exception:
        return False


def main():
    print(f"Watching {TARGET} for signs of life...")
    print(f"Ring the doorbell, trigger motion, or open Live View in Tapo app.")
    print(f"Checking ports {PORTS} every 0.5s. Press Ctrl+C to stop.\n")

    attempt = 0
    while True:
        attempt += 1
        for port in PORTS:
            if check_port(TARGET, port):
                print(f"\n{'!'*60}")
                print(f"  PORT {port} IS OPEN on {TARGET}!")
                print(f"  Camera is awake! Launching probe...")
                print(f"{'!'*60}\n")
                subprocess.run([sys.executable, "probe.py", TARGET])
                print(f"\nResuming watch (camera may go back to sleep)...")
                time.sleep(5)  # Brief pause before resuming
                break

        if attempt % 20 == 0:
            print(f"  [{attempt}] Still watching... ({time.strftime('%H:%M:%S')})")

        time.sleep(0.5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
