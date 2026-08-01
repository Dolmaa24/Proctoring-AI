# Gateway image.
#
# Only the Python side ships here. The Electron client is a desktop app
# that runs on the candidate's machine and has no place in a server image,
# and the proctor console is static files the gateway already serves.

FROM python:3.13-slim AS base

# curl is for the healthcheck in docker-compose.yml; without it the
# gateway's dependents start before it can actually answer a request.
RUN apt-get update \
    && apt-get install --no-install-recommends -y curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependency layer first so that editing source does not reinstall the
# world on every rebuild.
COPY pyproject.toml README.md ./
COPY python/ ./python/
RUN pip install --no-cache-dir .

COPY policies/ ./policies/
COPY apps/console/ ./apps/console/

# Non-root. The gateway holds candidates' biometric-derived evidence; a
# container escape from a root process is a materially worse day than one
# from an unprivileged process.
RUN useradd --create-home --uid 10001 proctor \
    && chown -R proctor:proctor /app
USER proctor

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PROCTOR_POLICY_PATH=/app/policies/default.yaml

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=5 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "proctor_gateway.app:app", "--host", "0.0.0.0", "--port", "8000"]
