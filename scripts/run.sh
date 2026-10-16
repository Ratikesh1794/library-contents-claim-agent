#!/usr/bin/env bash
# Start the server + an HTTPS tunnel and print the URL for the phone.
# Needs: .venv, and ngrok or cloudflared.   Usage: scripts/run.sh
# Fixed URL: set NGROK_DOMAIN in .env (your free static domain, e.g. something.ngrok-free.app) and the
# phone link + QR stay identical across restarts. Without it, ngrok/cloudflared hand out a random URL each run.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
envval() { $PY -c "from dotenv import dotenv_values;print(dotenv_values('.env').get('$1') or '')"; }   # value from .env
# Token: shell env > SWEEP_TOKEN in .env (stable URL/QR across restarts) > random
export SWEEP_TOKEN="${SWEEP_TOKEN:-$(envval SWEEP_TOKEN)}"
export SWEEP_TOKEN="${SWEEP_TOKEN:-$($PY -c 'import secrets;print(secrets.token_urlsafe(12))')}"
NGROK_DOMAIN="${NGROK_DOMAIN:-$(envval NGROK_DOMAIN)}"
NGROK_DOMAIN="${NGROK_DOMAIN#https://}"; NGROK_DOMAIN="${NGROK_DOMAIN%/}"      # accept "https://x.ngrok-free.app/" too
LOG=$(mktemp)
trap 'kill $(jobs -p) 2>/dev/null' EXIT                     # stop the tunnel whenever we exit

if [ -n "$NGROK_DOMAIN" ]; then
  command -v ngrok >/dev/null || { echo "NGROK_DOMAIN is set but ngrok is not installed (brew install ngrok)."; exit 1; }
  ngrok http 8000 --url="https://$NGROK_DOMAIN" --log=stdout >"$LOG" 2>&1 &
  URL=""
  for _ in $(seq 1 20); do                                  # wait until ngrok confirms the tunnel, or fails
    if grep -q '"started tunnel"\|msg="started tunnel"' "$LOG"; then URL="https://$NGROK_DOMAIN"; break; fi
    if grep -qi 'err=\|ERROR' "$LOG"; then break; fi
    sleep 1
  done
  if [ -z "$URL" ]; then
    echo "ngrok could not start https://$NGROK_DOMAIN. Its log:"; grep -i 'err\|ERROR' "$LOG" | tail -5
    echo "Check the domain in https://dashboard.ngrok.com/domains and that 'ngrok config add-authtoken' was run for the same account."
    exit 1
  fi
elif command -v cloudflared >/dev/null; then
  cloudflared tunnel --url http://localhost:8000 >"$LOG" 2>&1 &
  for _ in $(seq 1 30); do URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$LOG" | head -1 || true); [ -n "${URL:-}" ] && break; sleep 1; done
elif command -v ngrok >/dev/null; then
  ngrok http 8000 --log=stdout >"$LOG" 2>&1 &
  for _ in $(seq 1 30); do URL=$(curl -s localhost:4040/api/tunnels | $PY -c 'import sys,json;print(json.load(sys.stdin)["tunnels"][0]["public_url"])' 2>/dev/null || true); [ -n "${URL:-}" ] && break; sleep 1; done
else
  echo "Install cloudflared or ngrok (or use scripts/mkcert_setup.sh). Serving on http://localhost:8000 only."; URL="http://localhost:8000"
fi
export PUBLIC_ORIGIN="${URL:-}"
FULL="${URL:-http://localhost:8000}/?t=$SWEEP_TOKEN"
echo; echo "Open on the phone:  $FULL"; $PY scripts/print_qr.py "$FULL"; echo
# Laptop dashboard (records + live activity): opened automatically once the server is up.
DASH="http://localhost:8000/dashboard?t=$SWEEP_TOKEN"
echo "Laptop dashboard:  $DASH"; echo
command -v open >/dev/null && (sleep 3 && open "$DASH") &
$PY -m uvicorn backend.live.app:app --host 0.0.0.0 --port 8000 --ws-max-size 4000000
