#!/usr/bin/env python3
"""
Draw bounding boxes around bean bags in captured clips, using a confidential
(TEE-hosted, open-weight) vision model on SayGM, so frames stay private.

For each clip saved by capture.py:
  1. Motion pre-filter: the clip's per-pixel median is the empty scene (a bag
     only passes through for a few frames), so frames that barely differ from
     it are marked "no bag" without calling the model.
  2. Every frame with motion goes to the model, which returns boxes.
  3. Results go to boxes.json in the clip folder. Clips that already have one
     are skipped, so the script is safe to re-run or leave running.

Needs your SayGM key, stored once in the macOS Keychain (you'll be prompted to paste it):
    security add-generic-password -a "$USER" -s saygm-api-key -w

Usage:
    .venv/bin/python autobox.py --limit 50       # test run: first 50 frames sent to the model
    .venv/bin/python autobox.py                  # box every unprocessed clip
    .venv/bin/python autobox.py --watch          # keep going as capture.py saves new clips
    .venv/bin/python autobox.py --review         # build dataset/review.html to check the boxes
    .venv/bin/python autobox.py --compare        # same 30 frames through 6 private models, side by side
    .venv/bin/python autobox.py --model kimi-k3-tee   # use a different model
"""

import argparse
import asyncio
import base64
import html
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import openai

DATASET_DIR = Path(__file__).resolve().parent / "dataset"
DEFAULT_BASE_URL = "https://api.saygm.com/v1"
DEFAULT_MODEL = "mistral-large-4-tee"

PROMPT = """This is one frame from a camera mounted underneath a cornhole board, pointing up at the hole.

Find every cornhole bean bag that is visible, fully or partially: one falling through the hole, \
lying on the ground, sliding under the board, or held in a hand. A bag is a small square fabric pouch.

Return a tight box for each bag as x1,y1 (top-left) and x2,y2 (bottom-right) in coordinates \
normalised to 0-1000, where (0,0) is the image's top-left corner and (1000,1000) its bottom-right. \
If no bag is visible, return an empty list. Do not box hands, feet or other objects."""

def target_prompt(target):
    """Prompt for boxing any object, e.g. for live tests in viewer.py ("volleyball")."""
    return (f"Find every {target} visible in this camera frame, fully or partially.\n\n"
            "Return a tight box for each as x1,y1 (top-left) and x2,y2 (bottom-right) in coordinates "
            "normalised to 0-1000, where (0,0) is the image's top-left corner and (1000,1000) its bottom-right. "
            f"If no {target} is visible, return an empty list. Do not box anything else.")


JSON_HINT = '\n\nReply with only JSON, no other text: {"bags": [{"x1": 0, "y1": 0, "x2": 0, "y2": 0}]}'

SCHEMA = {
    "type": "object",
    "properties": {
        "bags": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "x1": {"type": "integer"},
                    "y1": {"type": "integer"},
                    "x2": {"type": "integer"},
                    "y2": {"type": "integer"},
                },
                "required": ["x1", "y1", "x2", "y2"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["bags"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------- motion filter

def motion_boxes(frames, threshold, min_area):
    """Per frame: bounding box of what differs from the clip's median scene, or None.

    Compares colour, not brightness: a red bag can be exactly as bright as grey decking."""
    blurred = np.stack([cv2.GaussianBlur(f, (5, 5), 0) for f in frames])
    background = np.median(blurred, axis=0).astype(np.uint8)
    out = []
    for b in blurred:
        diff = cv2.absdiff(b, background).max(axis=2)  # biggest change across B, G, R
        mask = cv2.threshold(diff, threshold, 255, cv2.THRESH_BINARY)[1]
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        if cv2.countNonZero(mask) < min_area:
            out.append(None)
            continue
        x, y, w, h = cv2.boundingRect(mask)
        out.append([int(x), int(y), int(x + w), int(y + h)])
    return out


# ---------------------------------------------------------------- model

def _is_box(b):
    return isinstance(b, dict) and all(k in b for k in ("x1", "y1", "x2", "y2"))


def parse_bags(text):
    """Pull the list of boxes out of a reply. Tolerates <think> blocks, code fences, a bare list,
    and the list under another name (models sometimes write {"volleyballs": [...]})."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        raise ValueError(f"no JSON in reply: {text[:120]!r}")
    data = json.JSONDecoder().raw_decode(text[min(starts):])[0]
    if isinstance(data, list):
        bags = data
    elif isinstance(data.get("bags"), list):
        bags = data["bags"]
    else:
        lists = [v for v in data.values() if isinstance(v, list)]
        if data.get("visible") is False or not lists:
            bags = []
        else:
            bags = lists[0]
    if not all(_is_box(b) for b in bags):
        raise ValueError(f"unexpected box format: {str(bags)[:120]}")
    return bags


KEYCHAIN_SERVICE = "saygm-api-key"


def saygm_key():
    """SAYGM_API_KEY from the environment, else from the macOS Keychain."""
    key = os.environ.get("SAYGM_API_KEY")
    if key:
        return key
    found = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                           capture_output=True, text=True)
    if found.returncode == 0 and found.stdout.strip():
        return found.stdout.strip()
    sys.exit("No SayGM key found. Store it in the Keychain (you'll be asked to paste it):\n"
             f"    security add-generic-password -a \"$USER\" -s {KEYCHAIN_SERVICE} -w\n"
             "or set SAYGM_API_KEY in your environment.")


class Boxer:
    def __init__(self, args):
        key = saygm_key()
        self.client = openai.AsyncOpenAI(
            api_key=key,
            base_url=os.environ.get("SAYGM_BASE_URL", DEFAULT_BASE_URL),
            max_retries=6,  # backs off automatically on 429 / 5xx
        )
        self.model = args.model
        self.upscale = args.upscale
        # Plain JSON by default: on a real photo, Mistral with strict json_schema output found a clearly
        # visible volleyball 0/6 times vs 6/6 with plain JSON prompting. --structured turns it back on.
        self.structured = getattr(args, "structured", False)
        self.sem = asyncio.Semaphore(args.concurrency)
        self.sent = 0

    async def box_frame(self, frame, prompt=PROMPT, retry_parse=True):
        """Return a list of [x1,y1,x2,y2] in the original frame's pixel space."""
        h, w = frame.shape[:2]
        big = cv2.resize(frame, (w * self.upscale, h * self.upscale), interpolation=cv2.INTER_CUBIC)
        jpeg = base64.b64encode(cv2.imencode(".jpg", big, [cv2.IMWRITE_JPEG_QUALITY, 92])[1]).decode()
        structured = self.structured  # this request's mode; the shared flag may flip mid-flight
        params = dict(
            model=self.model,
            messages=[{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{jpeg}"}},
                {"type": "text", "text": prompt + ("" if structured else JSON_HINT)},
            ]}],
        )
        if structured:
            params["response_format"] = {"type": "json_schema",
                                         "json_schema": {"name": "bags", "schema": SCHEMA, "strict": True}}

        async with self.sem:
            try:
                response = await self.client.chat.completions.create(**params)
            except openai.BadRequestError as e:
                if not (structured and any(k in str(e) for k in ("response_format", "json_schema"))):
                    raise
                response = None
            else:
                self.sent += 1
        if response is None:
            if self.structured:
                print("Model rejected json_schema output - falling back to plain JSON prompting.")
                self.structured = False
            return await self.box_frame(frame, prompt)

        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise RuntimeError("reply was cut off")
        try:
            bags = parse_bags(choice.message.content or "")
        except (ValueError, KeyError, AttributeError) as e:
            if retry_parse:  # one more go: the next reply is usually well-formed
                return await self.box_frame(frame, prompt, retry_parse=False)
            raise ValueError(f"unreadable reply: {e}")

        boxes = []
        for b in bags:
            x1, x2 = sorted((b["x1"] * w / 1000, b["x2"] * w / 1000))
            y1, y2 = sorted((b["y1"] * h / 1000, b["y2"] * h / 1000))
            x1, y1 = max(0, round(x1)), max(0, round(y1))
            x2, y2 = min(w, round(x2)), min(h, round(y2))
            if x2 - x1 >= 2 and y2 - y1 >= 2:
                boxes.append([x1, y1, x2, y2])
        return boxes


# ---------------------------------------------------------------- per clip

def pending_clips():
    clips = []
    for meta in sorted(DATASET_DIR.glob("*/*/*/meta.json")):
        if not (meta.parent / "boxes.json").exists():
            clips.append(meta.parent)
    return clips


async def process_clip(clip_dir, boxer, args, budget, prompt=PROMPT, target="bean bag"):
    paths = sorted(clip_dir.glob("[0-9][0-9][0-9].jpg"))
    frames = [cv2.imread(str(p)) for p in paths]
    if not frames or any(f is None for f in frames):
        print(f"skip {clip_dir.relative_to(DATASET_DIR)} (unreadable frames)")
        return

    motion = motion_boxes(frames, args.threshold, args.min_area)
    todo = [i for i, m in enumerate(motion) if m is not None]
    if budget is not None:
        todo = todo[:max(0, budget["left"])]
        budget["left"] -= len(todo)
        if not todo and any(m is not None for m in motion):
            return  # out of test budget; leave the clip unprocessed

    results = {}
    tasks = {i: asyncio.create_task(boxer.box_frame(frames[i], prompt)) for i in todo}
    for i, task in tasks.items():
        try:
            results[i] = {"boxes": await task, "source": "model"}
        except (openai.APIError, RuntimeError, ValueError, KeyError) as e:
            results[i] = {"boxes": None, "source": "error", "error": str(e)[:200]}

    out = {"model": boxer.model, "target": target, "frames": []}
    for i, p in enumerate(paths):
        entry = results.get(i) or {"boxes": [], "source": "no-motion" if motion[i] is None else "skipped"}
        out["frames"].append({"file": p.name, "motion": motion[i], **entry})

    if budget is not None and len(todo) < sum(m is not None for m in motion):
        out["partial"] = True  # test run only covered some frames; don't mark the clip done
        (clip_dir / "boxes.partial.json").write_text(json.dumps(out, indent=1))
    else:
        tmp = clip_dir / "boxes.json.tmp"
        tmp.write_text(json.dumps(out, indent=1))
        tmp.replace(clip_dir / "boxes.json")

    n_bag = sum(1 for f in out["frames"] if f["boxes"])
    n_err = sum(1 for f in out["frames"] if f["source"] == "error")
    print(f"{clip_dir.relative_to(DATASET_DIR)}: {len(paths)} frames, {len(todo)} sent, "
          f"{n_bag} with bags" + (f", {n_err} errors" if n_err else ""))


async def run(args):
    boxer = Boxer(args)
    budget = {"left": args.limit} if args.limit else None
    started = time.time()
    while True:
        clips = pending_clips()
        for clip in clips:
            if budget is not None and budget["left"] <= 0:
                break
            await process_clip(clip, boxer, args, budget)
        if budget is not None and budget["left"] <= 0:
            break
        if not args.watch:
            break
        await asyncio.sleep(10)
    print(f"Done: {boxer.sent} frames sent to {boxer.model} in {time.time() - started:.0f}s")


# ---------------------------------------------------------------- review page

def review():
    rows = []
    for boxes_file in sorted(DATASET_DIR.glob("*/*/*/boxes*.json")):
        clip = boxes_file.parent
        data = json.loads(boxes_file.read_text())
        cells = []
        for f in data["frames"]:
            if f["source"] == "no-motion":
                continue
            img = cv2.imread(str(clip / f["file"]))
            for x1, y1, x2, y2 in f["boxes"] or []:
                cv2.rectangle(img, (x1, y1), (x2, y2), (0, 255, 0), 2)
            if f["motion"]:
                x1, y1, x2, y2 = f["motion"]
                cv2.rectangle(img, (x1, y1), (x2, y2), (255, 160, 0), 1)
            b64 = base64.b64encode(cv2.imencode(".jpg", img)[1]).decode()
            note = f["source"] if f["source"] != "model" else f"{len(f['boxes'])} bag(s)"
            cells.append(f'<figure><img src="data:image/jpeg;base64,{b64}"><figcaption>{f["file"]} · {html.escape(note)}</figcaption></figure>')
        rel = clip.relative_to(DATASET_DIR)
        rows.append(f"<section><h2>{html.escape(str(rel))}</h2><div class=grid>{''.join(cells) or '<p>no motion frames</p>'}</div></section>")
    page = f"""<!doctype html><meta charset=utf-8><title>Box review</title>
<style>body{{font:14px system-ui;margin:16px;background:#111;color:#eee}}h2{{font-size:14px;margin:18px 0 6px}}
.grid{{display:flex;flex-wrap:wrap;gap:6px}}figure{{margin:0}}img{{width:180px;display:block}}figcaption{{font-size:11px;color:#aaa}}</style>
<p>Green = model's box. Thin orange = motion area (for comparison). Delete a clip's boxes.json to redo it.</p>
{''.join(rows) or '<p>No boxed clips yet.</p>'}"""
    out = DATASET_DIR / "review.html"
    out.write_text(page)
    print(f"Open {out}")


# ---------------------------------------------------------------- model comparison

COMPARE_MODELS = "mistral-large-4-tee,glm-5.3-flash-tee,qwen3.5-397b-a17b-tee,qwen3.8-27b-tee,kimi-k3-tee,gemma-4-31b-turbo-tee"


def iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union else 0.0


def sample_motion_frames(args, n):
    """The first n frames (across clips) where something moved."""
    picked = []
    for meta in sorted(DATASET_DIR.glob("*/*/*/meta.json")):
        paths = sorted(meta.parent.glob("[0-9][0-9][0-9].jpg"))
        frames = [cv2.imread(str(p)) for p in paths]
        if not frames or any(f is None for f in frames):
            continue
        for p, f, m in zip(paths, frames, motion_boxes(frames, args.threshold, args.min_area)):
            if m is not None:
                picked.append((p, f, m))
                if len(picked) == n:
                    return picked
    return picked


async def compare(args):
    models = [m.strip() for m in args.compare.split(",") if m.strip()]
    frames = sample_motion_frames(args, args.compare_frames)
    if not frames:
        sys.exit("No motion frames found - capture some clips first.")
    print(f"Comparing {len(models)} models on {len(frames)} frames...")

    results = {}
    for model in models:
        boxer = Boxer(argparse.Namespace(**{**vars(args), "model": model}))

        async def timed(frame, boxer=boxer):
            t = time.time()
            try:
                return await boxer.box_frame(frame), time.time() - t, None
            except (openai.APIError, RuntimeError, ValueError, KeyError) as e:
                return None, time.time() - t, str(e)[:120]

        t0 = time.time()
        results[model] = await asyncio.gather(*(timed(f) for _, f, _ in frames))
        ok = [r for r in results[model] if r[2] is None]
        ious = [max((iou(b, m) for b in r[0]), default=0.0) for r, (_, _, m) in zip(results[model], frames) if r[2] is None]
        print(f"  {model:28} {len(ok)}/{len(frames)} ok, mean overlap with motion area "
              f"{(sum(ious) / len(ious) if ious else 0):.2f}, {time.time() - t0:.0f}s total")

    # Summary table + one row per frame, one column per model.
    head = "".join(f"<th>{html.escape(m)}</th>" for m in models)
    summary = []
    for model in models:
        rs = results[model]
        ok = [r for r in rs if r[2] is None]
        ious = [max((iou(b, m) for b in r[0]), default=0.0) for r, (_, _, m) in zip(rs, frames) if r[2] is None]
        lat = sum(r[1] for r in rs) / len(rs)
        summary.append(f"<tr><td>{html.escape(model)}</td><td>{len(ok)}/{len(rs)}</td>"
                       f"<td>{sum(1 for r in ok if r[0])}</td><td>{(sum(ious) / len(ious) if ious else 0):.2f}</td>"
                       f"<td>{lat:.1f}s</td></tr>")
    rows = []
    for i, (path, frame, m) in enumerate(frames):
        cells = []
        for model in models:
            boxes, _, err = results[model][i]
            img = frame.copy()
            x1, y1, x2, y2 = m
            cv2.rectangle(img, (x1, y1), (x2, y2), (255, 160, 0), 1)
            for bx in boxes or []:
                cv2.rectangle(img, tuple(bx[:2]), tuple(bx[2:]), (0, 255, 0), 2)
            b64 = base64.b64encode(cv2.imencode(".jpg", img)[1]).decode()
            note = html.escape(err) if err else f"{len(boxes)} bag(s)"
            cells.append(f'<td><img src="data:image/jpeg;base64,{b64}"><div>{note}</div></td>')
        rel = path.relative_to(DATASET_DIR)
        rows.append(f"<tr><th>{html.escape(str(rel))}</th>{''.join(cells)}</tr>")
    page = f"""<!doctype html><meta charset=utf-8><title>Model comparison</title>
<style>body{{font:13px system-ui;margin:16px;background:#111;color:#eee}}table{{border-collapse:collapse}}
td,th{{padding:4px;vertical-align:top;border-bottom:1px solid #333;text-align:left}}img{{width:170px;display:block}}
div{{font-size:11px;color:#aaa}}.wrap{{overflow-x:auto}}</style>
<h1>Which model draws the best boxes?</h1>
<p>Green = the model's box. Thin orange = motion area (rough guide only: it also catches hands, shadows and shake).
"Overlap" is how well the model's box matches the motion area, from 0 to 1.</p>
<table><tr><th>Model</th><th>Answered</th><th>Frames with a bag</th><th>Overlap</th><th>Avg time/frame</th></tr>{''.join(summary)}</table>
<h2>Frame by frame</h2><div class=wrap><table><tr><th>Frame</th>{head}</tr>{''.join(rows)}</table></div>"""
    out = DATASET_DIR / "compare.html"
    out.write_text(page)
    print(f"Open {out}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--watch", action="store_true", help="keep running and box new clips as they appear")
    p.add_argument("--limit", type=int, help="stop after sending this many frames (test run)")
    p.add_argument("--review", action="store_true", help="build dataset/review.html and exit")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--compare", nargs="?", const=COMPARE_MODELS, metavar="MODELS",
                   help="send the same frames to several models (comma-separated) and build dataset/compare.html")
    p.add_argument("--compare-frames", type=int, default=30, help="frames to use with --compare")
    p.add_argument("--concurrency", type=int, default=8, help="frames in flight at once")
    p.add_argument("--upscale", type=int, default=2, help="enlarge frames before sending (helps box precision)")
    p.add_argument("--structured", action="store_true",
                   help="force the reply format with json_schema (off by default: it made Mistral miss objects)")
    p.add_argument("--threshold", type=int, default=25, help="pixel difference that counts as motion")
    p.add_argument("--min-area", type=int, default=40, help="changed pixels needed to count a frame as motion")
    args = p.parse_args()
    if args.review:
        review()
    elif args.compare:
        asyncio.run(compare(args))
    else:
        asyncio.run(run(args))


if __name__ == "__main__":
    main()
