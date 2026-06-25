#!/usr/bin/env bash
# claude-reasonix installer — copies the fleet (incl. the bundled fork engine) into
# ~/.claude/reasonix-fleet and puts the launcher on PATH.
#
# The DeepSeek engine is the owner's fork (built using ideas from reasonix), shipped
# as a self-contained bundle under vendor/reasonix-engine and called IN-PROCESS via
# a one-shot Node shim. There is NO upstream-reasonix install: end users need only
# node + a DeepSeek credential.
#
# Philosophy: CHECK and INSTRUCT. We never silently auto-install someone else's tools
# (claude, node) — instead we verify each dependency and, if something is missing,
# print exactly what to do and stop. Everything we DO own (file copy, launcher
# install, vendoring the engine) is idempotent: re-running install.sh is always safe.
#
# Usage:  ./install.sh            # install / re-install
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

# node: the bundled fork engine is driven in-process via the Node shim
# engine/run-lane.mjs, so node is REQUIRED (no upstream reasonix).
NODE_BIN="$(command -v node 2>/dev/null || true)"
[ -n "$NODE_BIN" ] \
  || die "node not found. The engine runs on Node — install Node 18+ (https://nodejs.org) and re-run."
ok "node — $NODE_BIN"

# DeepSeek auth: the engine authenticates with DEEPSEEK_API_KEY, or falls back to
# ~/.reasonix/config.json (the DeepSeek login). One of them must be present.
if [ -n "${DEEPSEEK_API_KEY:-}" ]; then
  ok "DeepSeek auth — DEEPSEEK_API_KEY (env)"
elif [ -f "$HOME/.reasonix/config.json" ]; then
  ok "DeepSeek auth — ~/.reasonix/config.json"
else
  die "No DeepSeek credential. Set DEEPSEEK_API_KEY, or log in so ~/.reasonix/config.json exists, then re-run."
fi

# ----------------------------------------------------------------------------
say "2/5  Installing the fleet into $INSTALL_HOME"
# ----------------------------------------------------------------------------
mkdir -p "$INSTALL_HOME/hooks" "$INSTALL_HOME/runtime/logs" "$INSTALL_HOME/state" "$INSTALL_HOME/engine"
# Copy the shipped components (NOT scratch: runtime ledgers/ports, .git, .superpowers).
for item in \
  reasonix-native-gateway.py reasonix-fleet-mcp.py \
  bridge-settings.json system-prompt-reasonix.md ; do
  cp -f "$SRC/$item" "$INSTALL_HOME/$item"
done
cp -f "$SRC/hooks/"*.py "$INSTALL_HOME/hooks/"
# The one-shot Node shim + its sibling modules (lane-opts.mjs, etc.) that drive the
# in-process engine. Copy the WHOLE engine dir, not just run-lane.mjs — run-lane.mjs
# imports sibling modules (e.g. ./lane-opts.mjs), and copying only run-lane.mjs leaves
# those imports dangling and breaks every lane at runtime.
cp -f "$SRC/engine/"*.mjs "$INSTALL_HOME/engine/"
# The bundled fork engine (self-contained dist + tree-sitter grammars + tokenizer
# data). This IS the DeepSeek engine — copy it verbatim; no build, no npm install.
rm -rf "$INSTALL_HOME/vendor/reasonix-engine"
mkdir -p "$INSTALL_HOME/vendor"
cp -R "$SRC/vendor/reasonix-engine" "$INSTALL_HOME/vendor/reasonix-engine"
[ -f "$INSTALL_HOME/vendor/reasonix-engine/dist/index.js" ] \
  || die "bundled engine missing after copy: $INSTALL_HOME/vendor/reasonix-engine/dist/index.js"
REASONIX_ENGINE_DIST="$INSTALL_HOME/vendor/reasonix-engine/dist/index.js"
# Benches/diagnostics are handy but optional; copy the runtime tooling, not the ledgers.
cp -f "$SRC/runtime/realworld-bench.py" "$INSTALL_HOME/runtime/" 2>/dev/null || true
cp -f "$SRC/runtime/cross-workflow-bench.py" "$INSTALL_HOME/runtime/" 2>/dev/null || true
ok "fleet files + bundled engine copied"

# ----------------------------------------------------------------------------
say "3/5  Installing the launcher into $BIN_DIR/$LAUNCHER_NAME"
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
say "4/5  Smoke-checking the install"
# ----------------------------------------------------------------------------
# The launcher's own doctor validates the wired-up install (files present, settings
# valid, node + bundled engine + shim present, DeepSeek auth resolvable).
if CLAUDE_REASONIX_FLEET_INSTALL_HOME="$INSTALL_HOME" REASONIX_ENGINE_DIST="$REASONIX_ENGINE_DIST" \
   "$BIN_DIR/$LAUNCHER_NAME" doctor >/tmp/claude-reasonix-doctor.$$ 2>&1; then
  ok "launcher doctor passed"
else
  warn "launcher 'doctor' reported issues (see /tmp/claude-reasonix-doctor.$$):"
  sed 's/^/      /' "/tmp/claude-reasonix-doctor.$$" || true
fi
rm -f "/tmp/claude-reasonix-doctor.$$" 2>/dev/null || true

# ----------------------------------------------------------------------------
say "5/5  Done"
# ----------------------------------------------------------------------------
ok "Installed. Start a reasonix session with:"
printf '      %s "your prompt"        # or just: %s\n' "$LAUNCHER_NAME" "$LAUNCHER_NAME"
printf '      %s on                   # enable fleet mode, then run claude normally\n' "$LAUNCHER_NAME"
echo
echo "  Reinstall/upgrade: re-run ./install.sh (safe, idempotent)."
echo "  The DeepSeek engine is bundled — no upstream reasonix to install or patch."
echo "  Uninstall: ./uninstall.sh"
