#!/usr/bin/env python3
"""
Cornhole training-data capture for the Grove Vision AI V2.

Streams JPEG frames from the V2 over USB (SSCMA AT protocol), shows a live
preview, and keeps the last few seconds in memory. After each throw, press a
key and those seconds are saved to disk as a labelled clip:

    h  hit      - bag went through the hole
    u  under    - bag went under the board
    b  bounce   - bag hit the board but didn't go in
    n  nothing  - no throw / people / hands / dogs (negative examples)
    z  undo the last saved clip
    q  quit (or Esc)

Clips land in  dataset/<session>/<label>/<timestamp>/000.jpg ...
with a meta.json per clip and a log.csv per session.

Usage:
    .venv/bin/python capture.py --session garage-night
    .venv/bin/python capture.py --check          # just test the connection
"""

import argparse
import base64
import binascii
import collections
import csv
import glob
import json
import os
import shutil
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import serial

LABELS = {ord("h"): "hit", ord("u"): "under", ord("b"): "bounce", ord("n"): "nothing"}
RESOLUTIONS = {0: "240x240", 1: "480x480", 2: "640x480"}
DATASET_DIR = Path(__file__).resolve().parent / "dataset"
BAUD = 921600


def find_port():
    ports = [p for pattern in ("/dev/cu.usbmodem*", "/dev/cu.wchusbserial*", "/dev/cu.usbserial*")
             for p in sorted(glob.glob(pattern))]
    if not ports:
        sys.exit("No USB serial device found. Is the Vision AI V2 plugged in with a data USB-C cable?")
    if len(ports) > 1:
        print(f"Several serial ports found, using {ports[0]} (pass --port to choose): {ports}")
    return ports[0]


_decode_lock = threading.Lock()


def jpeg_ok(jpeg):
    """Trial-decode a JPEG. False if it's broken, including damage only the decoder notices
    (it prints "Corrupt JPEG data" to stderr rather than failing), which we catch here."""
    if not (jpeg.startswith(b"\xff\xd8") and jpeg.rstrip(b"\x00").endswith(b"\xff\xd9")):
        return False
    with _decode_lock:
        r, w = os.pipe()
        saved = os.dup(2)
        os.dup2(w, 2)
        try:
            img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        finally:
            os.dup2(saved, 2)
            os.close(saved)
            os.close(w)
        os.set_blocking(r, False)
        try:
            warnings = os.read(r, 65536)
        except BlockingIOError:
            warnings = b""
        os.close(r)
    return img is not None and b"Corrupt JPEG" not in warnings


class SocketPort:
    """The bits of a pyserial port VisionAI uses, over TCP to the XIAO WiFi bridge (socket://host:port)."""

    def __init__(self, url, timeout=0.1):
        import socket
        hostport = url.split("://", 1)[1].strip("/")
        host, _, port = hostport.rpartition(":")
        self._socket_mod = socket
        self.sock = socket.create_connection((host, int(port)), timeout=5)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.settimeout(timeout)
        self.in_waiting = 0

    def read(self, _size=1):
        try:
            data = self.sock.recv(65536)
        except self._socket_mod.timeout:
            return b""
        except OSError as e:
            raise serial.SerialException(f"WiFi bridge connection lost: {e}")
        if not data:
            raise serial.SerialException("WiFi bridge closed the connection")
        return data

    def write(self, data):
        try:
            self.sock.sendall(data)
        except OSError as e:
            raise serial.SerialException(f"WiFi bridge connection lost: {e}")

    def close(self):
        self.sock.close()


class VisionAI:
    """Minimal SSCMA AT-protocol client: send commands, collect JSON replies and frames."""

    def __init__(self, port, baud=BAUD):
        if "://" in port:  # e.g. socket://boardcam.local:3333 via the XIAO WiFi bridge
            self.ser = SocketPort(port)
        else:
            self.ser = serial.Serial(port, baud, timeout=0.1)
        self.replies = collections.deque(maxlen=50)
        self.frames = collections.deque(maxlen=300)  # (timestamp, jpeg bytes)
        self.results = collections.deque(maxlen=300)  # (timestamp, data) from INVOKE without images
        self.last_frame_time = 0.0
        self.frame_count = 0
        self.bad_frames = 0  # damaged in transit and skipped
        self._running = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def send(self, cmd):
        self.ser.write(f"AT+{cmd}\r".encode())

    def request(self, cmd, timeout=3.0):
        """Send a command and wait for its operation reply (type 0)."""
        name = cmd.split("=")[0]
        self.send(cmd)
        deadline = time.time() + timeout
        while time.time() < deadline:
            for r in list(self.replies):
                if r.get("type") == 0 and r.get("name") == name:
                    self.replies.remove(r)
                    return r
            time.sleep(0.02)
        return None

    def _read_loop(self):
        buf = b""
        while self._running:
            try:
                chunk = self.ser.read(self.ser.in_waiting or 1)
            except serial.SerialException:
                break
            if not chunk:
                continue
            buf += chunk.replace(b"\r", b"\n")  # replies are framed by \r...\n (sometimes reversed)
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line.startswith(b"{"):
                    continue  # firmware log output, ignore
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                image = (msg.get("data") or {}).get("image") if isinstance(msg.get("data"), dict) else None
                if msg.get("type") == 1 and image:
                    # A byte lost on the wire (more likely over WiFi) breaks a frame: skip it, don't crash.
                    try:
                        jpeg = base64.b64decode(image, validate=True)
                    except (binascii.Error, ValueError):
                        jpeg = b""
                    if not jpeg_ok(jpeg):
                        self.bad_frames += 1
                        continue
                    self.frames.append((time.time(), jpeg))
                    self.last_frame_time = time.time()
                    self.frame_count += 1
                elif msg.get("type") == 1 and msg.get("name") == "INVOKE":
                    self.results.append((time.time(), msg.get("data") or {}))  # on-board detections
                else:
                    self.replies.append(msg)

    def close(self):
        try:
            self.send("BREAK")
        except serial.SerialException:
            pass
        self._running = False
        self.ser.close()


def check(dev):
    for cmd in ("NAME?", "VER?", "SENSORS?", "MODEL?"):
        r = dev.request(cmd)
        print(f"{cmd:9} -> {json.dumps(r['data']) if r else 'no reply'}")
    r = dev.request("SENSOR=1,1,0")
    if not r or r.get("code") != 0:
        code = r.get("code") if r else "no reply"
        print(f"Camera failed to start (code {code}). Code 4 means the board got no answer from the camera:")
        print("unplug USB, reseat the ribbon at both ends (contacts facing the socket's pins), and try again.")
        return
    print("Camera started. Grabbing one frame...")
    dev.send("SAMPLE=1")
    deadline = time.time() + 5
    while not dev.frames and time.time() < deadline:
        time.sleep(0.05)
    if dev.frames:
        _, jpeg = dev.frames[-1]
        out = DATASET_DIR / "check.jpg"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(jpeg)
        print(f"OK - camera works. Frame saved to {out}")
    else:
        print("No frame received. Check the camera ribbon (try flipping it at one end).")


def save_clip(session_dir, label, frames, seconds, resolution):
    now = time.time()
    clip = [(t, j) for t, j in frames if now - t <= seconds]
    if not clip:
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    clip_dir = session_dir / label / stamp
    clip_dir.mkdir(parents=True)
    for i, (_, jpeg) in enumerate(clip):
        (clip_dir / f"{i:03d}.jpg").write_bytes(jpeg)
    duration = clip[-1][0] - clip[0][0]
    meta = {
        "session": session_dir.name,
        "label": label,
        "frames": len(clip),
        "start": clip[0][0],
        "end": clip[-1][0],
        "fps": round((len(clip) - 1) / duration, 1) if duration > 0 else None,
        "resolution": resolution,
    }
    (clip_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    log = session_dir / "log.csv"
    new = not log.exists()
    with log.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["clip", "label", "frames", "fps", "saved_at"])
        w.writerow([stamp, label, len(clip), meta["fps"], datetime.now().isoformat(timespec="seconds")])
    return clip_dir


def count_clips(session_dir):
    return {label: len(list((session_dir / label).glob("*/"))) if (session_dir / label).exists() else 0
            for label in LABELS.values()}


def draw_overlay(frame, session, fps, counts, flash):
    h, w = frame.shape[:2]
    cv2.rectangle(frame, (0, 0), (w, 44), (0, 0, 0), -1)
    cv2.putText(frame, f"{session}  {fps:4.1f} fps", (8, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
    tally = "  ".join(f"{k[0].upper()}:{v}" for k, v in counts.items())
    cv2.putText(frame, f"{tally}   [h]it [u]nder [b]ounce [n]othing [z]undo [q]uit",
                (8, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (200, 200, 200), 1)
    if flash:
        text, colour = flash
        cv2.rectangle(frame, (0, h - 40), (w, h), colour, -1)
        cv2.putText(frame, text, (10, h - 13), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)


def capture(dev, args):
    resolution = RESOLUTIONS[args.res]
    session_dir = DATASET_DIR / args.session
    session_dir.mkdir(parents=True, exist_ok=True)
    dev.request("BREAK")
    dev.request(f"SENSOR=1,1,{args.res}")
    dev.send("SAMPLE=-1")  # continuous streaming

    polling = False
    started = last_request = time.time()
    history = []  # saved clip dirs, for undo
    flash, flash_until = None, 0.0
    fps_window = collections.deque(maxlen=30)
    last_seen = 0
    blank = np.zeros((480, 480, 3), np.uint8)

    print(f"Saving to {session_dir}\nThrow, then press h / u / b / n within a couple of seconds.")
    while True:
        # Fall back to requesting frames one at a time if continuous mode isn't supported.
        if not polling and dev.frame_count == 0 and time.time() - started > 3:
            print("No continuous stream - switching to polling mode.")
            polling = True
        if polling and (dev.frame_count != last_seen or time.time() - last_request > 0.5):
            dev.send("SAMPLE=1")
            last_request = time.time()

        frame = blank.copy()
        if dev.frames:
            t, jpeg = dev.frames[-1]
            if dev.frame_count != last_seen:
                fps_window.append(t)
                last_seen = dev.frame_count
            decoded = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            if decoded is not None:
                frame = cv2.resize(decoded, (480, 480 * decoded.shape[0] // decoded.shape[1]))
        fps = (len(fps_window) - 1) / (fps_window[-1] - fps_window[0]) if len(fps_window) > 1 and fps_window[-1] > fps_window[0] else 0.0
        if time.time() - dev.last_frame_time > 2:
            flash = ("No frames from camera - check ribbon / USB", (0, 0, 180))
            flash_until = time.time() + 0.5

        draw_overlay(frame, args.session, fps, count_clips(session_dir),
                     flash if time.time() < flash_until else None)
        cv2.imshow("Cornhole capture", frame)
        key = cv2.waitKey(1) & 0xFF

        if key in (ord("q"), 27):
            break
        if key in LABELS:
            label = LABELS[key]
            clip_dir = save_clip(session_dir, label, list(dev.frames), args.seconds, resolution)
            if clip_dir:
                history.append(clip_dir)
                n = len(list(clip_dir.glob("*.jpg")))
                flash = (f"Saved {label.upper()} ({n} frames)", (0, 140, 0) if label == "hit" else (140, 90, 0))
                print(f"saved {label:8} {n:3} frames  {clip_dir.relative_to(DATASET_DIR)}")
            else:
                flash = ("Nothing to save - no recent frames", (0, 0, 180))
            flash_until = time.time() + 1.0
        elif key == ord("z") and history:
            clip_dir = history.pop()
            shutil.rmtree(clip_dir)
            flash, flash_until = (f"Undid {clip_dir.parent.name}", (90, 90, 90)), time.time() + 1.0
            print(f"undo     {clip_dir.relative_to(DATASET_DIR)}")

    cv2.destroyAllWindows()
    print("Session totals:", count_clips(session_dir))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--session", help="name for this session, e.g. garage-night or lawn-sunny")
    p.add_argument("--port", help="serial port, or socket://boardcam.local:3333 for WiFi (default: auto-detect USB)")
    p.add_argument("--baud", type=int, default=BAUD, help=f"serial baud rate (default {BAUD})")
    p.add_argument("--res", type=int, default=0, choices=RESOLUTIONS,
                   help="0=240x240 (fastest), 1=480x480, 2=640x480")
    p.add_argument("--seconds", type=float, default=3.0, help="seconds of footage saved per clip")
    p.add_argument("--check", action="store_true", help="print device info, grab one frame, exit")
    args = p.parse_args()
    if not args.check and not args.session:
        p.error("--session is required (or use --check)")

    dev = VisionAI(args.port or find_port(), args.baud)
    try:
        check(dev) if args.check else capture(dev, args)
    finally:
        dev.close()


if __name__ == "__main__":
    main()
