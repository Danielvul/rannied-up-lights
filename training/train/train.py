#!/usr/bin/env python3
"""
Train a detector from your reviewed recordings and compile it for the Grove Vision AI V2.

    .venv-train/bin/python train/train.py --model-name OpenVolleyball

Steps:
  1. Collect every recording of the model (all cycles, or --cycle N), using the label each frame
     ends up with (labels.py: your review wins, then the frames both AI models agreed on).
  2. Hold back whole recordings as the test set (--test-clips, default 2: the ones you reviewed
     most). Neighbouring frames are near-copies, so a test frame from a training clip would flatter
     the model.
  3. Fine-tune YOLOv8n (pretrained) on the rest, on the Mac's GPU.
  4. Score it on the held-back recordings.
  5. Export to 8-bit TFLite (int8 in and out, what the V2 firmware expects) and compile it with
     Vela for the V2's Ethos-U55.
Output: train/models/<name>-<date>-<size>_vela.tflite, plus the command to flash it.
"""

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import labels  # noqa: E402

DATASET_DIR = HERE.parent / "dataset"  # as in capture.py (not imported: it needs the camera's serial library)
VELA = HERE.parent / ".venv-train" / "bin" / "vela"


def clips_for(model_name, cycle):
    prefix = f"{model_name}-cycle-"
    out = [c for c in labels.summary(DATASET_DIR) if c["group"].startswith(prefix)]
    if cycle is not None:
        out = [c for c in out if c["group"] == f"{prefix}{cycle:02d}"]
    return out


def write_split(clips, split, root):
    """Write YOLO images/labels for these clips; returns (frames, frames with an object)."""
    (root / "images" / split).mkdir(parents=True, exist_ok=True)
    (root / "labels" / split).mkdir(parents=True, exist_ok=True)
    n = with_obj = 0
    for c in clips:
        clip = labels.load(DATASET_DIR, c["id"])
        src = DATASET_DIR / c["id"]
        for f in clip["frames"]:
            boxes, _ = labels.final(f)
            if boxes is None:
                continue  # left out: unsure, failed, or excluded by you
            img = cv2.imread(str(src / f["file"]))
            if img is None:
                continue
            h, w = img.shape[:2]
            stem = f"{c['name']}_{Path(f['file']).stem}"
            shutil.copy(src / f["file"], root / "images" / split / f"{stem}.jpg")
            lines = []
            for x1, y1, x2, y2 in boxes:
                x1, x2 = max(0, min(w, x1)), max(0, min(w, x2))
                y1, y2 = max(0, min(h, y1)), max(0, min(h, y2))
                if x2 - x1 < 2 or y2 - y1 < 2:
                    continue
                lines.append(f"0 {(x1 + x2) / 2 / w:.6f} {(y1 + y2) / 2 / h:.6f} {(x2 - x1) / w:.6f} {(y2 - y1) / h:.6f}")
            (root / "labels" / split / f"{stem}.txt").write_text("\n".join(lines))  # empty file = nothing here
            n += 1
            with_obj += bool(lines)
    return n, with_obj


def frame_accuracy(weights, root, conf):
    """Per held-back frame: did the model say 'object' when there was one, and nothing when not?"""
    from ultralytics import YOLO
    model = YOLO(str(weights))  # a fresh copy: reusing one after .val() fails in PyTorch 2.15
    tp = fp = fn = tn = 0
    for img in sorted((root / "images" / "val").glob("*.jpg")):
        truth = bool((root / "labels" / "val" / f"{img.stem}.txt").read_text().strip())
        found = len(model.predict(str(img), conf=conf, verbose=False)[0].boxes) > 0
        tp += truth and found
        fn += truth and not found
        fp += found and not truth
        tn += not truth and not found
    return tp, fn, fp, tn


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model-name", default="OpenVolleyball", help="as on the Record card")
    ap.add_argument("--cycle", type=int, help="only this cycle (default: all cycles of the model)")
    ap.add_argument("--test-clips", type=int, default=2, help="whole recordings held back for testing")
    ap.add_argument("--imgsz", type=int, default=192, help="model input size, a multiple of 32 (the V2 needs square)")
    ap.add_argument("--width", choices=("full", "half", "quarter"), default="full",
                    help="model width: full = YOLOv8n (pretrained); half/quarter = slimmer, faster on the V2, but\n"
                         "trained from scratch (no pretrained weights exist at those widths)")
    ap.add_argument("--epochs", type=int, help="default 80 for full, 250 for half/quarter (from scratch)")
    ap.add_argument("--device", default="mps", help="mps (Mac GPU) or cpu")
    ap.add_argument("--conf", type=float, default=0.5, help="score threshold for the per-frame check")
    ap.add_argument("--from-run", help="skip training: score, export and compile this train/work/<run> again")
    args = ap.parse_args()
    if args.imgsz % 32:
        sys.exit("--imgsz must be a multiple of 32")

    from ultralytics import YOLO  # slow import: after argument errors

    if args.from_run:
        run = Path(args.from_run).resolve()
        stamp = run.name.rsplit("-", 2)[-2] + "-" + run.name.rsplit("-", 1)[-1]
        data = run / "dataset"
        target = next(l.split(":", 1)[1].strip() for l in (data / "data.yaml").read_text().splitlines() if l.strip().startswith("0:"))
        n_train = len(list((data / "images" / "train").glob("*.jpg")))
        n_test = len(list((data / "images" / "val").glob("*.jpg")))
        test = [{"id": n} for n in sorted({p.stem.rsplit("_", 1)[0] for p in (data / "images" / "val").glob("*.jpg")})]
    else:
        run, data, target, n_train, n_test, test, stamp = train_model(args, YOLO)
    report = evaluate_and_export(args, run, data, target, n_train, n_test, test, YOLO)
    print(f"\nModel: {report['model_abs']} ({report['size_kb']} KB). Flash it to the V2 (its USB-C plugged in):\n"
          f"  .venv/bin/python v2_firmware/flash.py /dev/cu.usbmodemXXXX --model {report['model']}")


def slim_yaml(run, width):
    """YOLOv8 at a fraction of YOLOv8n's width (n = 0.25 of the base channels): fewer weights for the V2's
    flash to stream each frame, and less compute."""
    import ultralytics
    src = Path(ultralytics.__file__).parent / "cfg" / "models" / "v8" / "yolov8.yaml"
    body = src.read_text().split("backbone:", 1)[1]
    multiple = {"half": 0.125, "quarter": 0.0625}[width]
    run.mkdir(parents=True, exist_ok=True)
    out = run / f"yolov8-{width}.yaml"
    out.write_text(f"nc: 1\ndepth_multiple: 0.33\nwidth_multiple: {multiple}\nbackbone:{body}")
    return out


def train_model(args, YOLO):
    clips = clips_for(args.model_name, args.cycle)
    if len(clips) <= args.test_clips:
        sys.exit(f"Only {len(clips)} recordings for {args.model_name}: need more than --test-clips {args.test_clips}")
    # Test set: the recordings you reviewed most (their labels are the most trustworthy).
    ranked = sorted(clips, key=lambda c: (c["reviewed"] / max(1, c["frames"]), c["name"]), reverse=True)
    test, train = ranked[:args.test_clips], ranked[args.test_clips:]
    target = labels.load(DATASET_DIR, clips[0]["id"])["target"] or "object"

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run = HERE / "work" / f"{args.model_name}-{stamp}" if args.width == "full" else HERE / "work" / f"{args.model_name}-{args.width}-{stamp}"
    data = run / "dataset"
    n_train, obj_train = write_split(train, "train", data)
    n_test, obj_test = write_split(test, "val", data)
    (data / "data.yaml").write_text(f"path: {data}\ntrain: images/train\nval: images/val\nnames:\n  0: {target}\n")
    print(f"Target: {target}. Train: {len(train)} recordings, {n_train} frames ({obj_train} with a {target}). "
          f"Test: {', '.join(c['name'] for c in test)}: {n_test} frames ({obj_test} with a {target}).")

    if args.width == "full":
        model = YOLO("yolov8n.pt")  # pretrained on COCO, fine-tuned here
    else:
        model = YOLO(str(slim_yaml(run, args.width)))  # same design, fewer channels, random start
    epochs = args.epochs or (80 if args.width == "full" else 250)
    model.train(data=str(data / "data.yaml"), imgsz=args.imgsz, epochs=epochs, patience=25 if args.width == "full" else 60, batch=16,
                device=args.device, workers=2, project=str(run), name="yolo", exist_ok=True, plots=False,
                verbose=False)
    return run, data, target, n_train, n_test, test, stamp


def evaluate_and_export(args, run, data, target, n_train, n_test, test, YOLO):
    best = run / "yolo" / "weights" / "best.pt"
    model = YOLO(str(best))
    m = model.val(data=str(data / "data.yaml"), imgsz=args.imgsz, device=args.device, verbose=False, plots=False)
    tp, fn, fp, tn = frame_accuracy(best, data, args.conf)
    report = {"target": target, "imgsz": args.imgsz, "train_frames": n_train, "test_frames": n_test,
              "test_recordings": [c["id"] for c in test],
              "mAP50": round(float(m.box.map50), 3), "precision": round(float(m.box.mp), 3),
              "recall": round(float(m.box.mr), 3),
              "frames": {"found": tp, "missed": fn, "false_alarm": fp, "correctly_empty": tn}}
    print(f"\nOn the held-back recordings: found the {target} in {tp} of {tp + fn} frames that have one, "
          f"false alarms in {fp} of {fp + tn} empty frames (score > {args.conf}). mAP50 {report['mAP50']}.")

    print("\nExporting to 8-bit TFLite (calibrating on the training frames)...")
    YOLO(str(best)).export(format="tflite", int8=True, imgsz=args.imgsz, data=str(data / "data.yaml"), nms=False)
    saved = run / "yolo" / "weights" / "best_saved_model"
    tfl = next(iter(sorted(saved.glob("*full_integer_quant.tflite"))), None)
    if tfl is None:
        sys.exit(f"No full-integer TFLite in {saved}: the V2 needs int8 input and output")
    import tensorflow as tf
    it = tf.lite.Interpreter(model_path=str(tfl))
    i, o = it.get_input_details()[0], it.get_output_details()[0]
    print(f"TFLite: input {list(i['shape'])} {i['dtype'].__name__}, output {list(o['shape'])} {o['dtype'].__name__}")
    if i["dtype"] != np.int8 or o["dtype"] != np.int8 or list(o["shape"])[:2] != [1, 5]:
        sys.exit("Unexpected TFLite format for the V2's YOLOv8 detector (wants int8 in/out, output [1, 5, N])")

    print("\nCompiling for the V2's Ethos-U55 with Vela...")
    vela_out = run / "vela"
    subprocess.run([str(VELA), "--accelerator-config", "ethos-u55-64", "--optimise", "Performance",
                    "--output-dir", str(vela_out), str(tfl)], check=True)
    compiled = next(vela_out.glob("*_vela.tflite"))
    models = HERE / "models"
    models.mkdir(exist_ok=True)
    final = models / f"{run.name}-{args.imgsz}_vela.tflite"
    shutil.copy(compiled, final)
    report["model"] = str(final.relative_to(HERE.parent))
    report["size_kb"] = round(final.stat().st_size / 1024)
    (models / f"{final.stem}.json").write_text(json.dumps(report, indent=1))
    report["model_abs"] = str(final)
    return report


if __name__ == "__main__":
    main()
