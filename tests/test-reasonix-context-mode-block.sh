#!/usr/bin/env bash
# Regression for the context-mode plugin disallow (commit e75a3b9 A).
#
# A real fan-out session thrashed because the third-party context-mode plugin
# drove ~79 ctx_* calls in an 82-line transcript, refilling context to the limit.
# The launcher now disallows mcp__plugin_context-mode_context-mode__* in a fleet
# session (default ON; opt out with CLAUDE_REASONIX_KEEP_CONTEXT_MODE=1). The
# invariant the commit message stresses:
#   - native branch passes ONLY the context-mode block, NEVER Agent,Task
#     (native fan-out relies on the Agent path).
#   - fleet branch passes Agent,Task AND the context-mode block.
#
# These assertions would FAIL if fix A were reverted:
#   - the "block present" greps would not find the context-mode pattern.
#   - the native "no Agent,Task" assertion catches a revert that re-adds it.
#
# Isolated: temp HOME, /bin/echo as CLAUDE_BIN, never touches real state.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAUNCHER="$ROOT/bin/claude-reasonix"

CTX_PATTERN='mcp__plugin_context-mode_context-mode__*'

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

[[ -x "$LAUNCHER" ]] || fail "launcher not executable: $LAUNCHER"

tmp_home="$(mktemp -d)"
trap 'rm -rf "$tmp_home"' EXIT

# Point the launcher at the REPO as its install home so it loads the renamed
# gateway/mcp/hooks/settings under test (not a stale ~/.claude install).
export CLAUDE_REASONIX_FLEET_INSTALL_HOME="$ROOT"
export CLAUDE_REASONIX_FLEET_HOME="$tmp_home/fleet"
export CLAUDE_BIN="/bin/echo"
export REASONIX_BIN="/bin/echo"
export ANTHROPIC_API_KEY="test-anthropic-key"
export CLAUDE_REASONIX_GATEWAY_MOCK=1
export CLAUDE_REASONIX_KEEP_ROUTER_RUNTIME=1

# Extract the single --disallowedTools VALUE token (the arg right after the flag)
# from a captured launcher command line. Empty if the flag is absent.
disallowed_value() {
  # shellcheck disable=SC2001
  sed -n 's/.*--disallowedTools \([^ ]*\).*/\1/p' <<<"$1"
}

# ---------------------------------------------------------------------------
# FLEET branch (native subagents OFF).
# ---------------------------------------------------------------------------

# Default: block ON. The fleet branch must disallow BOTH Agent,Task AND the
# context-mode plugin, in one comma-joined --disallowedTools value.
fleet_default="$(CLAUDE_REASONIX_NATIVE_SUBAGENTS=0 "$LAUNCHER" "bare prompt" 2>/dev/null)"
fleet_default_val="$(disallowed_value "$fleet_default")"
[[ -n "$fleet_default_val" ]] || fail "fleet default must emit a --disallowedTools value"
grep -q "Agent,Task" <<<"$fleet_default_val" || fail "fleet default must still block Agent,Task: $fleet_default_val"
grep -qF "$CTX_PATTERN" <<<"$fleet_default_val" || fail "fleet default must block the context-mode plugin: $fleet_default_val"

# Opt-out: KEEP_CONTEXT_MODE=1 -> Agent,Task stays, context-mode block is gone.
fleet_keep="$(CLAUDE_REASONIX_NATIVE_SUBAGENTS=0 CLAUDE_REASONIX_KEEP_CONTEXT_MODE=1 "$LAUNCHER" "bare prompt" 2>/dev/null)"
fleet_keep_val="$(disallowed_value "$fleet_keep")"
grep -q "Agent,Task" <<<"$fleet_keep_val" || fail "fleet keep-mode must still block Agent,Task: $fleet_keep_val"
if grep -qF "$CTX_PATTERN" <<<"$fleet_keep_val"; then
  fail "KEEP_CONTEXT_MODE=1 must NOT block the context-mode plugin in fleet: $fleet_keep_val"
fi

# ---------------------------------------------------------------------------
# NATIVE branch (native subagents ON).
# ---------------------------------------------------------------------------

# Default: block ON. The native branch must disallow ONLY the context-mode
# plugin and must NEVER carry Agent,Task (native fan-out needs the Agent path).
native_default="$(CLAUDE_REASONIX_NATIVE_SUBAGENTS=1 "$LAUNCHER" run "native prompt" 2>/dev/null)"
grep -q -- "--agents" <<<"$native_default" || fail "native mode must still pass --agents: $native_default"
native_default_val="$(disallowed_value "$native_default")"
[[ -n "$native_default_val" ]] || fail "native default must emit a --disallowedTools value (the context-mode block)"
grep -qF "$CTX_PATTERN" <<<"$native_default_val" || fail "native default must block the context-mode plugin: $native_default_val"
if grep -q "Agent,Task" <<<"$native_default"; then
  fail "native mode must NEVER disallow Agent,Task (native fan-out relies on the Agent path): $native_default"
fi
# Belt-and-suspenders: the context-mode disallow value must be EXACTLY the
# plugin pattern in native mode — nothing else gets blocked.
[[ "$native_default_val" == "$CTX_PATTERN" ]] || fail "native disallow value must be exactly the context-mode pattern: $native_default_val"

# Opt-out: KEEP_CONTEXT_MODE=1 -> native passes NO --disallowedTools at all.
native_keep="$(CLAUDE_REASONIX_NATIVE_SUBAGENTS=1 CLAUDE_REASONIX_KEEP_CONTEXT_MODE=1 "$LAUNCHER" run "native prompt" 2>/dev/null)"
if grep -q -- "--disallowedTools" <<<"$native_keep"; then
  fail "native + KEEP_CONTEXT_MODE=1 must pass NO --disallowedTools at all: $native_keep"
fi
grep -q -- "--agents" <<<"$native_keep" || fail "native keep-mode must still pass --agents: $native_keep"

echo "PASS: reasonix context-mode plugin block"
