"""
Training labels: the AI's boxes from each recording plus your review on top.

Each recording folder (dataset/<model>-cycle-NN/<timestamp>/) holds frames NNN.jpg,
boxes.json (written by the recorder: the AI's verdict per frame) and, once reviewed,
review.json (written by the Review page). Review labels always win:

    ok       you checked the AI's label and it's right
    boxes    your own boxes ([x1, y1, x2, y2] pixels), replacing the AI's
    none     nothing to find in this frame: kept as a "nothing here" example
    exclude  leave the frame out of training

Without a review, frames the two models agreed on (or both saw nothing in) are used; "unsure",
failed and single-model frames (older recordings, before the two-model check) are left out.
"""

import json
import os
import re
import tempfile
import time
from pathlib import Path

LABELS = ("ok", "boxes", "none", "exclude")
TRUSTED = ("agree", "none")  # the two models agreed: a box in the same place, or nothing
_ID = re.compile(r"^[\w.-]+/[\w.-]+$")  # "<model>-cycle-NN/<timestamp>"


def clip_dir(dataset, clip_id):
    """The folder for a recording id, refusing anything that isn't one (e.g. '../')."""
    if not _ID.match(clip_id or "") or ".." in clip_id:
        return None
    d = Path(dataset) / clip_id
    return d if (d / "boxes.json").is_file() else None


def _read(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def load(dataset, clip_id):
    """Everything the Review page needs about one recording."""
    d = clip_dir(dataset, clip_id)
    if not d:
        return None
    ai = _read(d / "boxes.json", {"frames": []})
    review = _read(d / "review.json", {}).get("frames", {})
    frames = []
    for f in ai.get("frames", []):
        frames.append({**f, "review": review.get(f["file"])})
    return {"id": clip_id, "meta": _read(d / "meta.json", {}), "target": ai.get("target"),
            "model": ai.get("model"), "frames": frames}


def final(frame):
    """The label training uses: (boxes, why) or (None, why) to leave the frame out."""
    r = frame.get("review")
    if r:
        if r["label"] == "boxes":
            return r["boxes"], "yours"
        if r["label"] == "none":
            return [], "yours: nothing here"
        if r["label"] == "exclude":
            return None, "yours: excluded"
        # "ok": the AI's label, confirmed
    if frame.get("source") in TRUSTED and frame.get("boxes") is not None:
        return frame["boxes"], "AI" + (", checked" if r else "")
    if frame.get("source") in ("model", "no motion"):
        return None, "AI, one model only"
    return None, "AI unsure" if frame.get("source") == "unsure" else "AI failed"


def summary(dataset):
    """One line per recording, newest first, for the Review page's list."""
    out = []
    for boxes_file in sorted(Path(dataset).glob("*/*/boxes.json"), reverse=True):
        d = boxes_file.parent
        clip_id = f"{d.parent.name}/{d.name}"
        clip = load(dataset, clip_id)
        if not clip:
            continue
        frames = clip["frames"]
        reviewed = sum(1 for f in frames if f["review"])
        used = sum(1 for f in frames if final(f)[0] is not None)
        unsure = sum(1 for f in frames if not f["review"] and f.get("source") not in TRUSTED)
        out.append({"id": clip_id, "group": d.parent.name, "name": d.name, "target": clip["target"],
                    "frames": len(frames), "reviewed": reviewed, "used": used, "unsure": unsure})
    return out


def save(dataset, clip_id, file, label, boxes=None):
    """Store your label for one frame (label None clears it). Returns the frame's new final label."""
    d = clip_dir(dataset, clip_id)
    if not d or not (d / file).is_file() or "/" in file:
        raise ValueError("unknown recording or frame")
    if label is not None and label not in LABELS:
        raise ValueError(f"label must be one of {LABELS}")
    review = _read(d / "review.json", {"frames": {}})
    review.setdefault("frames", {})
    if label is None:
        review["frames"].pop(file, None)
    else:
        entry = {"label": label, "at": round(time.time())}
        if label == "boxes":
            clean = []
            for b in boxes or []:
                x1, y1, x2, y2 = (int(round(float(v))) for v in b)
                x1, x2 = sorted((x1, x2))
                y1, y2 = sorted((y1, y2))
                if x2 - x1 >= 3 and y2 - y1 >= 3:
                    clean.append([x1, y1, x2, y2])
            if not clean:  # "boxes" with nothing left means nothing here
                entry = {"label": "none", "at": entry["at"]}
            else:
                entry["boxes"] = clean
        review["frames"][file] = entry
    # write atomically: a half-written review.json would lose every label in the recording
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".review-", suffix=".json")
    with os.fdopen(fd, "w") as fh:
        json.dump(review, fh, indent=1)
    os.replace(tmp, d / "review.json")
    frame = next(f for f in load(dataset, clip_id)["frames"] if f["file"] == file)
    boxes_final, why = final(frame)
    return {"review": frame["review"], "final": boxes_final, "why": why}
