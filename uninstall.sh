#!/usr/bin/env bash
# claude-reasonix uninstaller — removes what install.sh added. It does NOT touch
# reasonix/claude/node (we never installed those), and by default it LEAVES the
# reasonix ACP patch in place (it is harmless without the launcher). Pass
# --revert-patch to also undo the dist patch.
#
# Usage:  ./uninstall.sh [--revert-patch] [--purge]
#           --revert-patch   undo the reasonix ACP ephemeral-session edit
#           --purge          also delete runtime logs/ledgers/state under INSTALL_HOME
set -euo pipefail

INSTALL_HOME="${CLAUDE_REASONIX_FLEET_INSTALL_HOME:-$HOME/.claude/reasonix-fleet}"
BIN_DIR="${CLAUDE_REASONIX_BIN_DIR:-$HOME/.local/bin}"
LAUNCHER_NAME="claude-reasonix"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

REVERT_PATCH=0
PURGE=0
for arg in "$@"; do
  case "$arg" in
    --revert-patch) REVERT_PATCH=1 ;;
    --purge)        PURGE=1 ;;
    *) printf 'unknown option: %s\n' "$arg" >&2; exit 1 ;;
  esac
done

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  !\033[0m %s\n' "$*"; }

say "Removing the launcher"
if [ -e "$BIN_DIR/$LAUNCHER_NAME" ] || [ -L "$BIN_DIR/$LAUNCHER_NAME" ]; then
  rm -f "$BIN_DIR/$LAUNCHER_NAME"
  ok "removed $BIN_DIR/$LAUNCHER_NAME"
else
  warn "no launcher at $BIN_DIR/$LAUNCHER_NAME"
fi

say "Removing the fleet"
if [ -d "$INSTALL_HOME" ]; then
  if [ "$PURGE" -eq 1 ]; then
    rm -rf "$INSTALL_HOME"
    ok "deleted $INSTALL_HOME (including runtime logs/ledgers/state)"
  else
    # Keep runtime/ and state/ (logs, cost ledgers) unless --purge; remove code.
    for item in \
      reasonix-native-gateway.py reasonix-fleet-mcp.py ccr-claude-proxy.py \
      bridge-settings.json system-prompt-reasonix.md ; do
      rm -f "$INSTALL_HOME/$item"
    done
    rm -rf "$INSTALL_HOME/hooks"
    ok "removed fleet code from $INSTALL_HOME (kept runtime/ and state/ — pass --purge to delete)"
  fi
else
  warn "no fleet at $INSTALL_HOME"
fi

if [ "$REVERT_PATCH" -eq 1 ]; then
  say "Reverting the reasonix ACP ephemeral-session patch"
  if python3 "$SRC/patches/apply_ephemeral.py" --revert 2>/dev/null; then
    ok "patch reverted"
  else
    warn "could not auto-revert the patch (it is harmless; a reasonix upgrade also clears it)"
  fi
fi

say "Done"
ok "claude-reasonix uninstalled."
echo "  reasonix, claude, and node were left untouched (we never installed them)."
[ "$REVERT_PATCH" -eq 0 ] && \
  echo "  The reasonix ACP patch was left in place (harmless). Re-run with --revert-patch to undo it."
