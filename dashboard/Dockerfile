# syntax=docker/dockerfile:1
#
# Multi-stage build -- the runtime image ships only the finished virtualenv
# and the app code, so pip caches, build tooling and wheels never land in the
# final layer. Smaller image, smaller attack surface.

# ---- build stage: resolve and install dependencies once ----------------------
FROM python:3.12-alpine AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt \
    # Drop the package manager from the artifact we ship: only the
    # installed libraries are needed at runtime.
    && /opt/venv/bin/pip uninstall -y pip setuptools wheel \
    && rm -rf /opt/venv/lib/python*/site-packages/*.dist-info/RECORD

# ---- runtime stage: venv + app only ------------------------------------------
FROM python:3.12-alpine AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    DASHBOARD_PORT=9983

WORKDIR /app

RUN addgroup -S app && adduser -S -G app app

COPY --from=builder /opt/venv /opt/venv

COPY app.py ./
COPY templates ./templates
COPY static ./static

EXPOSE 9983

USER app

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9983/healthz', timeout=2).status == 200 else 1)"

# Exec form: gunicorn is PID 1 and receives SIGTERM directly for graceful
# shutdown. Override worker count with GUNICORN_CMD_ARGS if needed.
CMD ["gunicorn", "--bind", "0.0.0.0:9983", "--workers", "2", "--threads", "4", \
     "--access-logfile", "-", "--error-logfile", "-", "--graceful-timeout", "20", "app:app"]
