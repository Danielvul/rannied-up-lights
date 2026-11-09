#!/usr/bin/env python3
"""
Train a centre-point detector (FOMO) from your reviewed recordings and compile it for the V2.

    .venv-train/bin/python train/train_fomo.py --model-name OpenVolleyball

Instead of boxes, FOMO marks the grid cells where an object's centre is: a 160x160 input gives a
20x20 grid, so each cell is 12 pixels of the 240x240 frame. The centres come straight from your
labels (the middle of each box), so nothing needs labelling again. It's built on MobileNetV2 at
35% width, pretrained on ImageNet, cut after its first 1/8-resolution stage: tiny and fast on the
V2, but it keeps the pretrained head start the slim YOLO models lose.

Same split as train.py: the recordings you reviewed most are held back as the test set.
Output: train/models/<name>-fomo-<date>-<size>_vela.tflite, plus the command to flash it.
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
STRIDE = 8  # MobileNetV2 cut after block 6: one output cell per 8x8 input pixels
OBJECT_WEIGHT = 100.0  # an object cell is rare next to ~400 background cells: weigh it up (as Edge Impulse does)


def load(clips, size):
    """Images resized to size x size (0..1) and their centre maps: 1 where a box centre falls."""
    grid = size // STRIDE
    xs, ys, info = [], [], []
    for c in clips:
        clip = labels.load(DATASET_DIR, c["id"])
        for f in clip["frames"]:
            boxes, _ = labels.final(f)
            if boxes is None:
                continue  # left out: unsure, failed, or excluded by you
            img = cv2.imread(str(DATASET_DIR / c["id"] / f["file"]))
            if img is None:
                continue
            h, w = img.shape[:2]
            y = np.zeros((grid, grid), np.int64)
            centres = []
            for x1, y1, x2, y2 in boxes:
                cx, cy = (x1 + x2) / 2 / w, (y1 + y2) / 2 / h
                y[min(grid - 1, int(cy * grid)), min(grid - 1, int(cx * grid))] = 1
                centres.append((cx * w, cy * h))
            rgb = cv2.cvtColor(cv2.resize(img, (size, size), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB)
            xs.append(rgb.astype(np.float32) / 255.0)
            ys.append(y)
            info.append({"clip": c["id"], "file": f["file"], "centres": centres, "frame": (w, h)})
    return np.stack(xs), np.stack(ys), info


def build(size, tf):
    base = tf.keras.applications.MobileNetV2(input_shape=(size, size, 3), alpha=0.35, include_top=False,
                                             weights="imagenet")
    cut = base.get_layer("block_6_expand_relu").output  # stride 8
    trunk = tf.keras.Model(base.input, cut)
    inp = tf.keras.Input((size, size, 3))
    x = tf.keras.layers.Rescaling(2.0, offset=-1.0)(inp)  # 0..1 in, -1..1 as MobileNetV2 was trained
    x = trunk(x)
    x = tf.keras.layers.Conv2D(32, 1, activation="relu")(x)
    out = tf.keras.layers.Conv2D(2, 1, activation="softmax")(x)  # background, object
    return tf.keras.Model(inp, out)


def augment(x, y, rng):
    """Flips (applied to the centre map too) and light/colour jitter, new each epoch."""
    x, y = x.copy(), y.copy()
    for i in range(len(x)):
        if rng.random() < 0.5:
            x[i], y[i] = x[i][:, ::-1], y[i][:, ::-1]
        if rng.random() < 0.5:
            x[i], y[i] = x[i][::-1], y[i][::-1]
        gain = rng.uniform(0.6, 1.4)
        bias = rng.uniform(-0.12, 0.12)
        tint = rng.uniform(0.9, 1.1, 3)
        x[i] = np.clip(x[i] * gain * tint + bias, 0, 1)
    return x, y


def score(pred, info, threshold, size):
    """Per frame: found (a hot cell within 1.5 cells of a true centre), missed, false alarm."""
    found = missed = false_alarm = empty_ok = 0
    errors = []
    for p, inf in zip(pred, info):
        w, h = inf["frame"]
        hot = np.argwhere(p[..., 1] > threshold)  # (row, col)
        cell = w / (size // STRIDE)
        points = [((c + 0.5) * cell, (r + 0.5) * w / (size // STRIDE) * h / w) for r, c in hot]
        if inf["centres"]:
            dists = [min(np.hypot(px - cx, py - cy) for px, py in points) for cx, cy in inf["centres"]] if points else []
            if dists and min(dists) <= 1.5 * cell:
                found += 1
                errors.append(min(dists))
            else:
                missed += 1
        else:
            false_alarm += bool(points)
            empty_ok += not points
    return {"found": found, "missed": missed, "false_alarm": false_alarm, "correctly_empty": empty_ok,
            "centre_error_px": round(float(np.mean(errors)), 1) if errors else None}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model-name", default="OpenVolleyball", help="as on the Record card; comma-separate to combine")
    ap.add_argument("--cycle", type=int, help="only this cycle (default: all cycles of the model)")
    ap.add_argument("--test-clips", type=int, default=2, help="whole recordings held back for testing")
    ap.add_argument("--test", help="hold back exactly these recordings, by the end of their names, comma-separated")
    ap.add_argument("--imgsz", type=int, default=160, help="input size, a multiple of 8 (160 -> 20x20 grid)")
    ap.add_argument("--epochs", type=int, default=80)
    ap.add_argument("--min-passes", type=int, default=25, help="don't pick or stop before this pass: early test scores are noisy")
    ap.add_argument("--threshold", type=float, default=0.5, help="cell score counted as a centre")
    args = ap.parse_args()
    if args.imgsz % STRIDE:
        sys.exit(f"--imgsz must be a multiple of {STRIDE}")

    import tensorflow as tf  # slow import: after argument errors

    names = args.model_name.split(",")  # several recording names train one combined model
    prefixes = tuple(f"{n}-cycle-" for n in names)
    clips = [c for c in labels.summary(DATASET_DIR) if c["group"].startswith(prefixes)
             and (args.cycle is None or c["group"] in [f"{p}{args.cycle:02d}" for p in prefixes])]
    if len(clips) <= args.test_clips:
        sys.exit(f"Only {len(clips)} recordings for {args.model_name}: need more than --test-clips {args.test_clips}")
    ranked = sorted(clips, key=lambda c: (c["reviewed"] / max(1, c["frames"]), c["name"]), reverse=True)
    test, train = ranked[:args.test_clips], ranked[args.test_clips:]
    if args.test:  # e.g. to keep a mix of drops, hands and empty clips in the test
        ends = tuple(args.test.split(","))
        test, train = [c for c in clips if c["name"].endswith(ends)], [c for c in clips if not c["name"].endswith(ends)]
        if len(test) != len(ends):
            sys.exit(f"--test matched {len(test)} of {len(ends)} recordings")
    target = labels.load(DATASET_DIR, clips[0]["id"])["target"] or "object"

    xtr, ytr, _ = load(train, args.imgsz)
    xte, yte, info_te = load(test, args.imgsz)
    print(f"Target: {target}. Train: {len(train)} recordings, {len(xtr)} frames ({int((ytr.sum((1, 2)) > 0).sum())} "
          f"with a {target}). Test: {', '.join(c['name'] for c in test)}: {len(xte)} frames "
          f"({int((yte.sum((1, 2)) > 0).sum())} with a {target}).")

    model = build(args.imgsz, tf)
    weights = tf.constant([1.0, OBJECT_WEIGHT])

    def loss(y_true, y_pred):
        y_true = tf.cast(y_true, tf.int32)
        ce = tf.keras.losses.sparse_categorical_crossentropy(y_true, y_pred)
        return tf.reduce_mean(ce * tf.gather(weights, y_true))

    model.compile(optimizer=tf.keras.optimizers.Adam(1e-3), loss=loss)
    rng = np.random.default_rng(0)
    best, best_loss, patience = None, float("inf"), 0
    for epoch in range(args.epochs):
        xa, ya = augment(xtr, ytr, rng)
        hist = model.fit(xa, ya, batch_size=16, epochs=1, verbose=0, shuffle=True)
        val = float(model.evaluate(xte, yte, verbose=0))
        if epoch + 1 < args.min_passes:
            pass  # too early to judge: the test score jumps around
        elif val < best_loss - 1e-4:
            best, best_loss, patience = model.get_weights(), val, 0
        else:
            patience += 1
        if epoch % 5 == 0 or (patience == 0 and best is not None):
            print(f"pass {epoch + 1}: train loss {hist.history['loss'][-1]:.3f}, test loss {val:.3f}" + ("  (best)" if patience == 0 else ""))
        if patience >= 20:
            print(f"Stopped: no improvement for 20 passes.")
            break
    model.set_weights(best)

    s = score(model.predict(xte, verbose=0), info_te, args.threshold, args.imgsz)
    print(f"\nOn the held-back recordings: found the {target} in {s['found']} of {s['found'] + s['missed']} frames that "
          f"have one (centre within 1.5 cells, average {s['centre_error_px']} px off), false alarms in "
          f"{s['false_alarm']} of {s['false_alarm'] + s['correctly_empty']} empty frames.")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run = HERE / "work" / f"{'+'.join(names)}-fomo-{stamp}"
    run.mkdir(parents=True, exist_ok=True)
    model.save(run / "fomo.keras")

    print("\nExporting to 8-bit TFLite (calibrating on the training frames)...")
    conv = tf.lite.TFLiteConverter.from_keras_model(model)
    conv.optimizations = [tf.lite.Optimize.DEFAULT]
    conv.representative_dataset = lambda: ([x[None]] for x in xtr[:200])
    conv.target_spec.supported_ops = [tf.lite.OpsSet.TFLITE_BUILTINS_INT8]
    conv.inference_input_type = tf.int8
    conv.inference_output_type = tf.int8
    tfl = run / "fomo_int8.tflite"
    tfl.write_bytes(conv.convert())
    it = tf.lite.Interpreter(model_path=str(tfl))
    i, o = it.get_input_details()[0], it.get_output_details()[0]
    print(f"TFLite: input {list(i['shape'])} {i['dtype'].__name__}, output {list(o['shape'])} {o['dtype'].__name__}")

    print("\nCompiling for the V2's Ethos-U55 with Vela...")
    subprocess.run([str(VELA), "--accelerator-config", "ethos-u55-64", "--optimise", "Performance",
                    "--output-dir", str(run / "vela"), str(tfl)], check=True)
    compiled = next((run / "vela").glob("*_vela.tflite"))
    models = HERE / "models"
    models.mkdir(exist_ok=True)
    final = models / f"{run.name}-{args.imgsz}_vela.tflite"
    shutil.copy(compiled, final)
    report = {"kind": "fomo (centre points)", "target": target, "imgsz": args.imgsz,
              "grid": args.imgsz // STRIDE, "train_frames": len(xtr), "test_frames": len(xte),
              "test_recordings": [c["id"] for c in test], "threshold": args.threshold, "frames": s,
              "model": str(final.relative_to(HERE.parent)), "size_kb": round(final.stat().st_size / 1024)}
    (models / f"{final.stem}.json").write_text(json.dumps(report, indent=1))
    print(f"\nModel: {final} ({report['size_kb']} KB). Flash it to the V2 (its USB-C plugged in):\n"
          f"  .venv/bin/python v2_firmware/flash.py /dev/cu.usbmodemXXXX --model {report['model']}")


if __name__ == "__main__":
    main()
