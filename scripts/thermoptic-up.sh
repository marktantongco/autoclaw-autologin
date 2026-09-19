#!/usr/bin/env bash
# thermoptic launcher — brings up the browser-camouflage egress proxy
# (Synergy 14, source: mandatoryprogrammer/thermoptic, ISC license).
#
# Usage:
#   bash scripts/thermoptic-up.sh [port]
#
# What it does:
#   1. Clones/pulls the pinned thermoptic checkout into ./thermoptic/
#   2. docker compose up -d (proxyrouter :3128, chrome CDP :9223,
#      HTTP proxy :1234, optional xpra GUI :14111)
#   3. Waits for the proxy port, prints the AutoClaw env to enable it
set -euo pipefail

THERMOPTIC_DIR="${THERMOPTIC_DIR:-./thermoptic}"
PINNED_REF="${THERMOPTIC_REF:-main}"
HTTP_PORT="${1:-1234}"

if [ ! -d "$THERMOPTIC_DIR/.git" ]; then
  echo "[thermoptic-up] cloning mandatoryprogrammer/thermoptic → $THERMOPTIC_DIR"
  git clone --depth 1 https://github.com/mandatoryprogrammer/thermoptic "$THERMOPTIC_DIR"
else
  echo "[thermoptic-up] refreshing existing checkout"
  git -C "$THERMOPTIC_DIR" fetch --depth 1 origin "$PINNED_REF"
  git -C "$THERMOPTIC_DIR" checkout FETCH_HEAD
fi

echo "[thermoptic-up] docker compose up (HTTP proxy :$HTTP_PORT)"
(
  cd "$THERMOPTIC_DIR"
  HTTP_PROXY_PORT="$HTTP_PORT" docker compose up -d --build
)

echo -n "[thermoptic-up] waiting for proxy"
for _ in $(seq 1 60); do
  if nc -z 127.0.0.1 "$HTTP_PORT" 2>/dev/null; then
    echo " — up!"
    CA_FILE="$THERMOPTIC_DIR/ssl/rootCA.crt"
    cat <<EOF

[thermoptic-up] enable the egress tier in AutoClaw:

  export OWL_THERMOPTIC_ENABLED=1
  export OWL_THERMOPTIC_URL=http://127.0.0.1:$HTTP_PORT
  $( [ -f "$CA_FILE" ] && echo "export OWL_THERMOPTIC_CA=$CA_FILE" )

[thermoptic-up] xpra Chrome GUI (if ENABLE_GUI_CONTROL): http://127.0.0.1:14111
[thermoptic-up] verify:  curl --proxy http://127.0.0.1:$HTTP_PORT --insecure https://ja4db.com/id/ja4h/
EOF
    exit 0
  fi
  echo -n "."
  sleep 2
done

echo " — timeout waiting for :$HTTP_PORT (check: docker compose logs)" >&2
exit 1
