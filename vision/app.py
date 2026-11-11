"""jl-vision：CPU 版开放词汇目标检测 PoC。

先验证 Grounding DINO Tiny 在本机 Xeon 上能否给出真实 bbox。
接口只接受本地图片路径（PoC 由 BFF 调用），不对外暴露公网。
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import List, Optional

import torch
from fastapi import FastAPI, HTTPException
from PIL import Image
from pydantic import BaseModel, Field
from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor

MODEL_ID = os.environ.get("MODEL_ID", "IDEA-Research/grounding-dino-tiny")
DATA_ROOT = Path(os.environ.get("DATA_ROOT", "/data")).resolve()
TORCH_THREADS = int(os.environ.get("TORCH_THREADS", "16"))
THRESHOLD_DEFAULT = float(os.environ.get("BOX_THRESHOLD", "0.25"))

torch.set_num_threads(TORCH_THREADS)

app = FastAPI(title="jl-vision", version="0.1.0")

_processor = None
_model = None


class DetectRequest(BaseModel):
    path: str = Field(..., description="本地图片路径，必须在 DATA_ROOT 下")
    labels: List[str] = Field(..., min_length=1, description="英文开放词汇标签")
    threshold: Optional[float] = Field(None, ge=0, le=1)
    max_detections: int = Field(20, ge=1, le=100)


class DetectBox(BaseModel):
    x1: float
    y1: float
    x2: float
    y2: float
    score: float
    label: str


class DetectResponse(BaseModel):
    model: str
    imagePath: str
    width: int
    height: int
    elapsedMs: int
    boxes: List[DetectBox]


def _load():
    global _processor, _model
    if _model is not None:
        return _processor, _model
    started = time.time()
    _processor = AutoProcessor.from_pretrained(MODEL_ID)
    _model = AutoModelForZeroShotObjectDetection.from_pretrained(MODEL_ID)
    _model.eval()
    print("[vision] loaded %s in %.2fs" % (MODEL_ID, time.time() - started), flush=True)
    return _processor, _model


def _allowed(path: Path) -> bool:
    try:
        path.resolve().relative_to(DATA_ROOT)
        return True
    except ValueError:
        return False


@app.get("/healthz")
def healthz():
    return {"ok": True, "model": MODEL_ID, "loaded": _model is not None,
            "threads": TORCH_THREADS}


@app.post("/detect", response_model=DetectResponse)
def detect(req: DetectRequest):
    path = Path(req.path)
    if not _allowed(path) or not path.is_file():
        raise HTTPException(status_code=400, detail="图片路径不存在或不在 DATA_ROOT 下")

    labels = [str(x).strip() for x in req.labels if str(x).strip()]
    if not labels:
        raise HTTPException(status_code=400, detail="labels 不能为空")

    try:
        image = Image.open(path).convert("RGB")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="无法读取图片: %s" % exc)

    processor, model = _load()
    threshold = THRESHOLD_DEFAULT if req.threshold is None else float(req.threshold)
    text = ". ".join(labels) + "."
    inputs = processor(images=image, text=text, return_tensors="pt")

    started = time.time()
    with torch.inference_mode():
        outputs = model(**inputs)
    elapsed_ms = int((time.time() - started) * 1000)

    results = processor.post_process_grounded_object_detection(
        outputs,
        inputs.input_ids,
        box_threshold=threshold,
        text_threshold=threshold,
        target_sizes=[image.size[::-1]],
    )[0]

    boxes = []
    for score, box, label in zip(results["scores"], results["boxes"], results["labels"]):
        x1, y1, x2, y2 = [float(v) for v in box.tolist()]
        x1 = max(0.0, min(float(image.width), x1))
        y1 = max(0.0, min(float(image.height), y1))
        x2 = max(0.0, min(float(image.width), x2))
        y2 = max(0.0, min(float(image.height), y2))
        if x2 - x1 < 4 or y2 - y1 < 4:
            continue
        boxes.append(DetectBox(x1=x1, y1=y1, x2=x2, y2=y2,
                               score=float(score), label=str(label)))
        if len(boxes) >= req.max_detections:
            break

    return DetectResponse(model=MODEL_ID, imagePath=str(path), width=image.width,
                          height=image.height, elapsedMs=elapsed_ms, boxes=boxes)
