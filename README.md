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
- 🩺 **Selector Self-Test**: One click per account runs upstream's read-only `check_selectors.py` and reports which selectors resolve, are absent, or failed — so UI changes can be diagnosed without waiting for a full run.
- 🔒 **Optional Token Authentication**: Set `DASHBOARD_TOKEN` to lock the dashboard, API and interactive login behind an access token (HttpOnly cookie session).
- 📴 **Fully Offline Dashboard**: Tailwind and Lucide are vendored locally, so the UI loads with no internet access and no third-party CDN at runtime.
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
      - "8345:8345" # Web Dashboard (interactive login is proxied at /vnc)
      - "6345:6345" # Optional: direct noVNC access
    environment:
      # Optional but recommended: require a token for the dashboard/API/login
      - DASHBOARD_TOKEN=${DASHBOARD_TOKEN:-}
      # Match the host user that owns ./data (id -u / id -g). Defaults to 1000.
      - PUID=1000
      - PGID=1000
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
| `8345` | HTTP / WebSocket | Main Web Dashboard, REST API, live log terminal & proxied interactive login (`/vnc`) |
| `6345` | HTTP / WebSocket | Interactive Browser Login (noVNC) — **optional** now that the dashboard proxies it |

> 🔒 **Authentication**: set `DASHBOARD_TOKEN` (environment variable or `.env`) to require a token for the dashboard, API and interactive login. When unset the dashboard is open, so on a shared or internet-facing host you should set it. The token is entered once in the browser and stored in an HttpOnly cookie.
>
> 👤 **Non-root**: the server and browser run as an unprivileged user (`PUID`/`PGID`, default `1000:1000`). Set them to your host user (`id -u` / `id -g`) so `./data` stays owned by you rather than root. The container only uses root briefly at startup to fix ownership and clone/update the upstream repo.
>
> The direct noVNC port `6345` has no password of its own. Since the dashboard now proxies the interactive login through `/vnc`, you can drop the `6345` mapping entirely (or bind it to `127.0.0.1`).

Settings can be changed directly in the **Web Dashboard** under the Settings modal (⚙️):
- **Search Query Backend**: Choose between `trends` (default, zero setup using Google/Bing trends and Wikipedia) or `llm`.
- **LLM Provider**: `local` (any Ollama-compatible `/v1` endpoint) or `openrouter`.
- **LLM Base URL**: e.g. `http://host.docker.internal:11434/v1` for local, or the OpenRouter API base.
- **LLM Model / API Key**: model name and optional key for the chosen provider.
- **LLM Timeout / OpenRouter Title & Referer**: optional extras mapped to `LLM_REQUEST_TIMEOUT_SECONDS`, `OPENROUTER_TITLE` and `OPENROUTER_HTTP_REFERER`.
- **Automated Schedule**: Daily at a fixed UTC time, every N hours, or at multiple custom UTC times — plus an optional run shortly after startup.
- **Webhook URL**: Discord webhook URL to receive status notifications (generic receivers get a plain-text fallback).

> ℹ️ The LLM settings map onto upstream's `LLM_PROVIDER`, `LOCAL_LLM_*` and `OPENROUTER_*` environment variables. Older `ollama_host` values are still honoured as a fallback base URL.
>
> ⚙️ Other upstream environment variables set on the container are passed straight through to the run, so advanced options work without dashboard fields: `EDGE_BINARY` (custom Edge path), `REWARDS_DRIVER_LOG` (verbose msedgedriver log, useful when Edge will not start) and `REWARDS_FARMER_LOG_LEVEL`. `REWARDS_ACCOUNTS` and `QUERY_SOURCE` are managed by the dashboard.

A `/api/health` endpoint (also at `/health`) is available for container healthchecks and uptime monitoring. Past run logs are kept under `./data/logs` (newest 30 files) and can be opened from the **Recent Run History** table or via `/api/logs/{filename}`.

---

## 🩺 Diagnosing "everything skipped"

If a run reports `[SKIP]`/`[FAIL]` for tasks you expect to work, use the **stethoscope button** on the account card. It runs upstream's read-only selector check against that account's profile and prints an `OK / ABSENT / FAILED` report (it completes no activities and claims no points). `ABSENT` is normal for a task your market does not ship; `FAILED` entries are what upstream needs a selector fix for. The same report is available at `POST /api/selectors/check` with `{"account": "default"}`.

## 🔄 Keeping Upstream Updated

Whenever [User0332/rewards-farmer](https://github.com/User0332/rewards-farmer) releases updates or fixes:
- Click the **"Pull Latest"** button in the Settings modal of the dashboard, OR
- Simply restart the container (`docker compose restart`). The entrypoint checks for new commits at startup without affecting your profiles or configs.

---

## 🛡️ License

This project is licensed under the MIT License.
Upstream automation code is copyright [User0332/rewards-farmer](https://github.com/User0332/rewards-farmer).
