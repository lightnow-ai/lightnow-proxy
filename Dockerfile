# syntax=docker/dockerfile:1.28@sha256:bb22d9815c728170f72750f4e5b0d672e06176142e1d602c7e66c050100b7e5b

FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS base

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
