#!/bin/bash
set -e

echo "=== Starting Rewards Farmer Server ==="

DATA_DIR="${DATA_PATH:-/app/data}"
UPSTREAM_DIR="${UPSTREAM_PATH:-/app/upstream}"
PORT="${PORT:-8345}"

mkdir -p "$DATA_DIR/data-dir"
mkdir -p "$DATA_DIR/logs"

# Ensure upstream is cloned if not present
if [ ! -d "$UPSTREAM_DIR/.git" ]; then
    echo "Cloning upstream repository from https://github.com/User0332/rewards-farmer.git..."
    git clone --depth 1 https://github.com/User0332/rewards-farmer.git "$UPSTREAM_DIR"
else
    echo "Upstream directory already exists. Fetching latest updates..."
    git -C "$UPSTREAM_DIR" pull || true
fi

# Link persistent data inside upstream so upstream scripts find it naturally
ln -sfn "$DATA_DIR/data-dir" "$UPSTREAM_DIR/data-dir"

if [ -f "$DATA_DIR/nouns.txt" ]; then
    ln -sfn "$DATA_DIR/nouns.txt" "$UPSTREAM_DIR/nouns.txt"
elif [ -f "$UPSTREAM_DIR/nouns.txt" ]; then
    cp "$UPSTREAM_DIR/nouns.txt" "$DATA_DIR/nouns.txt"
    ln -sfn "$DATA_DIR/nouns.txt" "$UPSTREAM_DIR/nouns.txt"
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

# Start FastAPI server
echo "Starting Dashboard on port $PORT..."
exec uvicorn server.app:app --host 0.0.0.0 --port "$PORT"
