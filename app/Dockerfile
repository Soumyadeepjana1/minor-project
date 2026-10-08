# syntax=docker/dockerfile:1
#
# Multi-stage build: dependencies are resolved in a throwaway builder stage and
# only the finished virtualenv + app code are copied into the runtime image, so
# pip caches and build tooling never reach the shipped layers.

# ---- build stage -------------------------------------------------------------
FROM python:3.12-alpine AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt \
    # Only the installed libraries are needed at runtime.
    && /opt/venv/bin/pip uninstall -y pip setuptools wheel \
    && rm -rf /opt/venv/lib/python*/site-packages/*.dist-info/RECORD

# ---- runtime stage -----------------------------------------------------------
FROM python:3.12-alpine AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=5000

WORKDIR /app

# Run as an unprivileged user.
RUN addgroup -S app && adduser -S -G app app

COPY --from=builder /opt/venv /opt/venv

COPY main.py .

EXPOSE 5000

USER app

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=2).status == 200 else 1)"

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--threads", "2", "--access-logfile", "-", "main:app"]
