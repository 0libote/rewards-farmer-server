#!/bin/bash
set -e

echo "=== Starting Rewards Farmer Server ==="

DATA_DIR="${DATA_PATH:-/app/data}"
UPSTREAM_DIR="${UPSTREAM_PATH:-/app/upstream}"
PORT="${PORT:-8345}"

# The server and browser run as this unprivileged user. Match PUID/PGID to the
# host user that owns the bind-mounted ./data so files are not root-owned.
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
RUNTIME_USER="${RUNTIME_USER:-app}"

mkdir -p "$DATA_DIR/data-dir"
mkdir -p "$DATA_DIR/logs"

if [ "$(id -u)" = "0" ]; then
    if ! getent group "$PGID" >/dev/null 2>&1; then
        groupadd -o -g "$PGID" "$RUNTIME_USER"
    fi
    if ! id -u "$RUNTIME_USER" >/dev/null 2>&1; then
        useradd -o -u "$PUID" -g "$PGID" -d "/home/$RUNTIME_USER" -m "$RUNTIME_USER"
    else
        usermod -o -u "$PUID" -g "$PGID" "$RUNTIME_USER" 2>/dev/null || true
    fi
fi

# Ensure upstream is cloned if not present
if [ ! -d "$UPSTREAM_DIR/.git" ]; then
    echo "Cloning upstream repository from https://github.com/User0332/rewards-farmer.git..."
    git clone --depth 1 https://github.com/User0332/rewards-farmer.git "$UPSTREAM_DIR"
else
    echo "Upstream directory already exists. Fetching latest updates..."
    # Our copied nouns.txt shows as a local change to a tracked file, which
    # blocks a fast-forward pull. Discard it first; it is re-applied below.
    git -C "$UPSTREAM_DIR" checkout -- nouns.txt 2>/dev/null || true
    git -C "$UPSTREAM_DIR" pull --ff-only || true
fi

# Link persistent data inside upstream so upstream scripts find it naturally
ln -sfn "$DATA_DIR/data-dir" "$UPSTREAM_DIR/data-dir"

# nouns.txt is tracked by upstream git, so copy it instead of symlinking: a
# symlink replaces a tracked file and makes `git pull --ff-only` fail. The
# persistent copy is authoritative once it exists.
if [ -f "$DATA_DIR/nouns.txt" ]; then
    rm -f "$UPSTREAM_DIR/nouns.txt"
    cp -f "$DATA_DIR/nouns.txt" "$UPSTREAM_DIR/nouns.txt"
elif [ -f "$UPSTREAM_DIR/nouns.txt" ]; then
    cp -f "$UPSTREAM_DIR/nouns.txt" "$DATA_DIR/nouns.txt"
fi

if [ -f "$DATA_DIR/visual_search.jpg" ]; then
    ln -sfn "$DATA_DIR/visual_search.jpg" "$UPSTREAM_DIR/visual_search.jpg"
fi

# Ensure visual search image exists if not yet created
if [ ! -f "$DATA_DIR/visual_search.jpg" ]; then
    echo "Fetching initial random visual search image from Wikimedia..."
    python3 "$UPSTREAM_DIR/src/random_image_for_visual_search.py" || true
    if [ -f "$UPSTREAM_DIR/visual_search.jpg" ] && [ ! -L "$UPSTREAM_DIR/visual_search.jpg" ]; then
        mv "$UPSTREAM_DIR/visual_search.jpg" "$DATA_DIR/visual_search.jpg"
        ln -sfn "$DATA_DIR/visual_search.jpg" "$UPSTREAM_DIR/visual_search.jpg"
    fi
fi

# Hand the persistent directories to the runtime user, then drop root. git and
# the initial image fetch above ran as root; everything after runs unprivileged.
echo "Starting Dashboard on port $PORT as uid $PUID..."
if [ "$(id -u)" = "0" ]; then
    chown -R "$PUID:$PGID" "$DATA_DIR" "$UPSTREAM_DIR" "/home/$RUNTIME_USER" 2>/dev/null || true
    mkdir -p /tmp/.X11-unix && chown "$PUID:$PGID" /tmp/.X11-unix 2>/dev/null || true
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups \
        env HOME="/home/$RUNTIME_USER" \
        uvicorn server.app:app --host 0.0.0.0 --port "$PORT"
fi

exec uvicorn server.app:app --host 0.0.0.0 --port "$PORT"
