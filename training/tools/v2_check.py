#!/usr/bin/env python3
"""Ask the V2 who it is through the ESP. Stop the viewer first: a new connection replaces the old one.

    python3 tools/v2_check.py [host]          host defaults to boardcam.local
"""
import os
import subprocess
import sys
import threading
import time

host = sys.argv[1] if len(sys.argv) > 1 else "boardcam.local"
p = subprocess.Popen(["nc", host, "3333"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
got = bytearray()


def read():
    while True:
        d = os.read(p.stdout.fileno(), 4096)
        if not d:
            return
        got.extend(d)


threading.Thread(target=read, daemon=True).start()
time.sleep(1)
for command in ("AT+BREAK", "AT+ID?", "AT+VER?"):
    p.stdin.write((command + "\r\n").encode())
    time.sleep(1.5)
time.sleep(1)
p.kill()
print(f"{len(got)} bytes back")
print(bytes(got[:200]).decode("utf8", "replace").strip() or "The V2 didn't reply: see Faults in RUNBOOK.md")
