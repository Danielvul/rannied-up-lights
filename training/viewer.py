#!/usr/bin/env python3
"""
Live camera viewer for the Grove Vision AI V2: watch the stream in a browser
while you mount the camera and judge the light.

    .venv/bin/python viewer.py            # then open http://localhost:8765

Shows fps, brightness, clipped highlights/shadows, contrast, focus and motion,
with optional overlays. Only one program can use the USB port at a time, so
quit the viewer before running capture.py.
"""

import argparse
import glob
import json
import queue
import re
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cv2
import numpy as np
import serial

import autobox
from capture import DATASET_DIR, RESOLUTIONS, VisionAI

PAGE = Path(__file__).resolve().parent / "viewer.html"
SNAP_DIR = DATASET_DIR / "snapshots"


def find_port_quiet():
    for pattern in ("/dev/cu.usbmodem*", "/dev/cu.wchusbserial*", "/dev/cu.usbserial*"):
        ports = sorted(glob.glob(pattern))
        if ports:
            return ports[0]
    return None


class Camera:
    """Owns the USB connection: (re)connects, keeps the stream running, applies settings."""

    def __init__(self, port=None, res=0):
        self.port_arg = port
        self.res = res
        self.dev = None
        self.status = "starting"
        self.started = time.time()
        self.commands = queue.Queue()
        threading.Thread(target=self._loop, daemon=True).start()

    def set_resolution(self, res):
        self.commands.put(("res", res))

    def speed_test(self, seconds=4):
        """Measure on-board detection speed (no photos sent), then resume the photo stream."""
        done = {"event": threading.Event()}
        self.commands.put(("speed", (seconds, done)))
        if not done["event"].wait(seconds + 10):
            return {"error": "camera busy or disconnected"}
        return done["result"]

    def _speed_test(self, seconds):
        # The live picture is capped near 15 fps by sending photos over serial; detection on the V2
        # sends only results, so this shows how fast the camera and model really run.
        dev = self.dev
        self.status = "measuring detection speed"
        dev.request("BREAK", timeout=1)
        dev.results.clear()
        r = dev.request("INVOKE=-1,0,1", timeout=3)  # forever, every frame, results only
        if not r or r.get("code") != 0:
            result = {"error": f"the V2 couldn't run its model (code {r.get('code') if r else 'no reply'})"}
        else:
            time.sleep(seconds)
            dev.request("BREAK", timeout=1)
            got = list(dev.results)[2:]  # skip warm-up
            times = [t for t, _ in got]
            fps = (len(times) - 1) / (times[-1] - times[0]) if len(times) > 2 else 0.0
            perf = got[-1][1].get("perf") if got else None
            result = {"fps": round(fps, 1), "frames": len(times), "model_ms": perf[1] if perf else None}
        self._start_stream()
        return result

    def _start_stream(self):
        dev = self.dev
        dev.request("BREAK", timeout=1)
        r = dev.request(f"SENSOR=1,1,{self.res}")
        self.started = time.time()
        if not r:
            self.status = "no reply from the camera, reconnecting..."
            return False
        if r.get("code") != 0:
            self.status = f"camera failed to start (code {r.get('code')}): check the ribbon"
            return False
        dev.frames.clear()
        dev.send("SAMPLE=-1")
        self.status = "streaming"
        return True

    def _drop(self, status):
        """Close the connection; the loop opens a fresh one. Fixes dead WiFi links after a XIAO reboot."""
        self.status = status
        try:
            self.dev.close()
        except Exception:
            pass
        self.dev = None
        time.sleep(1)

    def _loop(self):
        while True:
            if self.dev is None:
                port = self.port_arg or find_port_quiet()
                if not port:
                    self.status = "no device: plug the Vision AI V2 in over USB"
                    time.sleep(1)
                    continue
                try:
                    self.dev = VisionAI(port)
                    self.status = "connecting"
                    time.sleep(0.5)
                    self._start_stream()
                except (serial.SerialException, OSError) as e:
                    self.status = f"can't open {port}: {e}. Is capture.py or SenseCraft using it?"
                    self.dev = None
                    time.sleep(2)
                    continue
            try:
                kind, value = self.commands.get(timeout=0.5)
                if kind == "res":
                    self.res = value
                    self._start_stream()
                elif kind == "speed":
                    seconds, done = value
                    done["result"] = self._speed_test(seconds)
                    done["event"].set()
            except queue.Empty:
                pass
            except (serial.SerialException, OSError):
                pass
            # unplugged / connection lost?
            if not self.dev._reader.is_alive():
                self._drop("device disconnected, reconnecting...")
                continue
            # Camera didn't start: reconnect and try again (never give up)
            if self.status != "streaming" and time.time() - self.started > 3:
                self._drop(self.status)
                continue
            # Stream stalled for 4 s (longer than a WiFi hiccup the XIAO can buffer): reconnect
            if self.status == "streaming" and time.time() - max(self.dev.last_frame_time, self.started) > 4:
                self._drop("stream stalled, reconnecting...")
                continue


class Analyser:
    """Turns each new frame into stats plus a display JPEG with the chosen overlays."""

    def __init__(self, cam):
        self.cam = cam
        self.overlays = {"zebra": False, "shadows": False, "motion": False, "grid": False}
        self.threshold = 25
        self.min_area = 40
        self.stats = {}
        self.display = None
        self.raw = None
        self.seq = 0
        self.cond = threading.Condition()
        self.times = []
        self.background = None
        self.ai = None  # set by main(): AIChecker whose boxes are drawn on the feed
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        last_count = -1
        while True:
            dev = self.cam.dev
            if dev is None or dev.frame_count == last_count or not dev.frames:
                time.sleep(0.005)
                if dev is None:
                    last_count = -1
                continue
            last_count = dev.frame_count
            t, jpeg = dev.frames[-1]
            img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                continue
            self._analyse(t, jpeg, img)

    def _analyse(self, t, jpeg, img):
        self.times = [x for x in self.times + [t] if t - x <= 2]
        fps = (len(self.times) - 1) / (self.times[-1] - self.times[0]) if len(self.times) > 1 else 0
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        hist = cv2.calcHist([gray], [0], None, [64], [0, 256]).ravel()
        n = gray.size
        blown = float((img.max(axis=2) >= 250).sum()) / n
        crushed = float((gray <= 8).sum()) / n
        sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var())

        # Motion against a slowly updating background (same colour-difference idea as autobox.py)
        blurred = cv2.GaussianBlur(img, (5, 5), 0).astype(np.float32)
        if self.background is None or self.background.shape != blurred.shape:
            self.background = blurred.copy()
        diff = cv2.absdiff(blurred, self.background).max(axis=2).astype(np.uint8)
        cv2.accumulateWeighted(blurred, self.background, 0.05)
        mask = cv2.threshold(diff, self.threshold, 255, cv2.THRESH_BINARY)[1]
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        moving = int(cv2.countNonZero(mask))
        box = cv2.boundingRect(mask) if moving >= self.min_area else None

        self.stats = {
            "fps": round(fps, 1),
            "width": img.shape[1], "height": img.shape[0],
            "kb": round(len(jpeg) / 1024, 1),
            "brightness": round(float(gray.mean()), 1),
            "contrast": round(float(gray.std()), 1),
            "blown": round(blown * 100, 1),
            "crushed": round(crushed * 100, 1),
            "sharpness": round(sharp, 0),
            "motion": round(moving / n * 100, 2),
            "motion_box": box,
            "hist": [round(float(h) / n * 100, 2) for h in hist],
            "status": self.cam.status,
            "resolution": self.cam.res,
            "frame_time": t,
            "bad_frames": self.cam.dev.bad_frames if self.cam.dev else 0,
            "overlays": self.overlays,
            "threshold": self.threshold,
        }

        out = img
        ai = self.ai.result if self.ai else None
        show_ai = bool(ai and ai["boxes"] and time.time() - ai["frame_time"] < 30)
        if any(self.overlays.values()) or show_ai:
            out = img.copy()
            if self.overlays["zebra"]:
                stripes = ((np.indices(gray.shape).sum(axis=0) // 4) % 2 == 0)
                out[(img.max(axis=2) >= 250) & stripes] = (255, 0, 255)
            if self.overlays["shadows"]:
                out[gray <= 8] = (255, 120, 0)
            if self.overlays["motion"]:
                tint = out.copy()
                tint[mask > 0] = (0, 255, 0)
                out = cv2.addWeighted(out, 0.6, tint, 0.4, 0)
                if box:
                    x, y, w, h = box
                    cv2.rectangle(out, (x, y), (x + w, y + h), (0, 255, 0), 1)
            if self.overlays["grid"]:
                h, w = gray.shape
                for i in (1, 2):
                    cv2.line(out, (w * i // 3, 0), (w * i // 3, h), (255, 255, 255), 1)
                    cv2.line(out, (0, h * i // 3), (w, h * i // 3), (255, 255, 255), 1)
                cv2.drawMarker(out, (w // 2, h // 2), (0, 255, 255), cv2.MARKER_CROSS, 16, 1)
            if show_ai:  # boxes are from the frame that was sent, a few seconds ago
                for x1, y1, x2, y2 in ai["boxes"]:
                    cv2.rectangle(out, (x1, y1), (x2, y2), (0, 140, 255), 2)
                    cv2.putText(out, ai["target"], (x1 + 2, max(10, y1 - 3)), cv2.FONT_HERSHEY_SIMPLEX,
                                0.35, (0, 140, 255), 1, cv2.LINE_AA)
            jpeg_out = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes()
        else:
            jpeg_out = jpeg
        with self.cond:
            self.raw = jpeg
            self.display = jpeg_out
            self.seq += 1
            self.cond.notify_all()


class AIChecker:
    """Sends the latest frame to SayGM's private Mistral model and keeps its answer for the overlay.
    One request at a time: "live" sends the next frame as soon as the previous answer arrives."""

    def __init__(self, ana):
        self.ana = ana
        self.target = "volleyball"
        self.live = False
        self.wake = threading.Event()
        self.result = None   # latest answer: boxes, latency, when, error
        self.busy = False
        self.boxer = None
        threading.Thread(target=self._loop, daemon=True).start()

    def ask(self, target=None, live=None, once=False):
        if target:
            self.target = str(target)[:60]
        if live is not None:
            self.live = bool(live)
        if once or self.live:
            self.wake.set()

    def status(self):
        r = dict(self.result) if self.result else None
        if r:
            r["age"] = round(time.time() - r.pop("frame_time"), 1)
        return {"target": self.target, "live": self.live, "busy": self.busy,
                "model": autobox.DEFAULT_MODEL, "result": r}

    def _loop(self):
        import argparse as _ap
        import asyncio
        loop = asyncio.new_event_loop()
        while True:
            self.wake.wait()
            self.wake.clear()
            jpeg = self.ana.raw
            if not jpeg:
                time.sleep(0.5)
                if self.live:
                    self.wake.set()
                continue
            frame_time = self.ana.stats.get("frame_time", time.time())
            img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            self.busy = True
            t = time.time()
            try:
                if self.boxer is None:
                    self.boxer = autobox.Boxer(_ap.Namespace(model=autobox.DEFAULT_MODEL, upscale=2, concurrency=1))
                boxes = loop.run_until_complete(self.boxer.box_frame(img, autobox.target_prompt(self.target)))
                self.result = {"boxes": boxes, "latency": round(time.time() - t, 1), "frame_time": frame_time,
                               "target": self.target, "error": None}
            except Exception as e:  # show any failure on the page rather than killing the worker
                self.result = {"boxes": [], "latency": round(time.time() - t, 1), "frame_time": frame_time,
                               "target": self.target, "error": f"{type(e).__name__}: {str(e)[:200]}"}
            self.busy = False
            if self.live:
                self.wake.set()


class Recorder:
    """Records the next few seconds of frames, then sends every frame to Mistral and keeps the
    results for the page's photo grid. Each recording is saved as training data in
    dataset/<model>-cycle-NN/<timestamp>/ (frames + meta.json + boxes.json, autobox's format)."""

    CONCURRENCY = 8  # frames in flight at once; answers fill in one by one as they arrive
    # Two private models. On a real recording Mistral alone boxed a wooden post in 14-15 of 16 empty
    # frames, while GLM rarely invents objects. Rule: GLM sees nothing -> "none"; both box it in the same
    # place -> "agree"; otherwise "unsure", left out of training rather than saved as a wrong label.
    CHECKERS = ("mistral-large-4-tee", "glm-5.3-flash-tee")

    def __init__(self, cam):
        self.cam = cam
        self.lock = threading.Lock()
        self.state = "idle"   # idle -> recording -> checking -> done
        self.frames = []      # jpeg bytes
        self.results = []     # per frame: None while waiting, else {boxes, latency, error}
        self.target = ""
        self.clip_dir = None
        self.started = self.finished = 0.0
        self.run_id = 0
        self.error = None
        self.model_name = "OpenVolleyball"
        self.cycle = self.latest_cycle(self.model_name)

    @staticmethod
    def clean_name(name):
        return re.sub(r"[^A-Za-z0-9_-]", "", str(name))[:40] or "OpenVolleyball"

    @staticmethod
    def latest_cycle(model_name):
        cycles = [int(m.group(1)) for d in DATASET_DIR.glob(f"{model_name}-cycle-*")
                  if (m := re.fullmatch(rf"{re.escape(model_name)}-cycle-(\d+)", d.name))]
        return max(cycles, default=1)

    def folder(self):
        return DATASET_DIR / f"{self.model_name}-cycle-{self.cycle:02d}"

    def configure(self, model_name=None, cycle=None, new_cycle=False):
        with self.lock:
            if model_name is not None:
                name = self.clean_name(model_name)
                if name != self.model_name:
                    self.model_name, self.cycle = name, self.latest_cycle(name)
            if cycle is not None:
                self.cycle = max(1, min(999, int(cycle)))
            if new_cycle:
                self.cycle = self.latest_cycle(self.model_name) + 1

    def start(self, target, seconds=2.0):
        with self.lock:
            if self.state in ("recording", "checking"):
                return False
            self.state, self.target, self.error = "recording", target, None
            self.frames, self.results, self.clip_dir = [], [], None
            self.started, self.finished = time.time(), 0.0
            self.run_id += 1
        threading.Thread(target=self._run, args=(seconds,), daemon=True).start()
        return True

    def status(self):
        with self.lock:
            done = [r for r in self.results if r is not None]
            end = self.finished or time.time()
            return {
                "state": self.state, "run": self.run_id, "target": self.target, "error": self.error,
                "model_name": self.model_name, "cycle": self.cycle,
                "folder": str(self.folder().relative_to(DATASET_DIR.parent)),
                "clips_in_cycle": len(list(self.folder().glob("*/meta.json"))),
                "frames": len(self.frames), "checked": len(done),
                "found": sum(1 for r in done if r["boxes"]),
                "failed": sum(1 for r in done if r["error"]),
                "elapsed": round(end - self.started, 1) if self.started else 0,
                "clip": str(self.clip_dir.relative_to(DATASET_DIR.parent)) if self.clip_dir else None,
                "unsure": sum(1 for r in done if r["verdict"] == "unsure"),
                "results": [None if r is None else {"n": len(r["boxes"] or []), "latency": r["latency"],
                                                    "error": r["error"], "verdict": r["verdict"]}
                            for r in self.results],
            }

    def thumbnail(self, i):
        with self.lock:
            if not 0 <= i < len(self.frames):
                return None
            jpeg, r = self.frames[i], self.results[i]
        img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None
        r = r or {}
        if r.get("verdict") == "unsure":  # show what each model thought, thin
            for colour, key in (((0, 140, 255), "mistral"), ((255, 200, 0), "glm")):
                for x1, y1, x2, y2 in (r.get("votes") or {}).get(key) or []:
                    cv2.rectangle(img, (x1, y1), (x2, y2), colour, 1)
        for x1, y1, x2, y2 in r.get("boxes") or []:
            cv2.rectangle(img, (x1, y1), (x2, y2), (0, 140, 255), 2)
        return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes()

    def _run(self, seconds):
        import argparse as _ap
        import asyncio
        try:
            start = time.time()
            time.sleep(seconds + 0.4)  # frames arrive a little after they're taken
            dev = self.cam.dev
            picked = [(t, j) for t, j in list(dev.frames) if start <= t <= start + seconds] if dev else []
            frames = [j for _, j in picked]
            if not frames:
                raise RuntimeError("no frames arrived while recording (is the camera streaming?)")
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            clip_dir = self.folder() / stamp
            clip_dir.mkdir(parents=True, exist_ok=True)
            for i, j in enumerate(frames):
                (clip_dir / f"{i:03d}.jpg").write_bytes(j)
            span = picked[-1][0] - picked[0][0]
            (clip_dir / "meta.json").write_text(json.dumps({
                "model_name": self.model_name, "cycle": self.cycle, "target": self.target,
                "frames": len(frames), "seconds": seconds, "start": picked[0][0], "end": picked[-1][0],
                "fps": round((len(frames) - 1) / span, 1) if span > 0 else None,
                "resolution": RESOLUTIONS[self.cam.res], "source": "viewer record button",
            }, indent=2))
            with self.lock:
                self.frames, self.results = frames, [None] * len(frames)
                self.clip_dir, self.state = clip_dir, "checking"

            boxers = [autobox.Boxer(_ap.Namespace(model=m, upscale=2, concurrency=64)) for m in self.CHECKERS]
            sem = None  # created inside the event loop below
            prompt = autobox.target_prompt(self.target)

            async def one(i):
                img = cv2.imdecode(np.frombuffer(frames[i], np.uint8), cv2.IMREAD_COLOR)
                async with sem:  # time only the requests themselves, not the wait for a free slot
                    t = time.time()
                    votes = await asyncio.gather(*(b.box_frame(img, prompt) for b in boxers), return_exceptions=True)
                errs = [f"{type(v).__name__}: {str(v)[:100]}" for v in votes if isinstance(v, Exception)]
                if errs:
                    boxes, verdict = None, "error"
                else:
                    a, b = votes  # Mistral, GLM
                    if not b:
                        # GLM's "nothing here" is trustworthy (Mistral's "yes" is not: it boxes posts/furniture)
                        boxes, verdict = [], "none"
                    elif a and max(autobox.iou(x, y) for x in a for y in b) > 0.3:
                        boxes, verdict = a, "agree"  # both see it in the same place; Mistral's box is tighter
                    else:
                        boxes, verdict = None, "unsure"  # only GLM sees it: leave it out
                with self.lock:
                    self.results[i] = {"boxes": boxes, "latency": round(time.time() - t, 1),
                                       "error": "; ".join(errs) or None, "verdict": verdict,
                                       "votes": {"mistral": None if isinstance(votes[0], Exception) else votes[0],
                                                 "glm": None if isinstance(votes[1], Exception) else votes[1]}}

            async def all_frames():
                nonlocal sem
                sem = asyncio.Semaphore(self.CONCURRENCY)
                await asyncio.gather(*(one(i) for i in range(len(frames))))

            asyncio.run(all_frames())
            (clip_dir / "boxes.json").write_text(json.dumps({  # same shape as autobox.py writes
                "model": " + ".join(self.CHECKERS), "target": self.target,
                # boxes: list = trusted label ([] = no object); None = unsure or error, leave out of training
                "frames": [{"file": f"{i:03d}.jpg", "motion": None, "boxes": r["boxes"], "source": r["verdict"],
                            "votes": r["votes"], "latency": r["latency"],
                            **({"error": r["error"]} if r["error"] else {})}
                           for i, r in enumerate(self.results)],
            }, indent=1))
            with self.lock:
                self.state = "done"
        except Exception as e:
            with self.lock:
                self.state, self.error = "done", f"{type(e).__name__}: {e}"
        finally:
            self.finished = time.time()


def make_handler(cam, ana, ai, rec):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                body = PAGE.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/stats":
                stats = dict(ana.stats) if ana.stats else {"overlays": ana.overlays}
                stats.update(status=cam.status, resolution=cam.res)
                if "frame_time" in stats:
                    stats["age"] = round(time.time() - stats.pop("frame_time"), 2)
                stats["ai"] = ai.status()
                self._json(stats)
            elif self.path == "/rec":
                self._json(rec.status())
            elif self.path.startswith("/rec/") and self.path.split("?")[0].endswith(".jpg"):
                try:
                    i = int(self.path.split("?")[0][len("/rec/"):-len(".jpg")])
                except ValueError:
                    return self.send_error(404)
                body = rec.thumbnail(i)
                if body is None:
                    return self.send_error(404)
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/stream"):
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                seen = -1
                try:
                    while True:
                        with ana.cond:
                            ana.cond.wait_for(lambda: ana.seq != seen and ana.display is not None, timeout=5)
                            if ana.seq == seen or ana.display is None:
                                continue  # no frame yet (camera still starting)
                            seen, frame = ana.seq, ana.display
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                         + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass
            else:
                self.send_error(404)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._json({"error": "bad json"}, 400)
            if self.path == "/config":
                if "resolution" in data and int(data["resolution"]) in RESOLUTIONS:
                    cam.set_resolution(int(data["resolution"]))
                for k, v in (data.get("overlays") or {}).items():
                    if k in ana.overlays:
                        ana.overlays[k] = bool(v)
                if "threshold" in data:
                    ana.threshold = max(5, min(100, int(data["threshold"])))
                self._json({"ok": True})
            elif self.path == "/rec":
                target = str(data.get("target") or ai.target)[:60]
                seconds = max(0.5, min(10.0, float(data.get("seconds") or 2)))
                rec.configure(model_name=data.get("model_name"), cycle=data.get("cycle"))
                ok = rec.start(target, seconds)
                self._json({"ok": ok, "error": None if ok else "already recording or checking"}, 200 if ok else 409)
            elif self.path == "/rec/config":
                rec.configure(model_name=data.get("model_name"), cycle=data.get("cycle"),
                              new_cycle=bool(data.get("new_cycle")))
                self._json(rec.status())
            elif self.path == "/speed":
                self._json(cam.speed_test(max(1.0, min(10.0, float(data.get("seconds") or 4)))))
            elif self.path == "/ai":
                ai.ask(target=data.get("target"), live=data.get("live"), once=bool(data.get("once")))
                self._json({"ok": True})
            elif self.path == "/snapshot":
                if not ana.raw:
                    return self._json({"error": "no frame yet"}, 409)
                SNAP_DIR.mkdir(parents=True, exist_ok=True)
                name = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3] + ".jpg"
                (SNAP_DIR / name).write_bytes(ana.raw)
                self._json({"ok": True, "file": f"dataset/snapshots/{name}"})
            else:
                self.send_error(404)

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial port, or socket://boardcam.local:3333 for WiFi (default: auto-detect USB)")
    ap.add_argument("--res", type=int, choices=sorted(RESOLUTIONS), default=0)
    ap.add_argument("--http", type=int, default=8765, help="web port (default 8765)")
    args = ap.parse_args()
    cam = Camera(args.port, args.res)
    ana = Analyser(cam)
    ai = AIChecker(ana)
    ana.ai = ai
    rec = Recorder(cam)
    server = ThreadingHTTPServer(("127.0.0.1", args.http), make_handler(cam, ana, ai, rec))
    server.daemon_threads = True
    print(f"Viewer running: open http://localhost:{args.http}  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if cam.dev:
            cam.dev.close()


if __name__ == "__main__":
    main()
