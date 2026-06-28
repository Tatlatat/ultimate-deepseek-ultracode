#!/usr/bin/env bash
# Drive the REAL conductor-guard hook as Claude Code does: JSON on stdin, assert exit code.
# Isolated: temp TMPDIR; never touches real state.
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK="$ROOT/hooks/conductor-guard.py"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
export TMPDIR="$TMP"
pass=0; fail=0
run() {  # $1=label $2=expected_exit $3=env(0/1 ON) $4=json
  local got
  if [ "$3" = "1" ]; then
    printf '%s' "$4" | CLAUDE_REASONIX_CONDUCTOR_REVIEW_ONLY=1 python3 "$HOOK" >/dev/null 2>&1
  else
    printf '%s' "$4" | python3 "$HOOK" >/dev/null 2>&1
  fi
  got=$?
  if [ "$got" = "$2" ]; then echo "  ok   $1"; pass=$((pass+1)); else echo "  FAIL $1 (exit $got, want $2)"; fail=$((fail+1)); fi
}

run "flag OFF: Edit allowed (exit 0)" 0 0 '{"tool_name":"Edit","tool_input":{"file_path":"x"},"session_id":"s"}'
run "flag ON: Edit denied (exit 2)"   2 1 '{"tool_name":"Edit","tool_input":{"file_path":"x"},"session_id":"s"}'
run "flag ON: Read allowed (exit 0)"  0 1 '{"tool_name":"Read","tool_input":{"file_path":"x"},"session_id":"s"}'
run "flag ON: Bash test allowed"      0 1 '{"tool_name":"Bash","tool_input":{"command":"pytest -q"},"session_id":"s"}'
run "flag ON: Bash redirect denied"   2 1 '{"tool_name":"Bash","tool_input":{"command":"echo x > f"},"session_id":"s"}'
run "fail-open: malformed JSON -> allow" 0 1 'not json'
run "fail-open: no session_id -> allow"  0 1 '{"tool_name":"Edit","tool_input":{"file_path":"x"}}'

# escalation valve
mkdir -p "$TMP/reasonix-conductor-escalations"
echo "LANE_ESCALATE" > "$TMP/reasonix-conductor-escalations/s"
run "escalation pending: Edit allowed" 0 1 '{"tool_name":"Edit","tool_input":{"file_path":"x"},"session_id":"s"}'

echo "=== $pass passed, $fail failed ==="
[ "$fail" = "0" ]
