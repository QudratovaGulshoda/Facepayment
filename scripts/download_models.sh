#!/usr/bin/env bash
# Liveness modellarini yuklab oladi. InsightFace buffalo_l birinchi ishga tushishda o'zi yuklanadi.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p models
curl -fL -o models/face_landmarker.task \
  https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task
curl -fL -o models/anti_spoof.onnx \
  https://github.com/yakhyo/face-anti-spoofing/releases/download/weights/MiniFASNetV2.onnx
echo "Tayyor: models/"
