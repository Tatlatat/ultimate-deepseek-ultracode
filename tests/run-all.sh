#!/usr/bin/env bash
# Run the full CI-safe test suite (python + node + shell). Offline: no DeepSeek,
# no network — every test runs in mock mode. Exits non-zero if any test fails.
# Benches (runtime/*bench*.py) need real DeepSeek and are NOT run here.
set -u
cd "$(dirname "$0")/.." || exit 2
pass=0; fail=0; failed=()

run_one() {  # $1=label  $2..=command
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then
    echo "  ok   $label"; pass=$((pass+1))
  else
    echo "  FAIL $label"; fail=$((fail+1)); failed+=("$label")
  fi
}

echo "=== python tests ==="
for t in tests/test-*.py; do run_one "$(basename "$t")" python3 "$t"; done
echo "=== node tests ==="
for t in tests/test-*.mjs; do run_one "$(basename "$t")" node "$t"; done
echo "=== shell tests ==="
for t in tests/test-*.sh; do
  [ "$(basename "$t")" = "run-all.sh" ] && continue
  run_one "$(basename "$t")" bash "$t"
done

echo
echo "=== summary: $pass passed, $fail failed ==="
if [ "$fail" -ne 0 ]; then
  printf '  failed: %s\n' "${failed[@]}"
  exit 1
fi
exit 0
