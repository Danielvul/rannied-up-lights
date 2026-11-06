#!/usr/bin/env python3
"""Forward 127.0.0.1:3334 to the ESP through nc, for when macOS blocks Python from the local network.

    python3 tools/esp_tunnel.py [host]        host defaults to boardcam.local
    .venv/bin/python viewer.py --port socket://127.0.0.1:3334
"""
import os
import socket
import subprocess
import sys
import threading

HOST = sys.argv[1] if len(sys.argv) > 1 else "boardcam.local"


def serve(c):
    p = subprocess.Popen(["nc", HOST, "3333"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)

    def down():
        try:
            while True:
                d = os.read(p.stdout.fileno(), 65536)
                if not d:
                    break
                c.sendall(d)
        except OSError:
            pass
        c.close()

    threading.Thread(target=down, daemon=True).start()
    try:
        while True:
            d = c.recv(4096)
            if not d:
                break
            p.stdin.write(d)
    except OSError:
        pass
    p.kill()


srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", 3334))
srv.listen(1)
print(f"Tunnel ready: 127.0.0.1:3334 -> {HOST}:3333 (Ctrl+C to stop)", flush=True)
while True:
    conn, _ = srv.accept()
    threading.Thread(target=serve, args=(conn,), daemon=True).start()
