# TEMU Image Factory Dockerfile
# Multi-stage build to keep final image small and avoid leaking build tools.

# ---------------- build stage ----------------
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -e .

# ---------------- runtime stage ----------------
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOME=/app \
    CONFIG_DIR=/app/config \
    CONFIG_DEFAULTS_DIR=/app/config-defaults \
    TEMPLATE_DIR=/app/templates \
    TEMPLATE_DEFAULTS_DIR=/app/templates-defaults \
    DATA_DIR=/app/data

WORKDIR $APP_HOME

# Install runtime dependencies: DejaVu fonts for Pillow text rendering, gosu for privilege drop
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-dejavu-core \
    libfreetype6 \
    libjpeg62-turbo \
    libpng16-16 \
    libwebp7 \
    gosu \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user (numeric UID/GID will be remapped by entrypoint if PUID/PGID set)
RUN groupadd -g 1000 tif && \
    useradd -u 1000 -g tif -d /app -s /bin/bash tif

# Copy installed Python packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY src ./src
COPY pyproject.toml ./

# Bundled defaults live OUTSIDE the runtime CONFIG_DIR/TEMPLATE_DIR so that
# host volume mounts never hide them; the entrypoint seeds missing files only.
COPY config ./config-defaults
COPY templates ./templates-defaults
COPY input ./input

# Copy entrypoint
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Create persistent/runtime directories and set base ownership (no chmod 777)
RUN mkdir -p /app/config /app/templates /app/data \
    /app/input /app/output /app/cache /app/logs && \
    chown -R tif:tif /app

# Entrypoint starts as root to remap PUID/PGID, seed defaults and fix
# ownership, then drops to the non-root tif user via gosu before exec'ing CMD.
EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "src.web.app:app", "--host", "0.0.0.0", "--port", "8000"]
