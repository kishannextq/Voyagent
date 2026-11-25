#!/usr/bin/env bash
# Start everything the browser tests need, run them, stop everything.
#   app project : web :3011 -> api :8011 (auth disabled)
#   auth project: web :3012 -> api :8012 (AUTH_MODE=cognito) -> mock OIDC :9100
# Extra args go to `playwright test`. PW_CHANNEL=msedge uses an installed Edge.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
WORK=${E2E_WORK_DIR:-$ROOT/tests/e2e/.servers}
export NEXT_TELEMETRY_DISABLED=1 MOCK_MODE=true MOCK_DELAY=0.05
PIDS=()
cleanup() { for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT
mkdir -p "$WORK"

cd "$ROOT/services/api"
FRONTEND_ORIGINS=http://127.0.0.1:3011 uv run uvicorn server:api --port 8011 > "$WORK/api-app.log" 2>&1 &
PIDS+=($!)
AUTH_MODE=cognito COGNITO_ISSUER=http://127.0.0.1:9100 COGNITO_CLIENT_IDS=e2e-web-client \
  FRONTEND_ORIGINS=http://127.0.0.1:3012 uv run uvicorn server:api --port 8012 > "$WORK/api-auth.log" 2>&1 &
PIDS+=($!)
MOCK_OIDC_ISSUER=http://127.0.0.1:9100 uv run uvicorn server:app --app-dir "$ROOT/tools/mock-oidc" --port 9100 > "$WORK/oidc.log" 2>&1 &
PIDS+=($!)

build_web() {  # $1 output dir, rest: NAME=VALUE build env
  local out=$1
  shift
  (cd "$ROOT/apps/web" && env "$@" npm run build > "$WORK/build-$(basename "$out").log" 2>&1)
  rm -rf "$out"
  cp -r "$ROOT/apps/web/.next/standalone" "$out"
  cp -r "$ROOT/apps/web/.next/static" "$out/.next/"
}
build_web "$WORK/web-app" NEXT_PUBLIC_API_URL=http://127.0.0.1:8011
build_web "$WORK/web-auth" NEXT_PUBLIC_API_URL=http://127.0.0.1:8012 NEXT_PUBLIC_AUTH_MODE=cognito \
  NEXT_PUBLIC_COGNITO_ISSUER=http://127.0.0.1:9100 NEXT_PUBLIC_COGNITO_CLIENT_ID=e2e-web-client \
  NEXT_PUBLIC_COGNITO_DOMAIN=http://127.0.0.1:9100
PORT=3011 HOSTNAME=127.0.0.1 node "$WORK/web-app/server.js" > "$WORK/web-app.log" 2>&1 &
PIDS+=($!)
PORT=3012 HOSTNAME=127.0.0.1 node "$WORK/web-auth/server.js" > "$WORK/web-auth.log" 2>&1 &
PIDS+=($!)

for url in http://127.0.0.1:8011/api/v1/health http://127.0.0.1:8012/api/v1/health \
           http://127.0.0.1:9100/.well-known/openid-configuration http://127.0.0.1:3011/ http://127.0.0.1:3012/; do
  for _ in $(seq 1 90); do curl -sf "$url" > /dev/null && break; sleep 1; done
  curl -sf "$url" > /dev/null || { echo "not ready: $url"; tail -20 "$WORK"/*.log; exit 1; }
done

cd "$ROOT/tests/e2e"
E2E_WEB_URL=http://127.0.0.1:3011 E2E_WEB_AUTH_URL=http://127.0.0.1:3012 npx playwright test "$@"
