#!/usr/bin/env bash
# Serverni va internetga chiquvchi Cloudflare tunnelini yoqadi. To'xtatish: Ctrl+C
cd "$(dirname "$0")"
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 &
SERVER=$!
trap 'kill $SERVER 2>/dev/null' EXIT
until curl -sf localhost:8000/health >/dev/null; do sleep 1; done
echo "Server tayyor. Internet manzili quyida chiqadi (https://....trycloudflare.com):"
bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8000 2>&1 | grep --line-buffered -oE "https://[a-z0-9-]+\.trycloudflare\.com"
