#!/usr/bin/env bash
# End-to-end smoke test for claude-reasonix-flash via the real gateway + real reasonix CLI.
# OPT-IN ONLY: only runs when CLAUDE_CODEX_REASONIX_E2E=1 (set by the caller or directly).
# Requires reasonix to be logged in. Slow (~10-40s, real DeepSeek call).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
GATEWAY="$ROOT/codex-native-gateway.py"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

# --- Step 1: locate reasonix --------------------------------------------------
REASONIX_BIN="$(command -v reasonix 2>/dev/null || true)"
if [[ -z "$REASONIX_BIN" ]]; then
  echo "SKIP: reasonix not on PATH"
  exit 0
fi
export REASONIX_BIN

# --- Setup: temp dir + trap ---------------------------------------------------
TMPDIR_E2E="$(mktemp -d)"
PORT_FILE="$TMPDIR_E2E/gateway.port"
GATEWAY_LOG="$TMPDIR_E2E/gateway.log"
GATEWAY_PID=""

cleanup() {
  if [[ -n "$GATEWAY_PID" ]] && kill -0 "$GATEWAY_PID" 2>/dev/null; then
    kill "$GATEWAY_PID" 2>/dev/null || true
    wait "$GATEWAY_PID" 2>/dev/null || true
  fi
  rm -rf "$TMPDIR_E2E"
}
trap cleanup EXIT

# --- Step 2: set env for gateway ----------------------------------------------
# Gateway needs a writable cwd for reasonix to create files.
export CLAUDE_CODEX_GATEWAY_CODEX_CWD="$TMPDIR_E2E"
# Expose reasonix flavor so gateway serves claude-reasonix-flash in its registry.
export CLAUDE_CODEX_FLAVOR=reasonix

# --- Step 3: start gateway on a random port -----------------------------------
python3 "$GATEWAY" --host 127.0.0.1 --port 0 --port-file "$PORT_FILE" \
  >"$GATEWAY_LOG" 2>&1 &
GATEWAY_PID=$!

# Wait up to 10s for the port file.
for _ in {1..100}; do
  if [[ -s "$PORT_FILE" ]]; then
    break
  fi
  if ! kill -0 "$GATEWAY_PID" 2>/dev/null; then
    echo "--- gateway log ---" >&2
    cat "$GATEWAY_LOG" >&2 || true
    fail "gateway exited before writing port file"
  fi
  sleep 0.1
done
[[ -s "$PORT_FILE" ]] || fail "gateway did not write port file within 10s"

PORT="$(cat "$PORT_FILE")"
BASE="http://127.0.0.1:${PORT}"

# --- Step 4: health check -----------------------------------------------------
curl -sf "$BASE/health" >/dev/null || fail "gateway /health check failed"

# --- Step 5: POST /v1/messages ------------------------------------------------
BODY='{"model":"claude-reasonix-flash","messages":[{"role":"user","content":"Create a file named rx.txt in the current directory containing exactly: PONG"}]}'

SSE_RESPONSE="$(curl -sS -N \
  --max-time 90 \
  -X POST "$BASE/v1/messages" \
  -H "content-type: application/json" \
  --data "$BODY" 2>"$TMPDIR_E2E/curl.err" || true)"

# --- Step 6: assert SSE structure ---------------------------------------------
if ! grep -q "event: message_start" <<<"$SSE_RESPONSE"; then
  echo "--- curl stderr ---" >&2
  cat "$TMPDIR_E2E/curl.err" >&2 || true
  echo "--- SSE response (first 500 chars) ---" >&2
  printf '%s\n' "$SSE_RESPONSE" | head -c 500 >&2
  fail "SSE response did not contain 'event: message_start'"
fi

# --- Step 7: assert rx.txt was created with PONG ------------------------------
RX_FILE="$TMPDIR_E2E/rx.txt"
if [[ ! -f "$RX_FILE" ]]; then
  echo "--- SSE response (first 500 chars) ---" >&2
  printf '%s\n' "$SSE_RESPONSE" | head -c 500 >&2
  fail "reasonix did not create rx.txt in $TMPDIR_E2E"
fi
if ! grep -q "PONG" "$RX_FILE"; then
  echo "--- rx.txt contents ---" >&2
  cat "$RX_FILE" >&2 || true
  fail "rx.txt does not contain 'PONG'"
fi

echo "PASS: reasonix e2e"
