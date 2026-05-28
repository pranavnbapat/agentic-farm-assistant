FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /app

# System deps: tini + build toolchain for llama_cpp_python + OpenBLAS runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl tini \
      build-essential cmake git \
      libopenblas0 \
      ffmpeg \
  && rm -rf /var/lib/apt/lists/*

# Python deps first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY . .

# Download Piper voice models into /app/models
RUN mkdir -p /app/models && \
    curl -L "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_GB/alan/medium/en_GB-alan-medium.onnx?download=true" \
      -o /app/models/en_GB-alan-medium.onnx && \
    curl -L "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_GB/alan/medium/en_GB-alan-medium.onnx.json?download=true" \
      -o /app/models/en_GB-alan-medium.onnx.json && \
    curl -L "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_GB/alba/medium/en_GB-alba-medium.onnx?download=true" \
      -o /app/models/en_GB-alba-medium.onnx && \
    curl -L "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_GB/alba/medium/en_GB-alba-medium.onnx.json?download=true" \
      -o /app/models/en_GB-alba-medium.onnx.json


# Make sure Python can import "app.*"
ENV PYTHONPATH=/app

EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000", "--proxy-headers","--forwarded-allow-ips","*", "--timeout-keep-alive","120"]
