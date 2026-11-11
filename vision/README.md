# jl-vision CPU PoC

CPU-only open-vocabulary detection for jl. It runs `IDEA-Research/grounding-dino-tiny`
with PyTorch CPU and exposes:

```text
GET  /healthz
POST /detect   {path, labels[], threshold?, max_detections?}
```

`path` is a file under `/data` inside the container. The BFF maps
`data/blobs/...` to that path.

## Why this model

- Apache-2.0;
- text-prompted open vocabulary (`cardboard box`, `worker`, `safety helmet`, ...);
- no training needed for the first pass;
- server has no NVIDIA GPU, so a tiny CPU model is the practical choice.

YOLO-World is faster but its upstream is GPL-3.0 and Ultralytics is AGPL-3.0;
keep that in mind for a commercial closed-source product.

## Hardware

```text
2 x Intel Xeon Platinum 8268, 96 threads
247 GiB RAM
No NVIDIA GPU
```

The container is limited to 16 CPUs / 24 GiB by the start script.

## Measured on this host

Grounding DINO Tiny, 1080x1889 photo, labels `cardboard box`, `carton`, `box`:

```text
model load: 5.5 s (first request only)
inference:  ~3.5-4.0 s
```

These are PoC numbers, not a production SLA.

## Start

```bash
# build once
docker build -t jl-vision:local vision

# model cache download once (158s on this host)
docker run --rm --network host -v jl-vision-cache:/root/.cache/huggingface \
  -e HF_ENDPOINT=https://hf-mirror.com -e HF_HUB_DISABLE_XET=1 \
  jl-vision:local python -c \
  'from huggingface_hub import snapshot_download; snapshot_download("IDEA-Research/grounding-dino-tiny")'

# start
bash scripts/start-vision.sh
```

## BFF integration

```http
POST /api/v1/vision/detect
{
  "projectId": "clean-demo-01",
  "photoId": 46,
  "labels": ["cardboard box", "carton"],
  "threshold": 0.2,
  "maxDetections": 10
}
```

The tool `annotate_photo` accepts `detectLabels` + `maxBoxes`; it calls this
endpoint and passes the returned pixel boxes to the existing annotation pipeline.
The language model should no longer estimate coordinates.
