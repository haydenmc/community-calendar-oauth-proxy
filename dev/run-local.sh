#!/usr/bin/env bash
# Run the whole stack locally against a mock identity provider.
#
#   ./dev/run-local.sh
#
# Then open http://127.0.0.1:8000 and sign in as any username you like.
# Everything runs on loopback, so there is no container networking to arrange.
# State lives in ./.local and can be deleted at any time.
set -euo pipefail

cd "$(dirname "$0")/.."

VENV=${VENV:-.venv}
WORK=.local
PROXY_PORT=${PROXY_PORT:-8000}
RADICALE_PORT=${RADICALE_PORT:-5232}
OIDC_PORT=${OIDC_PORT:-8080}

if [ ! -x "$VENV/bin/python" ]; then
  echo "No virtualenv at $VENV. Create one with:" >&2
  echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt" >&2
  exit 1
fi

if ! "$VENV/bin/python" -c "import radicale" 2>/dev/null; then
  echo "Installing Radicale into $VENV ..."
  "$VENV/bin/pip" install --quiet "radicale>=3.3,<4"
fi

mkdir -p "$WORK/collections"
sed "s|/data/collections|$PWD/$WORK/collections|; s|0.0.0.0:5232|127.0.0.1:$RADICALE_PORT|" \
  radicale/config > "$WORK/radicale.conf"

pids=()
cleanup() {
  trap - INT TERM EXIT
  [ ${#pids[@]} -gt 0 ] && kill "${pids[@]}" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

echo "Starting Radicale on 127.0.0.1:$RADICALE_PORT ..."
"$VENV/bin/radicale" --config "$WORK/radicale.conf" >"$WORK/radicale.log" 2>&1 &
pids+=($!)

echo "Starting mock identity provider on 127.0.0.1:$OIDC_PORT ..."
MOCK_OIDC_ISSUER="http://127.0.0.1:$OIDC_PORT" \
"$VENV/bin/uvicorn" dev.mock_oidc:app --host 127.0.0.1 --port "$OIDC_PORT" \
  >"$WORK/oidc.log" 2>&1 &
pids+=($!)

export PUBLIC_BASE_URL="http://127.0.0.1:$PROXY_PORT" \
       OIDC_ISSUER="http://127.0.0.1:$OIDC_PORT" \
       OIDC_CLIENT_ID=calendar \
       OIDC_CLIENT_SECRET=dev-secret \
       OIDC_PROVIDER_NAME="the mock provider" \
       SESSION_SECRET=dev-only-not-a-secret \
       COOKIE_SECURE=false \
       RADICALE_URL="http://127.0.0.1:$RADICALE_PORT" \
       DATABASE_PATH="$WORK/calendar-proxy.db" \
       DISPLAY_TIMEZONE="${DISPLAY_TIMEZONE:-UTC}"

echo
echo "  Calendar:  http://127.0.0.1:$PROXY_PORT"
echo "  Logs:      $WORK/radicale.log, $WORK/oidc.log"
echo "  Ctrl-C to stop everything."
echo

# The .env file is for deployment; ignore it here so local runs are reproducible.
"$VENV/bin/uvicorn" app.main:build --factory --host 127.0.0.1 --port "$PROXY_PORT" --reload
