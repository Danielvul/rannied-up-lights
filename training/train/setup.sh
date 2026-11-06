#!/bin/bash
# One-off: a separate Python environment for training (Ultralytics YOLO, TensorFlow for the 8-bit
# TFLite export, Arm's Vela compiler for the V2's Ethos-U55). Separate from .venv so the viewer's
# packages are never disturbed. About 2-3 GB.
#
#   train/setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."
PY=/opt/homebrew/bin/python3.12
[ -x .venv-train/bin/python ] || "$PY" -m venv .venv-train
.venv-train/bin/pip install --upgrade pip
# ultralytics: training and export. tensorflow + onnx2tf etc: the int8 TFLite export.
# ethos-u-vela 4.1: matches the Ethos-U driver in the V2 firmware (core driver 24.11).
.venv-train/bin/pip install "ultralytics>=8.3,<8.4" "tensorflow>=2.16,<2.20" "onnx>=1.15" "onnx2tf>=1.26" \
  "onnxslim" "onnxruntime" "sng4onnx" "onnx_graphsurgeon" "tf_keras" "ai-edge-litert" "ethos-u-vela==4.1.0"
.venv-train/bin/python -c "import ultralytics, tensorflow, ethosu.vela; print('ready: ultralytics', ultralytics.__version__, '| tensorflow', tensorflow.__version__)"
