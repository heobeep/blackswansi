#!/bin/sh
# Persistent disks (Render, Docker volumes) are often mounted root-owned. Fix ownership of the data
# directory while still root, then drop privileges so the bot itself never runs as root.
set -e
DB="${DATABASE_PATH:-data/trading_bot.db}"
DIR="$(dirname "$DB")"
mkdir -p "$DIR"
if [ "$(id -u)" = "0" ]; then
  chown -R bot:bot "$DIR" 2>/dev/null || echo "WARNING: could not chown $DIR; the database may not be writable"
  exec gosu bot "$@"
fi
exec "$@"
