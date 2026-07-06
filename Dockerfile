# syntax=docker/dockerfile:1.7

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_SYSTEM_PIP=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
      tini \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app
RUN useradd -m -u 10001 -s /usr/sbin/nologin appuser

FROM base AS runtime

COPY pyproject.toml README.md ./
COPY src ./src
COPY config.example.yaml ./

RUN uv pip install --system .

USER appuser
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/status', timeout=2).read()"

ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["lightnow-proxy"]
