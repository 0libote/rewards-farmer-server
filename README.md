# 🌾 Rewards Farmer Server

A modern Docker web dashboard and orchestration server for [User0332/rewards-farmer](https://github.com/User0332/rewards-farmer) with **in-browser interactive login**, real-time task telemetry, multi-account management, automated scheduling, and Discord/webhook notifications.

Designed specifically for headless Docker servers (VPS, Synology, Unraid, TrueNAS, Raspberry Pi, home labs) with **zero code modifications to the upstream project**—always running live and fresh with upstream updates!

---

## ✨ Features

- 🌐 **Zero-Touch Upstream Tracking**: Keeps the upstream `rewards-farmer` codebase 100% clean and fresh. Cloned dynamically on container launch, with a single-click **"Pull Latest"** button to pull upstream updates and selector fixes at any time.
- 🖥️ **In-Browser Interactive Login (Web noVNC)**: Solves the headless Linux Docker login barrier. Click *"Interactive Login"* on any account to launch Microsoft Edge inside the container and control it directly in your web browser. Type passwords, solve captchas, approve 2FA notifications on your phone, or accept EU consent banners with zero host-display setup.
- 👥 **Multi-Account Profile Manager**: Add, manage, and run multiple accounts (`default`, `personal`, `spare`, etc.) with isolated browser profiles and cookies stored under `./data/data-dir`.
- 📊 **Real-Time Telemetry & Progress**: Track search points earned vs. daily maximums, live task statuses (`[OK]`, `[SKIP]`, `[FAIL]`), and execution history.
- ⚡ **Live WebSocket Terminal**: Watch logs stream in real-time with color-coded syntax highlighting and auto-scroll.
- ⏰ **Automated Daily Scheduler**: Built-in cron scheduler triggers runs at your chosen time (UTC) without external cron services.
- 🔔 **Webhook Notifications**: Discord alerts (other generic webhook receivers get a plain-text fallback) when daily runs start, complete, or fail.
- 🖼️ **Visual Search Image Tool**: Automatically fetches Wikimedia images via upstream's `random_image_for_visual_search.py` or allows uploading a custom image.
- 📝 **Seed Wordlist Editor**: View and customize `nouns.txt` directly from the web settings.
- 📦 **Automated GHCR Package**: Automatically built and published as a Docker container package (`ghcr.io/0libote/rewards-farmer-server:latest`) on every commit.

---

## 🚀 Quick Start

### Option A: Using Pre-Built Image (Recommended)

Create a `docker-compose.yml`:
```yaml
services:
  rewards-farmer-server:
    image: ghcr.io/0libote/rewards-farmer-server:latest
    container_name: rewards-farmer-server
    restart: unless-stopped
    shm_size: 2gb
    ports:
      - "8345:8345" # Web Dashboard
      - "6345:6345" # Interactive Browser Login (noVNC)
    volumes:
      - ./data:/app/data
```

Start the container:
```bash
docker compose up -d
```

### Option B: Build from Source
```bash
git clone https://github.com/0libote/rewards-farmer-server.git
cd rewards-farmer-server
docker compose up -d --build
```

### Open the Web Dashboard
Navigate to `http://<your-server-ip>:8345` in your browser.

---

## 🔑 Logging In (First-Time Setup)

Because Microsoft Edge runs natively inside Linux Docker, your login tokens and session cookies are securely created inside the container's environment:

1. Open the dashboard at `http://<your-server-ip>:8345`.
2. Find the account you want to configure (e.g. `default`) and click **"Interactive Login"**.
3. A modal opens with the live Microsoft Edge browser window streamed to your screen.
4. Sign in to your Microsoft Rewards account, complete your phone 2FA or authenticator challenge, and accept any cookie consent banners.
5. Once signed in and verified on `https://rewards.bing.com`, click **"Finish & Save Login"** at the top right of the modal.
6. Edge will cleanly flush session cookies to disk and free the browser lock.
7. Click **"Run"** or let the automated scheduler run daily!

---

## 📁 Persistent Data Structure

All persistent data is stored in the `./data` volume on the host, keeping it safe across container rebuilds:

```
data/
├── data-dir/            # Chromium user data profiles (one per account)
├── config.json          # Server settings, schedule, accounts list
├── nouns.txt            # Seed words for searches
├── visual_search.jpg    # Image for the visual search task
└── logs/                # Past run logs and history.json
```

---

## ⚙️ Configuration & Ports

| Port | Protocol | Description |
|------|----------|-------------|
| `8345` | HTTP / WebSocket | Main Web Dashboard, REST API & live log terminal |
| `6345` | HTTP / WebSocket | Interactive Browser Login (noVNC stream) |

> ⚠️ The noVNC stream on `6345` has no password. On remote servers, bind it to localhost (`127.0.0.1:6345:6345`) and reach it via an SSH tunnel or authenticated reverse proxy instead of exposing it publicly.

Settings can be changed directly in the **Web Dashboard** under the Settings modal (⚙️):
- **Search Query Backend**: Choose between `trends` (default, zero setup using Google/Bing trends and Wikipedia) or `llm` (Ollama LLM).
- **Ollama Host**: Address to reach your Ollama instance (e.g. `host.docker.internal:11434`).
- **Automated Schedule**: Daily at a fixed UTC time, every N hours, or at multiple custom UTC times — plus an optional run shortly after startup.
- **Webhook URL**: Discord webhook URL to receive status notifications (generic receivers get a plain-text fallback).

A `/api/health` endpoint (also at `/health`) is available for container healthchecks and uptime monitoring. Past run logs are kept under `./data/logs` (newest 30 files) and can be opened from the **Recent Run History** table or via `/api/logs/{filename}`.

---

## 🔄 Keeping Upstream Updated

Whenever [User0332/rewards-farmer](https://github.com/User0332/rewards-farmer) releases updates or fixes:
- Click the **"Pull Latest"** button in the Settings modal of the dashboard, OR
- Simply restart the container (`docker compose restart`). The entrypoint checks for new commits at startup without affecting your profiles or configs.

---

## 🛡️ License

This project is licensed under the MIT License.
Upstream automation code is copyright [User0332/rewards-farmer](https://github.com/User0332/rewards-farmer).
