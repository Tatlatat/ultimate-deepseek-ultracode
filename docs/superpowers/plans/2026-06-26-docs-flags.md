# Docs + Flags (Sub-project C) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** README surfaces only the ~8-10 user flags + reflects the post-refactor package; `docs/CONFIGURATION.md` documents every flag (generated from source); a clean-clone install/uninstall test proves a stranger's flow works in isolation.

**Architecture:** Docs + one isolated test. No behavior/code change to gateway/engine/levers. The flag doc is GENERATED from the real source (grep env_* call sites) so it can't invent flags.

**Tech Stack:** Markdown, bash, python3.

## Global Constraints
- No flag REMOVAL — every flag keeps its code; this sub-project only organizes the DOCS (README surfaces ~8-10; CONFIGURATION.md documents all).
- Every flag/command a doc mentions MUST exist in the source — grep to confirm, invent nothing.
- The clean-clone test MUST use a TEMP install home + temp bin dir (via CLAUDE_REASONIX_FLEET_INSTALL_HOME + CLAUDE_REASONIX_BIN_DIR env) — it must NEVER touch the user's real ~/.claude/reasonix-fleet or ~/.local/bin.
- Offline: no DeepSeek/network needed for any of this (install doctor is a structural check).
- The 5 promoted levers (default-ON): READ_SUMMARY, READER_BROADEN, READ_RETRY_HOLLOW, LANE_FAIL_MARKER, OVERSCOPE_REJECT.

---

### Task 1: Generate the full flag inventory (the source of truth for C2)

**Files:**
- Create: `docs/CONFIGURATION.md`

**Interfaces:**
- Produces: a complete, source-accurate flag reference. The flag list is EXTRACTED from the code, not hand-written.

- [ ] **Step 1: Extract every flag + its default from the source**

Run this to get the raw inventory (flag name + default + which file uses it):
```bash
cd /Users/tatlatat/.claude/codex-fleet
grep -rhnoE 'env_(first|int|float|truthy)\("CLAUDE_REASONIX_[A-Z_]+"[^)]*\)' reasonix_gateway/*.py hooks/*.py 2>/dev/null | sort -u > /tmp/flags-raw.txt
grep -rhoE 'CLAUDE_REASONIX_[A-Z_]+' reasonix_gateway/*.py hooks/*.py bin/claude-reasonix 2>/dev/null | sort -u > /tmp/flags-names.txt
wc -l /tmp/flags-names.txt
```
This gives the authoritative list. For each flag, read its call site to get the real `default=` value and a one-line effect (read the function it gates).

- [ ] **Step 2: Write `docs/CONFIGURATION.md`** grouped by area, every flag with its REAL default + one-line effect. Structure:

```markdown
# Configuration — claude-reasonix

Every `CLAUDE_REASONIX_*` variable has a `CLAUDE_CODEX_*` backward-compat alias.
All levers default OFF unless marked **default-ON**; the system is byte-identical /
inert when a lever is off. You do NOT need any of these for normal use — see the
README's Configuration section for the handful that matter.

## User-facing (the ones you might actually set)
| Flag | Default | Effect |
|---|---|---|
| `DEEPSEEK_API_KEY` | (from ~/.reasonix/config.json) | DeepSeek auth |
| `CLAUDE_REASONIX_GATEWAY_LANE_HARNESS` | 0 | turn on the hard-task retry harness |
| ... (the ~8-10) |

## Promoted levers (default-ON, measured)
| `CLAUDE_REASONIX_GATEWAY_READ_SUMMARY` | 1 | read lanes return a short summary, not raw file |
| `CLAUDE_REASONIX_GATEWAY_READER_BROADEN` | 1 | ... |
| ... (the 5) |

## Cache / prefix
| keepalive, prime-gate, prime-serial, prefix-trace, read-cache flags ... |

## Harness
| LANE_HARNESS, LANE_BUDGET_USD, LANE_MAX_ATTEMPTS, LANE_FAIL_MARKER ... |

## Experimental levers
| preindex, prefetch, output-discipline, overscope, mapreduce ... |

## Internal / diagnostic
| CWD, TIMEOUT, MAX_ITER_PER_TURN, PRIME_DICT_CAP, traces ... |
```
Fill EVERY flag from the source inventory. Use the real default from the call site. If a flag's default or effect is unclear, read the gating function — do not guess.

- [ ] **Step 3: Verify completeness — every source flag is documented**

Run:
```bash
# every flag name in source:
grep -rhoE 'CLAUDE_REASONIX_[A-Z_]+' reasonix_gateway/*.py hooks/*.py | sort -u > /tmp/src-flags.txt
# every flag name in the doc:
grep -oE 'CLAUDE_REASONIX_[A-Z_]+' docs/CONFIGURATION.md | sort -u > /tmp/doc-flags.txt
echo "in source but NOT in doc (should be empty):"; comm -23 /tmp/src-flags.txt /tmp/doc-flags.txt
echo "in doc but NOT in source (invented — should be empty):"; comm -13 /tmp/src-flags.txt /tmp/doc-flags.txt
```
Expected: BOTH lists empty (doc covers every source flag, invents none). Some source "flags" are internal CWD/path vars that may be intentionally grouped — if a name is in source-but-not-doc, add it to the Internal section; if doc-but-not-source, remove it (it was invented).

- [ ] **Step 4: Commit**

```bash
git add -f docs/CONFIGURATION.md
git commit -m "docs: add CONFIGURATION.md — full flag reference (generated from source)"
```

---

### Task 2: Update the README (C1)

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: `docs/CONFIGURATION.md` (Task 1) — links to it.

- [ ] **Step 1: Fix the Layout section** to reflect the post-refactor structure. Find the line `reasonix-native-gateway.py   local Anthropic-compatible gateway ...` and update it to note it's now an 11-line shim + add the package:
```
reasonix-native-gateway.py   thin shim (kept for the stable import path)
reasonix_gateway/            the gateway package: env, text, harness, cost,
                             levers, engine_seam, server modules
```
(Match the existing Layout formatting.)

- [ ] **Step 2: Add a Configuration section** (after Quick start or Defaults) listing ONLY the ~8-10 user flags, each one line with default. Include: the MCP settings already in Defaults (model/reasoning/workers — keep or reference), `DEEPSEEK_API_KEY`, fleet on/off, `CLAUDE_REASONIX_GATEWAY_LANE_HARNESS=1` (hard-task harness), and the 5 promoted levers (one line each, "default-on"). Do NOT list the ~70 internal flags.

- [ ] **Step 3: Add an Advanced configuration pointer** (short paragraph, near the end):
```markdown
## Advanced configuration
≈70 further internal/experimental levers exist (cache-tuning, prime-gate, prefetch,
output-discipline, etc.) — all default-OFF and not needed for normal use. See
[docs/CONFIGURATION.md](docs/CONFIGURATION.md) for the full reference.
```

- [ ] **Step 4: Verify no invented flag/command** — every `CLAUDE_REASONIX_*` and every command the README mentions exists in the source:
```bash
for f in $(grep -oE 'CLAUDE_REASONIX_[A-Z_]+' README.md | sort -u); do
  grep -rq "$f" reasonix_gateway/ hooks/ bin/ engine/ 2>/dev/null || echo "INVENTED: $f"
done; echo "check done"
```
Expected: `check done` with NO `INVENTED:` lines.

- [ ] **Step 5: Commit**

```bash
git add -f README.md
git commit -m "docs(readme): surface ~8-10 user flags + post-refactor layout + advanced pointer"
```

---

### Task 3: Clean-clone install/uninstall test (C3)

**Files:**
- Create: `tests/test-clean-install.sh`

**Interfaces:**
- Consumes: `install.sh`, `uninstall.sh` (both honor `CLAUDE_REASONIX_FLEET_INSTALL_HOME` + `CLAUDE_REASONIX_BIN_DIR` env — verified).

- [ ] **Step 1: Write `tests/test-clean-install.sh`** — installs into a TEMP home, asserts doctor + package present, uninstalls, asserts clean. NEVER touches the real install:

```bash
#!/usr/bin/env bash
# Clean-clone install/uninstall test: proves a stranger can install + uninstall.
# Uses a TEMP install home + temp bin dir (env-overridden) so it NEVER touches the
# user's real ~/.claude/reasonix-fleet or ~/.local/bin. Offline (structural check).
set -u
cd "$(dirname "$0")/.." || exit 2
SRC="$PWD"
TMP="$(mktemp -d)"
export CLAUDE_REASONIX_FLEET_INSTALL_HOME="$TMP/install-home"
export CLAUDE_REASONIX_BIN_DIR="$TMP/bin"
fail() { echo "  FAIL $1"; rm -rf "$TMP"; exit 1; }

echo "=== install into temp home $CLAUDE_REASONIX_FLEET_INSTALL_HOME ==="
bash "$SRC/install.sh" >/tmp/cleaninstall.$$ 2>&1 || { cat /tmp/cleaninstall.$$; fail "install.sh exited non-zero"; }
# the A-refactor package must have landed (the gap that was fixed)
[ -f "$CLAUDE_REASONIX_FLEET_INSTALL_HOME/reasonix_gateway/__init__.py" ] || fail "reasonix_gateway package not copied to install home"
[ -f "$CLAUDE_REASONIX_FLEET_INSTALL_HOME/reasonix-native-gateway.py" ] || fail "gateway shim not copied"
[ -x "$CLAUDE_REASONIX_BIN_DIR/claude-reasonix" ] || fail "launcher not installed"
# install.sh already ran doctor; if it exited 0 above, doctor passed (it warns, not fails) — re-run doctor explicitly:
"$CLAUDE_REASONIX_BIN_DIR/claude-reasonix" doctor >/tmp/cleandoctor.$$ 2>&1 || echo "  (doctor reported warnings — see /tmp/cleandoctor.$$; non-fatal)"

echo "=== uninstall ==="
yes 2>/dev/null | bash "$SRC/uninstall.sh" >/tmp/cleanuninstall.$$ 2>&1 || true
[ ! -d "$CLAUDE_REASONIX_FLEET_INSTALL_HOME" ] || fail "install home still present after uninstall"
[ ! -e "$CLAUDE_REASONIX_BIN_DIR/claude-reasonix" ] || fail "launcher still present after uninstall"

echo "=== PASS: clean-clone install + uninstall ==="
rm -rf "$TMP" /tmp/cleaninstall.$$ /tmp/cleandoctor.$$ /tmp/cleanuninstall.$$ 2>/dev/null
exit 0
```
NOTE on uninstall prompt: check uninstall.sh — if it prompts for confirmation, pipe `yes` (as above) or pass its non-interactive flag (read uninstall.sh for a `-y`/`--force`/`--yes` flag and use it instead of `yes |` if one exists). Verify the test's INSTALL_HOME env actually redirects install.sh (it reads CLAUDE_REASONIX_FLEET_INSTALL_HOME at line ~20 — confirmed).

- [ ] **Step 2: Run it — must pass without touching the real install**

Run: `bash tests/test-clean-install.sh; echo "exit=$?"`
Expected: `=== PASS: clean-clone install + uninstall ===`, `exit=0`. CONFIRM the real install is untouched: `ls ~/.claude/reasonix-fleet >/dev/null 2>&1 && echo "real install still there (good — test used temp)"`.

- [ ] **Step 3: Decide CI inclusion** — if the test is fast (<60s) and fully isolated (temp home, never the real install), it's safe for run-all. Run it once more to time it. If it mutates anything outside the temp dir, do NOT add to run-all. If safe, no change needed (run-all globs `tests/test-*.sh` so it's auto-included — verify it doesn't break run-all):
```bash
bash tests/run-all.sh 2>&1 | tail -3
```
Expected: summary still all-passed (now +1 test = 51), exit 0. If the clean-install test is too slow or touches the real install, rename it so run-all's `test-*.sh` glob skips it (e.g. `clean-install.sh` without the `test-` prefix) and document why.

- [ ] **Step 4: Commit**

```bash
git add -f tests/test-clean-install.sh
git commit -m "test(install): clean-clone install/uninstall in an isolated temp home"
```

---

## Notes for the executor
- Task 1 (the flag doc) is the heaviest — the flag list MUST be generated from the source grep, never hand-invented; the completeness check (comm) is the gate.
- The clean-install test MUST stay isolated to a temp home — re-read install.sh/uninstall.sh to confirm both honor the env overrides before trusting the test.
- No flag removed, no behavior changed — this is docs + one test.
