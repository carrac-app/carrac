FROM python:3.11-slim

WORKDIR /app

# pip runs as root while building the image, which is expected there.
ENV PIP_ROOT_USER_ACTION=ignore \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Create non-root user for security (CWE-250)
RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid 1000 --no-create-home appuser

# Install system dependencies (Playwright/Chromium + libpq for PostgreSQL).
# procps and net-tools give the container ps and netstat for debugging.
# Debian package versions are not pinned: the slim base image is the pin, and
# the point-release version strings disappear from the mirrors within weeks.
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 libasound2 libatspi2.0-0 \
    libpq-dev \
    procps net-tools \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies from the lockfile: exact pins, so a rebuild
# reproduces the same package set. requirements.txt is copied too so the image
# records the declared constraints (tests/test_requirements_lock.py checks they agree).
COPY requirements.txt requirements.lock ./
RUN pip install -r requirements.lock

# Install Playwright Chromium to a shared location
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
RUN playwright install chromium && chmod -R 755 /opt/pw-browsers

# The curl chart endpoint needs the carrac-tui renderer; fetch the pinned,
# checksummed release for this architecture. A bin/carrac-render in the build
# context (a local build of carrac-tui) replaces it at COPY . . below.
ARG TARGETARCH
COPY scripts/fetch-tui-renderer.py /tmp/
RUN python /tmp/fetch-tui-renderer.py "${TARGETARCH:-amd64}" /app/bin/carrac-render \
    && rm /tmp/fetch-tui-renderer.py

# Copy application files
COPY . .

# Create data directory and set ownership
RUN mkdir -p /app/data && chown -R appuser:appuser /app

# Expose port
EXPOSE 5000

# Switch to non-root user
USER appuser

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import requests; requests.get('http://localhost:5000/health').raise_for_status()"]

# Run gunicorn with UvicornWorker, binding 0.0.0.0 for Docker networking. Flask
# is served through asgiref's WsgiToAsgi wrapper; FastAPI handles the migrated
# routes. Four workers; WORKER_MODE=true keeps gunicorn workers from starting
# the background compute threads, which live in worker.py.
# --max-requests/--max-requests-jitter recycle workers to bound memory.
# --timeout 60: no web endpoint should run longer (the morning engine runs in worker.py).
# --graceful-timeout 30 lets in-flight requests drain on restart; compose pairs it with
# stop_grace_period: 60s and AUTOHEAL_DEFAULT_STOP_TIMEOUT=60 so the whole stop path agrees.
# The anyio thread-pool cap is set in fastapi_app/main.py's lifespan.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "4", "--worker-class", "uvicorn.workers.UvicornWorker", "--timeout", "60", "--graceful-timeout", "30", "--max-requests", "1000", "--max-requests-jitter", "50", "--access-logfile", "-", "carrac.asgi:asgi_app"]
