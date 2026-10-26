# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm AS base

LABEL org.opencontainers.image.title="Local ADK Personal Agent" \
      org.opencontainers.image.description="Google ADK Personal Agent with Search Grounding, Deep Research, Workspace, Google Chat Pub/Sub, and Headless Chrome" \
      org.opencontainers.image.licenses="Apache-2.0"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    GOOGLE_GENAI_USE_VERTEXAI=FALSE \
    DATA_DIR=/data \
    PORT=8080

# 1. Install system dependencies, tini (PID 1 zombie reaper), and Node.js 22 LTS for @playwright/mcp
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gnupg \
    tini \
    fonts-liberation \
    fonts-noto-color-emoji \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libcairo2 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libglib2.0-0 \
    libnspr4 \
    libnss3 \
    libpango-1.0-0 \
    libvulkan1 \
    libx11-6 \
    libxcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    xdg-utils \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
       | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
       > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# 2. Pre-install @playwright/mcp globally so MCP tool invocations start in <200ms without runtime downloads
RUN npm install -g @playwright/mcp@latest && npm cache clean --force

WORKDIR /app

# 3. Install Python dependencies and Playwright Chromium binary into /ms-playwright
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip \
    && pip install -r /app/requirements.txt \
    && playwright install --with-deps chromium \
    && chmod -R 755 /ms-playwright

# 4. Create non-root runtime user (UID/GID 1000 matching typical personal Linux box user)
RUN groupadd -g 1000 adkuser \
    && useradd -m -u 1000 -g 1000 -s /bin/bash adkuser \
    && mkdir -p /data/chrome-profile /data/screenshots \
    && chown -R adkuser:adkuser /data /app

# 5. Copy application code & entrypoint (no secrets — enforced by .dockerignore)
COPY --chown=adkuser:adkuser src/ /app/src/
COPY --chown=adkuser:adkuser entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

USER adkuser
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/api/health || exit 1

# Use tini as PID 1 to reap orphaned Chromium renderer/zygote processes
ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
