"""Label a saved clip the way the viewer does, e.g. one recorded with no internet.

    .venv/bin/python tools/relabel_clip.py dataset/<model>-cycle-NN/<timestamp> [--force]

--force replaces an existing boxes.json. Needs internet and the SayGM key in the Keychain.
"""
import argparse, asyncio, json, sys, time
from collections import Counter
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import autobox
from viewer import Recorder as R

clip = Path(sys.argv[1])
meta = json.loads((clip / "meta.json").read_text())
if (clip / "boxes.json").exists() and "--force" not in sys.argv:
    sys.exit("Already labelled: add --force to replace boxes.json")
files = sorted(clip.glob("*.jpg"))
prompt = autobox.target_prompt(meta["target"], autobox.camera_note(meta["model_name"]))
boxers = [autobox.Boxer(argparse.Namespace(model=m, upscale=2, concurrency=64, reasoning_effort=R.EFFORT.get(m)))
          for m in R.CHECKERS]
results = [None] * len(files)

async def one(i, sem):
    img = cv2.imread(str(files[i]))
    async with sem:
        t = time.time()
        votes = await asyncio.gather(*(b.box_frame(img, prompt) for b in boxers), return_exceptions=True)
    errs = [f"{type(v).__name__}: {str(v)[:100]}" for v in votes if isinstance(v, Exception)]
    if errs:
        boxes, verdict = None, "error"
    else:
        a, b = votes
        if not a and not b:
            boxes, verdict = [], "none"
        elif a and b and max(autobox.iou(x, y) for x in a for y in b) > 0.3:
            boxes, verdict = a, "agree"
        else:
            boxes, verdict = None, "unsure"
    results[i] = {"boxes": boxes, "latency": round(time.time() - t, 1), "error": "; ".join(errs) or None,
                  "verdict": verdict,
                  "votes": {"qwen": None if isinstance(votes[0], Exception) else votes[0],
                            "glm": None if isinstance(votes[1], Exception) else votes[1]}}
    print(files[i].name, verdict, results[i]["latency"], "ds", len(votes[0]) if isinstance(votes[0], list) else "ERR",
          "glm", len(votes[1]) if isinstance(votes[1], list) else "ERR", flush=True)

async def main():
    sem = asyncio.Semaphore(R.CONCURRENCY)
    await asyncio.gather(*(one(i, sem) for i in range(len(files))))

t0 = time.time()
asyncio.run(main())
(clip / "boxes.json").write_text(json.dumps({
    "model": " + ".join(R.CHECKERS), "target": meta["target"],
    "frames": [{"file": f.name, "motion": None, "boxes": r["boxes"], "source": r["verdict"], "votes": r["votes"],
                "latency": r["latency"], **({"error": r["error"]} if r["error"] else {})}
               for f, r in zip(files, results)],
}, indent=1))
print("done in", round(time.time() - t0), "s", Counter(r["verdict"] for r in results))
