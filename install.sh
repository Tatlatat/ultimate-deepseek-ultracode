#!/usr/bin/env bash
# claude-reasonix installer — copies the fleet into ~/.claude/reasonix-fleet, puts the
# launcher on PATH, and applies the reasonix ACP ephemeral-session patch.
#
# Philosophy: CHECK and INSTRUCT. We never silently auto-install someone else's tools
# (reasonix, claude, node) — instead we verify each dependency and, if something is
# missing, print exactly what to do and stop. Everything we DO own (file copy, launcher
# symlink, dist patch) is idempotent: re-running install.sh is always safe.
#
# Usage:  ./install.sh            # install / re-install
#         REASONIX_BIN=/path ./install.sh   # if reasonix is not on PATH
set -euo pipefail

# Resolve the repo root (this script's dir), independent of the caller's cwd.
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_HOME="${CLAUDE_REASONIX_FLEET_INSTALL_HOME:-$HOME/.claude/reasonix-fleet}"
BIN_DIR="${CLAUDE_REASONIX_BIN_DIR:-$HOME/.local/bin}"
LAUNCHER_NAME="claude-reasonix"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  !\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m  ✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ----------------------------------------------------------------------------
say "1/6  Checking required tools"
# ----------------------------------------------------------------------------
command -v python3 >/dev/null 2>&1 \
  || die "python3 not found. Install Python 3.8+ and re-run."
ok "python3 — $(command -v python3)"

command -v claude >/dev/null 2>&1 \
  || die "Claude Code CLI ('claude') not found. Install it (https://claude.com/claude-code) and re-run."
ok "claude — $(command -v claude)"

# reasonix: prefer $REASONIX_BIN, else PATH. It is the DeepSeek engine the fleet drives.
REASONIX_BIN="${REASONIX_BIN:-$(command -v reasonix 2>/dev/null || true)}"
[ -n "$REASONIX_BIN" ] && [ -x "$REASONIX_BIN" ] \
  || die "reasonix CLI not found. Install it (npm i -g reasonix), or pass REASONIX_BIN=/path/to/reasonix, then re-run."
ok "reasonix — $REASONIX_BIN"

# node: reasonix needs node alongside it (the gateway spawns reasonix and needs node on PATH).
NODE_BIN="$(command -v node 2>/dev/null || true)"
if [ -z "$NODE_BIN" ]; then
  cand="$(dirname "$REASONIX_BIN")/node"
  [ -x "$cand" ] && NODE_BIN="$cand"
fi
[ -n "$NODE_BIN" ] \
  || warn "node not found on PATH. reasonix usually ships beside node; if lanes fail with 'node not found', add it to PATH."
[ -n "$NODE_BIN" ] && ok "node — $NODE_BIN"

# ----------------------------------------------------------------------------
say "2/6  Installing the fleet into $INSTALL_HOME"
# ----------------------------------------------------------------------------
mkdir -p "$INSTALL_HOME/hooks" "$INSTALL_HOME/runtime/logs" "$INSTALL_HOME/state"
# Copy the shipped components (NOT scratch: runtime ledgers/ports, .git, .superpowers).
for item in \
  reasonix-native-gateway.py reasonix-fleet-mcp.py \
  bridge-settings.json system-prompt-reasonix.md ; do
  cp -f "$SRC/$item" "$INSTALL_HOME/$item"
done
cp -f "$SRC/hooks/"*.py "$INSTALL_HOME/hooks/"
# Benches/diagnostics are handy but optional; copy the runtime tooling, not the ledgers.
cp -f "$SRC/runtime/realworld-bench.py" "$INSTALL_HOME/runtime/" 2>/dev/null || true
cp -f "$SRC/runtime/cross-workflow-bench.py" "$INSTALL_HOME/runtime/" 2>/dev/null || true
ok "fleet files copied"

# ----------------------------------------------------------------------------
say "3/6  Installing the launcher into $BIN_DIR/$LAUNCHER_NAME"
# ----------------------------------------------------------------------------
mkdir -p "$BIN_DIR"
cp -f "$SRC/bin/claude-reasonix" "$BIN_DIR/$LAUNCHER_NAME"
chmod +x "$BIN_DIR/$LAUNCHER_NAME"
ok "launcher installed"

case ":$PATH:" in
  *":$BIN_DIR:"*) ok "$BIN_DIR is on PATH" ;;
  *) warn "$BIN_DIR is NOT on PATH. Add this to your shell rc and restart your shell:"
     printf '      export PATH="%s:$PATH"\n' "$BIN_DIR" ;;
esac

# ----------------------------------------------------------------------------
say "4/6  Applying the reasonix ACP ephemeral-session patch"
# ----------------------------------------------------------------------------
# This is the one dependency we modify in place; it reverts on a reasonix upgrade, so
# re-run install.sh (or patches/apply_ephemeral.py) after upgrading reasonix.
if REASONIX_BIN="$REASONIX_BIN" python3 "$SRC/patches/apply_ephemeral.py"; then
  ok "ephemeral-session patch applied (or already present)"
else
  warn "ephemeral-session patch did not apply cleanly. Fan-out cache will be lower; see patches/ephemeral-session.md. Continuing."
fi

# ----------------------------------------------------------------------------
say "5/6  Smoke-checking the install"
# ----------------------------------------------------------------------------
# The launcher's own doctor validates the wired-up install (files present, settings valid).
if CLAUDE_REASONIX_FLEET_INSTALL_HOME="$INSTALL_HOME" REASONIX_BIN="$REASONIX_BIN" \
   "$BIN_DIR/$LAUNCHER_NAME" doctor >/tmp/claude-reasonix-doctor.$$ 2>&1; then
  ok "launcher doctor passed"
else
  warn "launcher 'doctor' reported issues (see /tmp/claude-reasonix-doctor.$$):"
  sed 's/^/      /' "/tmp/claude-reasonix-doctor.$$" || true
fi
rm -f "/tmp/claude-reasonix-doctor.$$" 2>/dev/null || true

# ----------------------------------------------------------------------------
say "6/6  Done"
# ----------------------------------------------------------------------------
ok "Installed. Start a reasonix session with:"
printf '      %s "your prompt"        # or just: %s\n' "$LAUNCHER_NAME" "$LAUNCHER_NAME"
printf '      %s on                   # enable fleet mode, then run claude normally\n' "$LAUNCHER_NAME"
echo
echo "  Reinstall/upgrade: re-run ./install.sh (safe, idempotent)."
echo "  After upgrading reasonix: re-run ./install.sh to re-apply the ACP patch."
echo "  Uninstall: ./uninstall.sh"
