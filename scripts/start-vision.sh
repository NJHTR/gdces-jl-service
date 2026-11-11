#!/usr/bin/env bash
# 启动 CPU 版 jl-vision（Grounding DINO Tiny）。
# 端口固定 8083：8082 被宿主 Java 服务占用。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA="${DATA:-$ROOT/data}"
PORT="${VISION_PORT:-8083}"
MODEL_CACHE="${VISION_CACHE_VOLUME:-jl-vision-cache}"

docker rm -f jl-vision >/dev/null 2>&1 || true

docker run -d --name jl-vision \
  --network host \
  --restart unless-stopped \
  --cpus="${VISION_CPUS:-16}" \
  --memory="${VISION_MEMORY:-24g}" \
  -v "$DATA:/data:ro" \
  -v "$MODEL_CACHE:/root/.cache/huggingface" \
  -e HF_ENDPOINT=https://hf-mirror.com \
  -e HF_HUB_DISABLE_XET=1 \
  -e TORCH_THREADS="${VISION_THREADS:-16}" \
  jl-vision:local \
  uvicorn app:app --host 0.0.0.0 --port "$PORT" --workers 1

echo "jl-vision started on http://127.0.0.1:$PORT"
