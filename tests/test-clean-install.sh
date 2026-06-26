#!/usr/bin/env bash
# Clean-clone install/uninstall test: proves a stranger can install + uninstall.
# Uses a TEMP install home + temp bin dir (env-overridden) so it NEVER touches the
# user's real ~/.claude/reasonix-fleet or ~/.local/bin. Offline (structural check).
#
# Safety: both install.sh and uninstall.sh fully honour CLAUDE_REASONIX_FLEET_INSTALL_HOME
# and CLAUDE_REASONIX_BIN_DIR (confirmed — no hardcoded paths in either script).
# We pass --purge to uninstall.sh so the entire temp home is removed (without --purge,
# uninstall.sh keeps the directory and only removes individual files).
set -u
cd "$(dirname "$0")/.." || exit 2
SRC="$PWD"
TMP="$(mktemp -d)"
export CLAUDE_REASONIX_FLEET_INSTALL_HOME="$TMP/install-home"
export CLAUDE_REASONIX_BIN_DIR="$TMP/bin"
# Also forward the engine dist path so the launcher's doctor can find it without
# relying on the real install home.
export REASONIX_ENGINE_DIST="$CLAUDE_REASONIX_FLEET_INSTALL_HOME/vendor/reasonix-engine/dist/index.js"
# Provide a dummy DeepSeek key so install.sh's auth step takes the env branch.
# This test checks the install STRUCTURE, not auth — without a key, install.sh's
# first-run key prompt would `die` on a key-less, non-TTY runner. A real CI install
# would set DEEPSEEK_API_KEY too. No network happens here; the dummy is never used.
export DEEPSEEK_API_KEY="${DEEPSEEK_API_KEY:-sk-clean-install-test-dummy}"

# Cleanup on exit (success or failure) — this is the safety net that guarantees
# the temp dir is always removed even if a test assertion aborts early.
cleanup() { rm -rf "$TMP" /tmp/cleaninstall.$$ /tmp/cleandoctor.$$ /tmp/cleanuninstall.$$ 2>/dev/null || true; }
trap cleanup EXIT

fail() { echo "  FAIL $1"; exit 1; }

echo "=== install into temp home $CLAUDE_REASONIX_FLEET_INSTALL_HOME ==="
bash "$SRC/install.sh" >/tmp/cleaninstall.$$ 2>&1 || { cat /tmp/cleaninstall.$$; fail "install.sh exited non-zero"; }

# The A-refactor package must have landed (the gap that was fixed).
[ -f "$CLAUDE_REASONIX_FLEET_INSTALL_HOME/reasonix_gateway/__init__.py" ] \
  || fail "reasonix_gateway package not copied to install home"
[ -f "$CLAUDE_REASONIX_FLEET_INSTALL_HOME/reasonix-native-gateway.py" ] \
  || fail "gateway shim not copied"
[ -x "$CLAUDE_REASONIX_BIN_DIR/claude-reasonix" ] \
  || fail "launcher not installed"

# Re-run doctor explicitly (install.sh ran it already and exited 0, but be explicit).
"$CLAUDE_REASONIX_BIN_DIR/claude-reasonix" doctor >/tmp/cleandoctor.$$ 2>&1 \
  || echo "  (doctor reported warnings — see /tmp/cleandoctor.$$; non-fatal)"

echo "=== uninstall (--purge so the install home dir is fully removed) ==="
bash "$SRC/uninstall.sh" --purge >/tmp/cleanuninstall.$$ 2>&1 || true

# After --purge the entire install home must be gone.
[ ! -d "$CLAUDE_REASONIX_FLEET_INSTALL_HOME" ] \
  || fail "install home still present after uninstall --purge"
[ ! -e "$CLAUDE_REASONIX_BIN_DIR/claude-reasonix" ] \
  || fail "launcher still present after uninstall"

echo "=== PASS: clean-clone install + uninstall ==="
exit 0
