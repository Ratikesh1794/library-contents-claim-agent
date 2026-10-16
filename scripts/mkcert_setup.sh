#!/usr/bin/env bash
# Local-HTTPS alternative to a tunnel (same Wi-Fi, lower latency, media never leaves your network).
# 1) brew install mkcert && mkcert -install      2) run this      3) install the mkcert root CA on the phone
set -euo pipefail
cd "$(dirname "$0")/.."
IP=$(ipconfig getifaddr en0 || hostname -I | awk '{print $1}')
mkdir -p certs && mkcert -cert-file certs/cert.pem -key-file certs/key.pem "$IP" localhost
# Token: shell env > SWEEP_TOKEN in .env (stable URL/QR across restarts) > random
export SWEEP_TOKEN="${SWEEP_TOKEN:-$(.venv/bin/python -c 'from dotenv import dotenv_values;print(dotenv_values(".env").get("SWEEP_TOKEN") or "")')}"
export SWEEP_TOKEN="${SWEEP_TOKEN:-$(.venv/bin/python -c 'import secrets;print(secrets.token_urlsafe(12))')}"
export PUBLIC_ORIGIN="https://$IP:8000"
DASH="https://localhost:8000/dashboard?t=$SWEEP_TOKEN"
echo "Dashboard: $DASH  (opening in your browser; the phone's QR code is there)"
command -v open >/dev/null && (sleep 3 && open "$DASH") &
.venv/bin/python -m uvicorn backend.live.app:app --host 0.0.0.0 --port 8000 --ssl-keyfile certs/key.pem --ssl-certfile certs/cert.pem --ws-max-size 4000000
