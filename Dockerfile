FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    DATA_PATH=/app/data \
    UPSTREAM_PATH=/app/upstream \
    PORT=8345 \
    VNC_PORT=6345

# System dependencies: Xvfb, VNC, window manager, noVNC, git, curl
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gnupg \
    unzip \
    fonts-liberation \
    procps \
    git \
    xvfb \
    x11vnc \
    fluxbox \
    novnc \
    websockify \
    && rm -rf /var/lib/apt/lists/*

# Microsoft Edge Stable from official repo
RUN curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
    | gpg --dearmor -o /usr/share/keyrings/microsoft.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft.gpg] https://packages.microsoft.com/repos/edge stable main" \
    > /etc/apt/sources.list.d/microsoft-edge.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends microsoft-edge-stable \
    && rm -rf /var/lib/apt/lists/*

# Edge WebDriver pinned to installed Edge version
RUN EDGE_VERSION="$(microsoft-edge --version | awk '{print $3}')" \
    && curl -fsSL -o /tmp/edgedriver.zip \
    "https://msedgedriver.microsoft.com/${EDGE_VERSION}/edgedriver_linux64.zip" \
    && unzip -j /tmp/edgedriver.zip msedgedriver -d /usr/local/bin \
    && chmod +x /usr/local/bin/msedgedriver \
    && rm /tmp/edgedriver.zip \
    && msedgedriver --version

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy server & web code
COPY server/ ./server/
COPY web/ ./web/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

VOLUME ["/app/data"]

EXPOSE 8345 6345

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD curl -f http://localhost:8345/api/health || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
