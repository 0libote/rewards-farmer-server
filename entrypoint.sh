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

# Start FastAPI server
echo "Starting Dashboard on port $PORT..."
exec uvicorn server.app:app --host 0.0.0.0 --port "$PORT"
