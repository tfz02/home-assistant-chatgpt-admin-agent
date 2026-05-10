#!/usr/bin/env bash
set -e

echo "[ChatGPT Admin Agent] starting..."

OPTIONS_FILE="/data/options.json"

NGROK_ENABLED="$(jq -r '.ngrok_enabled // false' "$OPTIONS_FILE")"
NGROK_AUTHTOKEN="$(jq -r '.ngrok_authtoken // ""' "$OPTIONS_FILE")"
NGROK_DOMAIN="$(jq -r '.ngrok_domain // ""' "$OPTIONS_FILE")"

if [ "$NGROK_ENABLED" = "true" ]; then
  echo "[ChatGPT Admin Agent] ngrok enabled"

  if [ -z "$NGROK_AUTHTOKEN" ] || [ "$NGROK_AUTHTOKEN" = "null" ]; then
    echo "[ChatGPT Admin Agent] ERROR: ngrok_enabled=true but ngrok_authtoken is empty"
    exit 1
  fi

  ngrok config add-authtoken "$NGROK_AUTHTOKEN"

  if [ -n "$NGROK_DOMAIN" ] && [ "$NGROK_DOMAIN" != "null" ]; then
    echo "[ChatGPT Admin Agent] starting ngrok with domain: $NGROK_DOMAIN"
    ngrok http --domain="$NGROK_DOMAIN" 8787 > /tmp/ngrok.log 2>&1 &
  else
    echo "[ChatGPT Admin Agent] starting ngrok with random domain"
    ngrok http 8787 > /tmp/ngrok.log 2>&1 &
  fi

  sleep 3
  echo "[ChatGPT Admin Agent] ngrok log:"
  cat /tmp/ngrok.log || true
else
  echo "[ChatGPT Admin Agent] ngrok disabled"
fi

python3 /app/app.py
