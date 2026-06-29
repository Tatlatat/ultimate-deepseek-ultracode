# Silent-Worker output-style Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Opus orchestrator in a claude-reasonix session talk minimally (run long tool chains silently, speak only the result / a needed question / a warning) by shipping a Claude Code output-style, wired ON-by-default in the reasonix flavor with a `CLAUDE_REASONIX_SILENT=0` escape hatch.

**Architecture:** A markdown output-style file (`output-styles/silent-worker.md`) is installed user-level to `~/.claude/output-styles/`. The launcher's `render_settings()` adds `"outputStyle": "silent-worker"` to the rendered settings it passes via `claude --settings`, unless `CLAUDE_REASONIX_SILENT=0` strips it. Claude Code resolves the style by NAME from `~/.claude/output-styles/`. A missing style silently falls back to normal behavior (fail-safe).

**Tech Stack:** Bash launcher (`bin/claude-reasonix`), embedded Python in `render_settings()`, `install.sh`, a markdown output-style with YAML frontmatter, Python+shell tests.

## Global Constraints

- **Verified mechanism (Claude Code 2.1.195):** `outputStyle` is a style NAME (no extension/path); Claude Code searches `~/.claude/output-styles/` (user, any cwd) and `<cwd>/.claude/output-styles/` (project) — NO arbitrary-path setting exists.
- The style file MUST set `keep-coding-instructions: true` in frontmatter (preserve Claude Code's built-in software-engineering behavior; only cut chatter).
- A non-existent `outputStyle` → SILENT fallback to normal behavior, exit 0 (verified) — never a crash.
- Install the style **user-level: `~/.claude/output-styles/silent-worker.md`** — NOT `$INSTALL_HOME` (Claude Code never looks there).
- Default **ON in reasonix flavor**; `CLAUDE_REASONIX_SILENT=0` is the only OFF switch; plain `claude` and `plain` mode unaffected.
- Pattern parity: the OFF switch is removed from the *rendered* JSON in `render_settings()` (Python step), mirroring how other flags pass through the launcher. No sed surgery on JSON.
- DRY, YAGNI, TDD, frequent commits. No-claim-without-measurement: Task 5 is an A/B measurement gate, not an assertion.
- The repo template (`bridge-settings.json`) holds NO machine paths — it already uses `__INSTALL_HOME__`; do NOT add an absolute path for the style (the style is user-level and referenced by name only).

---

## File Structure

- **Create `output-styles/silent-worker.md`** — the output-style document (frontmatter + §3 boundary body). Single responsibility: define minimal-talk behavior.
- **Modify `bin/claude-reasonix`** — (a) export pass-through for `CLAUDE_REASONIX_SILENT` in the reasonix-flavor block; (b) `render_settings()` injects/strips `outputStyle` based on the SILENT switch.
- **Modify `install.sh`** — copy `output-styles/silent-worker.md` → `~/.claude/output-styles/` (idempotent).
- **Create `tests/test-silent-worker.py`** — unit-tests the render_settings outputStyle injection/strip logic (ON adds the key, `=0` removes it) by replicating the launcher's Python step against the real `bridge-settings.json`, plus asserts the style file exists with the required frontmatter.
- **Modify `tests/test-reasonix-fleet.sh`** — end-to-end: a launcher dry-run (stub `CLAUDE_BIN`) produces a rendered settings file that contains `outputStyle: silent-worker` by default and omits it under `CLAUDE_REASONIX_SILENT=0`.

---

### Task 1: The output-style document

**Files:**
- Create: `output-styles/silent-worker.md`
- Test: `tests/test-silent-worker.py` (frontmatter assertions only — full test file is built in Task 4; Task 1 adds just the existence+frontmatter check)

**Interfaces:**
- Consumes: nothing.
- Produces: a file at repo `output-styles/silent-worker.md` whose YAML frontmatter has `name: silent-worker`, a non-empty `description:`, and `keep-coding-instructions: true`. Later tasks reference the style by the name `silent-worker`.

- [ ] **Step 1: Write the failing test**

Create `tests/test-silent-worker.py` with ONLY this (Task 4 appends more):

```python
#!/usr/bin/env python3
"""Tests for the silent-worker output-style: the file ships with the required
frontmatter (name/description/keep-coding-instructions:true) and the launcher's
render_settings step injects/strips the outputStyle key correctly."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

passed = 0
failed = 0
def check(label, cond):
    global passed, failed
    if cond:
        print(f"  ok   {label}"); passed += 1
    else:
        print(f"  FAIL {label}"); failed += 1

# --- Task 1: the style file exists with the required frontmatter ---
style = ROOT / "output-styles" / "silent-worker.md"
check("silent-worker.md exists", style.is_file())
text = style.read_text(encoding="utf-8") if style.is_file() else ""
# frontmatter is the block between the first two '---' lines
fm = ""
if text.startswith("---"):
    end = text.find("\n---", 3)
    fm = text[3:end] if end != -1 else ""
check("frontmatter has name: silent-worker", "name: silent-worker" in fm)
check("frontmatter has a description", "description:" in fm and len(fm.split("description:")[1].strip()) > 0)
check("frontmatter keeps coding instructions", "keep-coding-instructions: true" in fm)
# the body must encode the KEEP/CUT boundary, not be empty
check("body is non-trivial", len(text) > 400)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 tests/test-silent-worker.py`
Expected: FAIL — `silent-worker.md exists` fails (file not created yet).

- [ ] **Step 3: Create the output-style document**

Create `output-styles/silent-worker.md` with this exact content:

```markdown
---
name: silent-worker
description: Near-silent worker — run long tool chains without narration; speak only the result, a needed question, or a warning.
keep-coding-instructions: true
---

You work in near-silence, like a focused engineer who narrates nothing and shows
results. The user rarely reads prose between tool calls and it wastes their time.

## Stay silent — do NOT generate these

- Pre/post-tool narration: "I'll read X", "Let me scope this", "Now I'll…", "Done, next…".
- Decision reasoning narrated to the screen: "This is a single edit so…", "Per the policy this is a fan-out…".
- Long post-result explanations, analyses, or tables when the user only asked for the outcome.
- Between tools in a long chain (many tools in a row): say nothing at all. Run the chain, then report.

## Always speak — these are mandatory

1. The final RESULT line: short, concrete, with the data. Examples: "Done. Added the
   header comment at `src/main.ts:6`." / "29 occurrences; none need changing." / "All 57 tests pass."
2. A genuinely-needed decision question (use AskUserQuestion when the user's choice changes what you do).
3. A warning, surprise, or risk: a real bug found, an irreversible action you're about to take,
   a failed lane, an unexpected blocker. Never hide these inside silence.
4. A direct explanation when the user explicitly asked for one — explain on request, never volunteer.

## The litmus test for every sentence

Ask: "Would the user act on this sentence, or skip it?" If they'd skip it, don't generate it.
Result, decision-needed, and warning are act-on-able — keep them. Narration and volunteered
reasoning are skippable — cut them.

Keep all of your normal coding ability, tool use, and correctness. This changes only how much you
SAY, never what you DO.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 tests/test-silent-worker.py`
Expected: PASS — all 5 checks ok.

- [ ] **Step 5: Commit**

```bash
git add output-styles/silent-worker.md tests/test-silent-worker.py
git commit -m "feat(silent-worker): add the silent-worker output-style + frontmatter test"
```

---

### Task 2: render_settings injects/strips outputStyle (launcher)

**Files:**
- Modify: `bin/claude-reasonix` — the `render_settings()` function (currently a plain `__INSTALL_HOME__` substitution) and the reasonix-flavor export block.
- Test: covered by Task 4 (`tests/test-silent-worker.py` render-logic checks) and Task 5 (`tests/test-reasonix-fleet.sh` e2e).

**Interfaces:**
- Consumes: `bridge-settings.json` (template), `$INSTALL_HOME`, `$RENDERED_SETTINGS`, env `CLAUDE_REASONIX_SILENT` (and back-compat alias not required — this is a new flag).
- Produces: the rendered settings JSON at `$RENDERED_SETTINGS` contains top-level `"outputStyle": "silent-worker"` UNLESS `CLAUDE_REASONIX_SILENT` is set to `0`/`false`, in which case the key is absent. The decision is made in `render_settings()` via the embedded Python.

- [ ] **Step 1: Read the current render_settings + reasonix-flavor export block**

Run: `sed -n '93,160p' bin/claude-reasonix`
Note the current `render_settings()` (the Python heredoc that replaces `__INSTALL_HOME__`) and the reasonix-flavor block ending around the `DEEPSEEK_API_KEY` pass-through (line ~113).

- [ ] **Step 2: Add the SILENT pass-through export**

In `bin/claude-reasonix`, inside the `if [[ "$CLAUDE_REASONIX_FLAVOR" == "reasonix" ]]` block, right AFTER the two big-read-guard pass-through lines (the `CLAUDE_REASONIX_BIG_READ_THRESHOLD_BYTES` export), add:

```bash
  # Silent-worker output-style (default ON in reasonix). The reasonix session talks
  # minimally — long tool chains run silent; only result / needed-question / warning
  # are spoken. render_settings injects outputStyle:silent-worker unless this is 0.
  # Pass the OFF switch through if the user set it; default needs no export.
  [[ -n "${CLAUDE_REASONIX_SILENT:-}" ]] && export CLAUDE_REASONIX_SILENT
```

- [ ] **Step 3: Rewrite render_settings to inject/strip outputStyle**

Replace the entire `render_settings()` function body's Python heredoc. The new version takes a 4th arg (the SILENT decision) and edits the JSON object, not just the text. Replace:

```bash
render_settings() {
  ensure_dirs
  python3 - "$SETTINGS_FILE" "$INSTALL_HOME" "$RENDERED_SETTINGS" <<'PY'
import sys
src, install_home, dst = sys.argv[1:4]
with open(src, "r", encoding="utf-8") as fh:
    text = fh.read()
text = text.replace("__INSTALL_HOME__", install_home)
with open(dst, "w", encoding="utf-8") as fh:
    fh.write(text)
PY
}
```

with:

```bash
render_settings() {
  ensure_dirs
  # Silent-worker default ON; only "0"/"false" turns it off.
  local silent="on"
  case "${CLAUDE_REASONIX_SILENT:-}" in
    0|false|FALSE|no|NO) silent="off" ;;
  esac
  python3 - "$SETTINGS_FILE" "$INSTALL_HOME" "$RENDERED_SETTINGS" "$silent" <<'PY'
import json
import sys
src, install_home, dst, silent = sys.argv[1:5]
with open(src, "r", encoding="utf-8") as fh:
    text = fh.read()
text = text.replace("__INSTALL_HOME__", install_home)
data = json.loads(text)
# Inject the output-style by NAME (Claude Code resolves it from
# ~/.claude/output-styles/silent-worker.md). Default ON; CLAUDE_REASONIX_SILENT=0
# strips the key so behavior reverts. A missing style file -> Claude Code silently
# falls back to normal behavior (verified), so this is fail-safe.
if silent == "on":
    data["outputStyle"] = "silent-worker"
else:
    data.pop("outputStyle", None)
with open(dst, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2)
    fh.write("\n")
PY
}
```

- [ ] **Step 4: Verify the launcher still renders valid JSON (manual smoke)**

Run:
```bash
CLAUDE_REASONIX_FLEET_INSTALL_HOME=/tmp/swtest bash -c '
  set -e
  HOME_DIR=$(mktemp -d)
  export CLAUDE_REASONIX_FLEET_INSTALL_HOME=$(pwd)
  # render with default (ON)
  ./bin/claude-reasonix status >/dev/null 2>&1 || true
'
echo "smoke: render_settings is exercised by the e2e test in Task 5"
```
Expected: no crash. (Full assertions live in Task 4/5; this is just a syntax smoke.)

- [ ] **Step 5: Commit**

```bash
git add bin/claude-reasonix
git commit -m "feat(silent-worker): render_settings injects outputStyle (default ON, CLAUDE_REASONIX_SILENT=0 off)"
```

---

### Task 3: install.sh copies the style to ~/.claude/output-styles/

**Files:**
- Modify: `install.sh` — the "2/5 Installing the fleet" section (after the hooks/engine copies, before the `ok "fleet files…"` line).
- Test: covered by Task 4 (a check that the install copy block targets the user-level dir, by static assertion on install.sh text — we do not run a real install in CI).

**Interfaces:**
- Consumes: `$SRC/output-styles/silent-worker.md` (the repo file from Task 1).
- Produces: after install, `~/.claude/output-styles/silent-worker.md` exists. The install step is idempotent (overwrite).

- [ ] **Step 1: Add the failing test (append to tests/test-silent-worker.py)**

Append this block to `tests/test-silent-worker.py` BEFORE the final `print(...)`/`sys.exit(...)` lines:

```python
# --- Task 3: install.sh copies the style to the user-level output-styles dir ---
install_sh = (ROOT / "install.sh").read_text(encoding="utf-8")
check("install copies silent-worker.md", "output-styles/silent-worker.md" in install_sh)
check("install targets user-level ~/.claude/output-styles", ".claude/output-styles" in install_sh)
# guard against the spec footgun: it must NOT be installed into INSTALL_HOME (Claude
# Code never looks there for output styles)
check("install does NOT put the style under INSTALL_HOME",
      '"$INSTALL_HOME/output-styles' not in install_sh)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 tests/test-silent-worker.py`
Expected: FAIL — the two install-copy checks fail (install.sh not edited yet).

- [ ] **Step 3: Add the install copy step**

In `install.sh`, immediately AFTER the line `cp -f "$SRC/engine/"*.mjs "$INSTALL_HOME/engine/"` and its comment block (before the `reasonix_gateway` package copy), add:

```bash
# The silent-worker output-style. Claude Code resolves output styles by NAME from
# ~/.claude/output-styles/ (user-level, found from any cwd) — NOT from INSTALL_HOME —
# so install it there, not into the fleet home. Idempotent overwrite. A missing style
# would make Claude Code silently fall back to normal behavior, so this is fail-safe.
USER_OUTPUT_STYLES="$HOME/.claude/output-styles"
mkdir -p "$USER_OUTPUT_STYLES"
cp -f "$SRC/output-styles/silent-worker.md" "$USER_OUTPUT_STYLES/silent-worker.md"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 tests/test-silent-worker.py`
Expected: PASS — all install checks ok.

- [ ] **Step 5: Commit**

```bash
git add install.sh tests/test-silent-worker.py
git commit -m "feat(silent-worker): install the output-style into ~/.claude/output-styles"
```

---

### Task 4: render_settings unit test (ON injects, OFF strips)

**Files:**
- Modify: `tests/test-silent-worker.py` — append render-logic checks that replicate the launcher's embedded Python against the real `bridge-settings.json`.

**Interfaces:**
- Consumes: `bridge-settings.json` (template), the render decision logic mirrored from Task 2's Python.
- Produces: a self-contained unit test proving the injected JSON has `outputStyle: silent-worker` when ON and lacks it when OFF, AND that the rendered JSON is still valid and preserves the existing hooks block.

- [ ] **Step 1: Write the failing test (append to tests/test-silent-worker.py)**

Append BEFORE the final `print(...)`/`sys.exit(...)`:

```python
# --- Task 4: the render_settings decision (mirror of the launcher Python) ---
import json

def render(silent_on: bool):
    text = (ROOT / "bridge-settings.json").read_text(encoding="utf-8")
    text = text.replace("__INSTALL_HOME__", "/tmp/fake-install-home")
    data = json.loads(text)
    if silent_on:
        data["outputStyle"] = "silent-worker"
    else:
        data.pop("outputStyle", None)
    return data

on = render(True)
off = render(False)
check("ON: rendered settings name the style", on.get("outputStyle") == "silent-worker")
check("OFF: rendered settings omit outputStyle", "outputStyle" not in off)
# the existing hooks block must survive the JSON round-trip both ways
check("ON: hooks preserved", "hooks" in on and "PreToolUse" in on["hooks"])
check("OFF: hooks preserved", "hooks" in off and "PreToolUse" in off["hooks"])
# placeholder must be substituted, not left raw, in the rendered hook commands
on_text = json.dumps(on)
check("ON: __INSTALL_HOME__ substituted", "__INSTALL_HOME__" not in on_text)
```

- [ ] **Step 2: Run test to verify it passes**

Run: `python3 tests/test-silent-worker.py`
Expected: PASS — the render checks pass because Task 2's logic and this mirror agree. (If it fails, the launcher Python and this mirror have diverged — fix the launcher to match this contract: ON sets `outputStyle="silent-worker"`, OFF pops it.)

Note: this is a TDD inversion by design — the production logic (Task 2) already exists, so this test should pass immediately and acts as a regression lock on the contract. If you are doing Task 4 before Task 2, it will fail at the `render` import-equivalence; do Task 2 first.

- [ ] **Step 3: (no new production code — test-only task)**

This task adds no implementation; it locks the render contract. Skip to commit.

- [ ] **Step 4: Run the full silent-worker test once more**

Run: `python3 tests/test-silent-worker.py`
Expected: PASS — all checks (Task 1 + 3 + 4 blocks) green.

- [ ] **Step 5: Commit**

```bash
git add tests/test-silent-worker.py
git commit -m "test(silent-worker): lock the render_settings outputStyle contract (ON injects, OFF strips)"
```

---

### Task 5: end-to-end launcher wiring test + full suite

**Files:**
- Modify: `tests/test-reasonix-fleet.sh` — add a block that dry-runs the launcher with a stub `CLAUDE_BIN` and asserts the rendered settings file the launcher passes to `--settings` carries `outputStyle` by default and omits it under `CLAUDE_REASONIX_SILENT=0`.

**Interfaces:**
- Consumes: the launcher's `--settings "$RENDERED_SETTINGS"` arg (already produced by `run_claude_with_fleet`), the rendered file path.
- Produces: an e2e assertion that the WHOLE chain (env → render_settings → rendered file) yields the right `outputStyle`.

- [ ] **Step 1: Inspect how the existing fleet test captures launcher args**

Run: `sed -n '344,367p' tests/test-reasonix-fleet.sh`
Note: `bare_output="$(CLAUDE_REASONIX_NATIVE_SUBAGENTS=0 "$LAUNCHER" "bare prompt")"` captures the stub-claude args, and `--settings <path>` is among them. The test environment must define a stub `CLAUDE_BIN` that echoes its args — confirm how `CLAUDE_BIN` is set in this test (search `CLAUDE_BIN` in the file). If a stub already exists, reuse it; if the launcher execs real `claude`, set `CLAUDE_BIN` to a stub script that does `printf '%s\n' "$@"`.

- [ ] **Step 2: Add the failing e2e assertions**

In `tests/test-reasonix-fleet.sh`, AFTER the existing `bare_output=...` assertions block (right after the `grep -q "bare prompt" <<<"$bare_output"` line), add:

```bash
# --- silent-worker: the rendered settings carry outputStyle by default ---
# Pull the --settings path out of the launcher's args, then read that file.
sw_settings_path="$(sed -n 's/.*--settings \([^ ]*\).*/\1/p' <<<"$bare_output" | head -1)"
[[ -f "$sw_settings_path" ]] || fail "silent-worker: could not find rendered settings path in launcher args"
grep -q '"outputStyle": "silent-worker"' "$sw_settings_path" \
  || fail "silent-worker: default reasonix session should set outputStyle:silent-worker"

# --- silent-worker OFF switch: CLAUDE_REASONIX_SILENT=0 strips the key ---
off_output="$(CLAUDE_REASONIX_NATIVE_SUBAGENTS=0 CLAUDE_REASONIX_SILENT=0 "$LAUNCHER" "bare prompt")"
off_settings_path="$(sed -n 's/.*--settings \([^ ]*\).*/\1/p' <<<"$off_output" | head -1)"
[[ -f "$off_settings_path" ]] || fail "silent-worker: could not find rendered settings path (OFF run)"
if grep -q '"outputStyle"' "$off_settings_path"; then
  fail "silent-worker: CLAUDE_REASONIX_SILENT=0 should remove outputStyle from rendered settings"
fi
```

- [ ] **Step 3: Run the fleet test to verify the new block passes**

Run: `bash tests/test-reasonix-fleet.sh`
Expected: PASS (prints its existing PASS line and exits 0). If the `--settings` path extraction yields empty, adjust the `sed` to match the actual arg format seen in `bare_output` (run `echo "$bare_output"` to inspect). The launcher uses `--settings "$RENDERED_SETTINGS"` with a space, so the `sed` above matches.

- [ ] **Step 4: Run the FULL suite — no regressions**

Run: `bash tests/run-all.sh`
Expected: `=== summary: N passed, 0 failed ===` where N is the prior count + the new `test-silent-worker.py`. The byte-identical engine-seam test and all existing tests stay green (we only added an output-style + launcher render logic + install copy + tests).

- [ ] **Step 5: Commit**

```bash
git add tests/test-reasonix-fleet.sh
git commit -m "test(silent-worker): e2e — rendered settings carry outputStyle, OFF switch strips it"
```

---

### Task 6: A/B measurement gate (no-claim-without-measurement)

**Files:**
- No production code. Produces a measurement record only. Uses the existing isolated sandbox loop rig (the conductor-loop rig under `$CLAUDE_JOB_DIR/tmp/conductor-loop/` or an equivalent isolated install — do NOT touch the user's live `~/.claude/reasonix-fleet` for this experiment; use an isolated INSTALL_HOME so other live sessions are unaffected).

**Interfaces:**
- Consumes: a fixed small set of tasks (reuse `tasks-10.md` from the conductor-loop rig, or a 3-task subset for speed) run through a real reasonix session twice — once with `CLAUDE_REASONIX_SILENT=0` (OFF) and once default (ON).
- Produces: a measurement comparing (a) assistant text-block count (chatter), (b) total assistant output tokens, and (c) a KEEP-set audit confirming the final result line, any warnings, and any decision-questions still appear under ON.

- [ ] **Step 1: Set up an isolated install for the A/B**

Run (isolated home so live sessions are untouched):
```bash
SW_HOME="$CLAUDE_JOB_DIR/tmp/silent-ab/fleet-install"
mkdir -p "$SW_HOME"
CLAUDE_REASONIX_FLEET_INSTALL_HOME="$SW_HOME" CLAUDE_REASONIX_SKIP_CLAUDE_CHECK=1 ./install.sh
ls "$HOME/.claude/output-styles/silent-worker.md"
```
Expected: install completes; the style file exists at the user-level path. (Note: the style is user-level and shared; that is fine — it is inert unless a session names it, and only the reasonix rendered settings do.)

- [ ] **Step 2: Run the OFF arm**

Drive the isolated reasonix session over the task set with `CLAUDE_REASONIX_SILENT=0`. Capture the session transcript JSONL path.
Record into `$CLAUDE_JOB_DIR/tmp/silent-ab/off.txt`:
- chatter = count of assistant message blocks containing non-empty text (not tool_use only),
- output tokens = sum of assistant output tokens from the transcript usage records.

- [ ] **Step 3: Run the ON arm**

Repeat the identical task set with the default (ON — do not set the OFF switch). Capture to `$CLAUDE_JOB_DIR/tmp/silent-ab/on.txt` the same two numbers, PLUS a manual KEEP audit: confirm each task's final result line is present, and that any warning/decision-question that appeared in the OFF arm also appears in the ON arm.

- [ ] **Step 4: Decide promote/hold**

Compare. Promote (keep default ON) ONLY if: chatter count AND output tokens drop substantially under ON, AND zero KEEP-class messages were lost (every result line / warning / decision-question still present). If a KEEP-class message was suppressed, the style body is over-aggressive — revise Task 1's body to strengthen the KEEP list and re-measure. Write the verdict + the four numbers + the KEEP audit to a memory file `reasonix-silent-worker-ab.md` (and a one-line MEMORY.md pointer).

- [ ] **Step 5: Commit the measurement record (docs only)**

```bash
git add docs/superpowers/specs/2026-06-28-silent-worker-design.md
git commit -m "docs(silent-worker): record A/B measurement verdict (chatter/tokens/KEEP-audit)"
```
(If the spec needs no edit, skip the commit; the measurement lives in memory + the A/B txt files.)

---

## Self-Review

**1. Spec coverage:**
- §1 Problem (3 narration types) → encoded in Task 1's style body CUT list. ✓
- §2 why output-style not hook → Global Constraints + Task 1 frontmatter `keep-coding-instructions: true`. ✓
- §3 CUT/KEEP boundary + litmus → Task 1 body verbatim. ✓
- §4 Architecture (file user-level, render injection, OFF switch, install copy) → Tasks 1/2/3, with the corrected user-level install path. ✓
- §5 Measurement → Task 6 (A/B gate, isolated install). ✓
- §6 Risks (over-silence, ignored mid-task, hidden stall, bleed) → KEEP list (Task 1), A/B audit (Task 6), missing-style fail-safe (Constraints), scoped to rendered settings (Task 2). ✓
- §7 Scope (no hook trimming, one style, default ON) → respected; no hook task. ✓
- §8 Success criteria → Task 5 (e2e ON/OFF) + Task 6 (measured drop, KEEP intact, revert via =0, plain untouched). ✓

**2. Placeholder scan:** No TBD/TODO; every code step has full content; the style body, the render Python, the install block, and all test code are complete. Task 6 is inherently a measurement task (no code) — its steps name exact files and the decision rule, not "measure somehow". ✓

**3. Type/name consistency:** style name `silent-worker` is identical across Task 1 (file/frontmatter), Task 2 (`data["outputStyle"]="silent-worker"`), Task 3 (copy target `silent-worker.md`), Task 4 (`== "silent-worker"`), Task 5 (`grep '"outputStyle": "silent-worker"'`). Env flag `CLAUDE_REASONIX_SILENT` identical across Task 2 export + render + Task 5 OFF run. Install user-level path `~/.claude/output-styles/` consistent in Task 3 code + test + Global Constraints. ✓

One ordering note baked into the plan: Task 4 (render contract test) assumes Task 2's launcher logic exists; the plan flags this and says do Task 2 first. Task 5's `--settings` extraction matches the launcher's actual `--settings "$RENDERED_SETTINGS"` form.
