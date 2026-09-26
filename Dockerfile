FROM python:3.12-slim

# Pin the install toolchain + Python deps to the versions verified in production
# (2026-06) so a rebuild can't silently pull a behavior-changing LiteLLM. The base
# image stays a tag (python:3.12-slim) on purpose — it tracks Debian security
# patches; for a fully frozen artifact, deploy the prebuilt ECR image (see
# docs/hosted_deployment.md) instead of rebuilding on the box.
#
# The LiteLLM version is single-sourced from .litellm-version (refs #126) so a
# bump updates Docker, Makefile, and start_proxy.sh together.
COPY .litellm-version .
RUN LITELLM_VERSION="$(cat .litellm-version)" && \
    pip install --no-cache-dir "uv==0.11.21" && \
    uv pip install --system "litellm[proxy]==${LITELLM_VERSION}" "prisma==0.15.0"

WORKDIR /app

COPY litellm_config.yaml .
COPY litellm_logger.py .
COPY health_version.py .
COPY scripts/db_mode_guard.py scripts/db_mode_guard.py
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

EXPOSE 4000

# Build-time version info baked into the image (set during docker build).
ARG BUILD_SHA=unknown
ARG BUILD_TIMESTAMP=unknown
ENV BUILD_SHA=${BUILD_SHA}
ENV BUILD_TIMESTAMP=${BUILD_TIMESTAMP}

# /app on the import path so litellm can load the litellm_logger callback module.
ENV PYTHONPATH=/app
ENV UV_NATIVE_TLS=true
ENV LITELLM_LOCAL_MODEL_COST_MAP=true

# Entrypoint enforces the DB-mode boundary (refs #169): DATABASE_URL set
# requires a reachable Postgres, else fail fast at container start instead of
# serving 400 "No connected db" on every request.
ENTRYPOINT ["/app/entrypoint.sh"]
CMD []
