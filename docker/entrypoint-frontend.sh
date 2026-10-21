#!/bin/sh
set -eu

escape_js() {
  printf '%s' "${1:-}" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

cat <<EOF > /usr/share/nginx/html/env-config.js
window.__ENV__ = {
  INGESTION_API_URL: "$(escape_js "${INGESTION_API_URL:-}")",
  AGENT_API_URL: "$(escape_js "${AGENT_API_URL:-}")",
  FIREBASE_API_KEY: "$(escape_js "${FIREBASE_API_KEY:-}")",
  FIREBASE_AUTH_DOMAIN: "$(escape_js "${FIREBASE_AUTH_DOMAIN:-}")",
  FIREBASE_PROJECT_ID: "$(escape_js "${FIREBASE_PROJECT_ID:-}")",
  FIREBASE_APP_ID: "$(escape_js "${FIREBASE_APP_ID:-}")"
};
EOF

echo "[entrypoint-frontend] Generated /usr/share/nginx/html/env-config.js"
