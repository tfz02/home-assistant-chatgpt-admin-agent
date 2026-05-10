#!/usr/bin/env bash
set -e

echo "[ChatGPT Admin Agent] starting..."

OPTIONS_FILE="/data/options.json"
NGROK_CONFIG="/data/ngrok.yml"

NGROK_ENABLED="$(jq -r '.ngrok_enabled // false' "$OPTIONS_FILE")"
NGROK_AUTHTOKEN="$(jq -r '.ngrok_authtoken // ""' "$OPTIONS_FILE")"
NGROK_DOMAIN="$(jq -r '.ngrok_domain // ""' "$OPTIONS_FILE")"

export HOME="/data"

if [ "$NGROK_ENABLED" = "true" ]; then
  echo "[ChatGPT Admin Agent] ngrok enabled"

  if [ -z "$NGROK_AUTHTOKEN" ] || [ "$NGROK_AUTHTOKEN" = "null" ]; then
    echo "[ChatGPT Admin Agent] ERROR: ngrok_enabled=true but ngrok_authtoken is empty"
    exit 1
  fi

  echo "[ChatGPT Admin Agent] writing ngrok config to $NGROK_CONFIG"

  cat > "$NGROK_CONFIG" <<EOF
version: "2"
authtoken: "$NGROK_AUTHTOKEN"
EOF

  if [ -n "$NGROK_DOMAIN" ] && [ "$NGROK_DOMAIN" != "null" ]; then
    echo "[ChatGPT Admin Agent] starting ngrok with domain: $NGROK_DOMAIN"
    ngrok http --config="$NGROK_CONFIG" --domain="$NGROK_DOMAIN" 8787 > /tmp/ngrok.log 2>&1 &
  else
    echo "[ChatGPT Admin Agent] starting ngrok with random domain"
    ngrok http --config="$NGROK_CONFIG" 8787 > /tmp/ngrok.log 2>&1 &
  fi

  sleep 5
  echo "[ChatGPT Admin Agent] ngrok log:"
  cat /tmp/ngrok.log || true
else
  echo "[ChatGPT Admin Agent] ngrok disabled"
fi

python3 /app/app.py
