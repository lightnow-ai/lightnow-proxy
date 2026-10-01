# syntax=docker/dockerfile:1.27@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e

FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea AS base

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

ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["lightnow-proxy"]
