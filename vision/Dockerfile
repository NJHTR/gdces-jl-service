FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OMP_NUM_THREADS=16 \
    MKL_NUM_THREADS=16 \
    HF_HOME=/root/.cache/huggingface \
    HF_ENDPOINT=https://hf-mirror.com \
    DATA_ROOT=/data \
    TORCH_THREADS=16 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --no-deps \
      --index-url https://download.pytorch.org/whl/cpu \
      torch torchvision

RUN pip install --no-cache-dir \
      filelock typing-extensions setuptools sympy networkx jinja2 fsspec numpy pillow \
      transformers==4.49.0 \
      fastapi==0.115.8 \
      uvicorn[standard]==0.34.0 \
      python-multipart==0.0.20

WORKDIR /app
COPY app.py /app/app.py

EXPOSE 8082
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8082", "--workers", "1"]
