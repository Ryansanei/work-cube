FROM python:3.12-slim

# libgl1/libglib2.0-0 — opencv-python (a Docling dependency) needs libGL.so.1
# at import time even headless; missing it is a common Docker footgun for
# this dependency chain specifically, not a general Python requirement.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 postgresql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# torch/torchvision first, from PyTorch's CPU-only wheel index — the default
# PyPI wheel for linux pulls several GB of CUDA/GPU dependencies that this
# container has no use for (Docling's models run on CPU here) and that
# don't fit in typical build disk space. Installing the CPU build first
# means the rest of requirements.txt's resolve finds it already satisfied.
RUN pip install --no-cache-dir --timeout 120 --retries 5 \
    torch==2.13.0 torchvision==0.28.0 --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir --timeout 120 --retries 5 -r requirements.txt

COPY . .

EXPOSE 8010
ENTRYPOINT ["./docker-entrypoint.sh"]
