# Conductor Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Opus a hands-off conductor by adding an always-on PreToolUse hook that denies Opus's operator tools (Edit/Write/MultiEdit + clearly-mutating Bash) in fleet mode, with an escalation safety-valve and fail-open, default-OFF behind a flag.

**Architecture:** A new standalone hook `hooks/conductor-guard.py` reads the PreToolUse JSON on stdin and returns exit 2 (deny) for operator tools when (a) the conductor flag is on AND (b) there is no unresolved escalation for the session, else exit 0 (allow). The gateway/harness writes an escalation-ledger line when a lane escalates/fails; the hook reads it. The launcher exports the opt-in flag; bridge-settings.json wires the hook on `Edit|Write|MultiEdit|Bash`. The system-prompt prose is trimmed so structure (not exhortation) carries the policy.

**Tech Stack:** Python 3.8+ (stdlib only — matches existing hooks), bash launcher, JSON settings. Tests are standalone scripts run by `tests/run-all.sh` (Python `test-*.py`, bash `test-*.sh`), the established pattern in this repo.

## Global Constraints

- **Hooks are stdlib-only Python**, read JSON from stdin, exit 0 = allow / exit 2 = deny, deny-reason printed to stderr (match `hooks/only-reasonix-fleet.py`).
- **Default OFF:** every new behavior is inert unless `CLAUDE_REASONIX_GATEWAY_*`/`CLAUDE_REASONIX_CONDUCTOR_REVIEW_ONLY` is explicitly truthy. With the flag unset, behavior is byte-identical to today.
- **Fail-OPEN always:** any uncertainty (unreadable ledger, malformed JSON, ambiguous Bash, unknown state) ⇒ exit 0 (allow). The guard must NEVER wedge the user.
- **Env var precedence:** read `CLAUDE_REASONIX_*` first, fall back to `CLAUDE_CODEX_*` (back-compat), matching every other flag in this repo.
- **Marker/ledger files** live under `$TMPDIR` (fallback `/tmp`), keyed by `session_id`, exactly like `hooks/reasonix-workflow.py:621-627`.
- **No-claim-without-measurement:** the guard ships default-OFF; promotion to default-ON requires a measured A/B (out of scope for this plan — this plan delivers the mechanism + tests, OFF).
- **Tests isolated:** every test uses a temp `$TMPDIR`/temp HOME, never touches real state. `tests/run-all.sh` auto-discovers `test-*.py` / `test-*.sh`.
- **Settings template:** `bridge-settings.json` stays a portable template using the `__INSTALL_HOME__` placeholder (the launcher renders it).

---

## File Structure

- `hooks/conductor-guard.py` — **new.** The PreToolUse guard. Pure decision logic + ledger read. Stdlib only.
- `reasonix_gateway/harness.py` — **modify.** Add a small helper `escalation_ledger_path(session_id)` and write a ledger line when a lane returns a `LANE_ESCALATE`/failed/hollow result. (The `LANE_ESCALATE:` string already exists at `harness.py:61`; we add the *persistence* the hook reads.)
- `bridge-settings.json` — **modify.** Add a `PreToolUse` matcher `Edit|Write|MultiEdit|Bash` → `conductor-guard.py`.
- `bin/claude-reasonix` — **modify.** Export `CLAUDE_REASONIX_CONDUCTOR_REVIEW_ONLY` (pass-through; default unset = OFF) inside the reasonix-flavor block; copy the new hook on install (the install loop already copies `hooks/*.py`, so verify only).
- `system-prompt-reasonix.md` — **modify.** Trim the loophole prose per spec §5.
- `tests/test-conductor-guard.py` — **new.** Unit tests for the hook decision logic (allow/deny/escalation/fail-open/Bash-classification).
- `tests/test-conductor-guard-e2e.sh` — **new.** Drives the real hook as a subprocess with JSON on stdin (mirrors how Claude Code calls it), asserts exit codes.

**Build order:** Task 1 (ledger path helper, pure) → Task 2 (the hook, consumes ledger path) → Task 3 (gateway writes the ledger) → Task 4 (wire settings + launcher flag) → Task 5 (prompt cleanup) → Task 6 (e2e subprocess test). Hook before gateway-write because the hook's read contract defines the file format the gateway must produce.

---

### Task 1: Escalation-ledger path helper (shared contract)

**Files:**
- Modify: `reasonix_gateway/harness.py` (add one function near the top, after the existing imports)
- Test: `tests/test-conductor-guard.py` (new file — start it here with this one test)

**Interfaces:**
- Produces: `escalation_ledger_path(session_id: str | None) -> str | None` — returns the absolute path
  `<tmpdir>/reasonix-conductor-escalations/<session_id>` (tmpdir = `$TMPDIR` or `/tmp`), or `None`
  if `session_id` is falsy. Does NOT create the file. Both the hook (Task 2) and the gateway
  (Task 3) import/replicate this so they agree on the path.

- [ ] **Step 1: Write the failing test**

Create `tests/test-conductor-guard.py` with:

```python
#!/usr/bin/env python3
"""Unit tests for conductor-guard hook + the shared escalation-ledger path."""
import importlib.util
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

passed = 0
failed = 0
def check(label, cond):
    global passed, failed
    if cond:
        print(f"  ok   {label}"); passed += 1
    else:
        print(f"  FAIL {label}"); failed += 1

# --- Task 1: ledger path helper (defined in reasonix_gateway/harness.py) ---
sys.path.insert(0, ROOT)
from reasonix_gateway import harness as _h

with tempfile.TemporaryDirectory() as td:
    os.environ["TMPDIR"] = td
    p = _h.escalation_ledger_path("sess-abc")
    check("ledger path includes session id", p is not None and p.endswith("sess-abc"))
    check("ledger path under tmpdir", p is not None and p.startswith(td))
    check("ledger path in conductor-escalations dir", "reasonix-conductor-escalations" in p)
    check("None session -> None path", _h.escalation_ledger_path(None) is None)
    check("empty session -> None path", _h.escalation_ledger_path("") is None)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 tests/test-conductor-guard.py`
Expected: FAIL with `AttributeError: module 'reasonix_gateway.harness' has no attribute 'escalation_ledger_path'`

- [ ] **Step 3: Add the helper to `reasonix_gateway/harness.py`**

Add near the top of the module (after the existing imports, before `_lane_harness_on`):

```python
import os as _os


def escalation_ledger_path(session_id):
    """Absolute path to the per-session conductor escalation ledger, or None.

    Both the conductor-guard hook (reads) and the gateway (appends) compute this
    the same way so they agree on the file. Does NOT create the file. Returns None
    for a falsy session_id (the caller then fails open — see conductor-guard.py)."""
    if not session_id:
        return None
    tmp = _os.environ.get("TMPDIR") or "/tmp"
    return _os.path.join(tmp, "reasonix-conductor-escalations", str(session_id))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 tests/test-conductor-guard.py`
Expected: PASS — `5 passed, 0 failed`

- [ ] **Step 5: Commit**

```bash
git add reasonix_gateway/harness.py tests/test-conductor-guard.py
git commit -m "feat(conductor): escalation-ledger path helper (shared hook/gateway contract)"
```

---

### Task 2: The conductor-guard hook (decision logic)

**Files:**
- Create: `hooks/conductor-guard.py`
- Test: `tests/test-conductor-guard.py` (extend with the hook-decision tests below)

**Interfaces:**
- Consumes: `escalation_ledger_path(session_id)` from Task 1 (replicated inline in the hook so the
  hook stays stdlib-only and import-free — it must run as a bare `python3 hooks/conductor-guard.py`
  with no package on the path).
- Produces: a CLI hook. Reads PreToolUse JSON `{tool_name, tool_input:{command?,file_path?}, session_id}`
  on stdin. Exit 0 = allow, exit 2 = deny (reason on stderr). Decision function
  `decide(payload) -> tuple[int, str]` is importable for unit tests.

The decision rule (in order):
1. If `CLAUDE_REASONIX_CONDUCTOR_REVIEW_ONLY` (fallback `CLAUDE_CODEX_CONDUCTOR_REVIEW_ONLY`) is NOT
   truthy → allow (exit 0). (Default OFF.)
2. If `tool_name` not in the operator set → allow. Operator set: `Edit`, `Write`, `MultiEdit`, and
   `Bash` ONLY when its command is clearly file-mutating.
3. If an unresolved escalation exists for `session_id` (ledger file exists and is non-empty) → allow
   (the safety valve — Opus may fix the broken lane).
4. Any uncertainty (no session_id, ledger unreadable, malformed JSON) → allow (fail-open).
5. Otherwise → deny (exit 2) with the redirect message.

Bash mutation classification (`bash_mutates(command: str) -> bool`): True if the command contains an
output redirection to a file (`>` or `>>` not inside a quoted string heuristic — keep simple: any
`>` token), `sed -i`, `tee `, a here-doc redirected to a file (`<<` together with `>`), or `perl -i`.
False otherwise (reads, pipes-without-redirect, `git`, test runners, `ls`, `grep`). When in doubt,
return False (fail-open — never block a test run on a guess).

- [ ] **Step 1: Write the failing tests** (append to `tests/test-conductor-guard.py`, before the final summary block)

```python
# --- Task 2: the hook decision logic ---
spec = importlib.util.spec_from_file_location(
    "conductor_guard", os.path.join(ROOT, "hooks", "conductor-guard.py"))
cg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cg)

def decide(payload, env):
    saved = dict(os.environ)
    os.environ.update(env)
    try:
        return cg.decide(payload)
    finally:
        os.environ.clear(); os.environ.update(saved)

ON = {"CLAUDE_REASONIX_CONDUCTOR_REVIEW_ONLY": "1"}

# flag OFF -> always allow (byte-inert default)
code, _ = decide({"tool_name": "Edit", "tool_input": {"file_path": "x.py"}, "session_id": "s1"}, {})
check("flag OFF: Edit allowed", code == 0)

# flag ON, no escalation -> deny Edit/Write/MultiEdit
with tempfile.TemporaryDirectory() as td:
    env = {**ON, "TMPDIR": td}
    for tool in ("Edit", "Write", "MultiEdit"):
        code, msg = decide({"tool_name": tool, "tool_input": {"file_path": "x.py"}, "session_id": "s1"}, env)
        check(f"flag ON, no escalation: {tool} denied", code == 2 and "conductor" in msg.lower())

    # Read/Grep/Glob always allowed
    for tool in ("Read", "Grep", "Glob"):
        code, _ = decide({"tool_name": tool, "tool_input": {"file_path": "x.py"}, "session_id": "s1"}, env)
        check(f"flag ON: {tool} allowed", code == 0)

    # Bash mutating -> deny ; Bash read -> allow
    code, _ = decide({"tool_name": "Bash", "tool_input": {"command": "echo hi > file.txt"}, "session_id": "s1"}, env)
    check("flag ON: Bash redirect-write denied", code == 2)
    code, _ = decide({"tool_name": "Bash", "tool_input": {"command": "sed -i 's/a/b/' f"}, "session_id": "s1"}, env)
    check("flag ON: Bash sed -i denied", code == 2)
    code, _ = decide({"tool_name": "Bash", "tool_input": {"command": "pytest tests/ -q"}, "session_id": "s1"}, env)
    check("flag ON: Bash test-run allowed", code == 0)
    code, _ = decide({"tool_name": "Bash", "tool_input": {"command": "git status && grep foo *.py"}, "session_id": "s1"}, env)
    check("flag ON: Bash git/grep allowed", code == 0)

    # escalation valve: ledger file present+nonempty -> allow Edit
    led_dir = os.path.join(td, "reasonix-conductor-escalations")
    os.makedirs(led_dir, exist_ok=True)
    with open(os.path.join(led_dir, "s1"), "w") as f:
        f.write("LANE_ESCALATE: status=stagnated\n")
    code, _ = decide({"tool_name": "Edit", "tool_input": {"file_path": "x.py"}, "session_id": "s1"}, env)
    check("escalation pending: Edit allowed (valve)", code == 0)

    # fail-open: no session_id -> allow
    code, _ = decide({"tool_name": "Edit", "tool_input": {"file_path": "x.py"}}, env)
    check("fail-open: no session_id -> allowed", code == 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 tests/test-conductor-guard.py`
Expected: FAIL — `conductor-guard.py` does not exist yet (`FileNotFoundError` / load error).

- [ ] **Step 3: Create `hooks/conductor-guard.py`**

```python
#!/usr/bin/env python3
"""Conductor-mode guard: deny Opus's operator tools (Edit/Write/MultiEdit + clearly
mutating Bash) so the conductor delegates to the Reasonix fleet instead of doing the
work itself. Default OFF (CLAUDE_REASONIX_CONDUCTOR_REVIEW_ONLY). Fail-OPEN on any
uncertainty: the guard must never wedge the user. A pending escalation for the
session unlocks editing (Opus may fix a broken lane)."""
import json
import os
import re
import sys

_OPERATOR_TOOLS = {"Edit", "Write", "MultiEdit"}

# Clearly file-mutating Bash. Conservative: anything not matched is treated as a
# read/test/scope command and ALLOWED (fail-open).
_BASH_WRITE_RE = re.compile(
    r"(>>?)"            # output redirection > or >>
    r"|\bsed\s+-i\b"    # in-place sed
    r"|\btee\b"         # tee writes
    r"|\bperl\s+-i\b",  # in-place perl
)


def _truthy(name, fallback_name):
    v = os.environ.get(name)
    if v is None:
        v = os.environ.get(fallback_name, "")
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def _enabled():
    return _truthy("CLAUDE_REASONIX_CONDUCTOR_REVIEW_ONLY",
                   "CLAUDE_CODEX_CONDUCTOR_REVIEW_ONLY")


def _ledger_path(session_id):
    if not session_id:
        return None
    tmp = os.environ.get("TMPDIR") or "/tmp"
    return os.path.join(tmp, "reasonix-conductor-escalations", str(session_id))


def _has_unresolved_escalation(session_id):
    p = _ledger_path(session_id)
    if not p:
        return False
    try:
        return os.path.isfile(p) and os.path.getsize(p) > 0
    except Exception:
        return False


def bash_mutates(command):
    if not command:
        return False
    return bool(_BASH_WRITE_RE.search(command))


def decide(payload):
    """Returns (exit_code, message). 0 = allow, 2 = deny."""
    if not _enabled():
        return 0, ""
    tool = str(payload.get("tool_name") or "")
    if tool not in _OPERATOR_TOOLS and tool != "Bash":
        return 0, ""
    if tool == "Bash":
        cmd = ""
        ti = payload.get("tool_input")
        if isinstance(ti, dict):
            cmd = str(ti.get("command") or "")
        if not bash_mutates(cmd):
            return 0, ""
    # operator action detected; the only thing that unlocks it is a pending escalation
    sid = payload.get("session_id")
    if not sid:
        return 0, ""  # fail-open: can't key the valve, never wedge
    if _has_unresolved_escalation(sid):
        return 0, ""  # safety valve: a lane escalated/failed; Opus may fix it
    return 2, (
        "Conductor mode: you are the orchestrator, not the operator. Do NOT edit "
        "files yourself. Decompose this into lane(s) with an acceptanceTest and "
        "dispatch via mcp__reasonix_fleet__run_reasonix_worker (or an agent() lane "
        "in a Workflow). Reasonix workers write the files. (This block lifts "
        "automatically if a lane escalates/fails so you can intervene.)"
    )


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # fail-open: malformed hook JSON must never block the user
    code, msg = decide(payload)
    if code == 2:
        print(msg, file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 tests/test-conductor-guard.py`
Expected: PASS — all checks ok (5 from Task 1 + the Task-2 checks).

- [ ] **Step 5: Commit**

```bash
git add hooks/conductor-guard.py tests/test-conductor-guard.py
git commit -m "feat(conductor): always-on guard hook denying operator tools, fail-open + escalation valve"
```

---

### Task 3: Gateway writes the escalation ledger

**Files:**
- Modify: `reasonix_gateway/harness.py` (the place that builds the `LANE_ESCALATE` reply — around `harness.py:55-61`)
- Modify: `tests/test-conductor-guard.py` (add a test that the ledger gets written)

**Interfaces:**
- Consumes: `escalation_ledger_path(session_id)` from Task 1.
- Produces: a side effect — when a lane result is an escalation/failure, append a line to the
  ledger file so the hook (Task 2) sees a pending escalation. New function
  `record_escalation(session_id: str | None, note: str) -> None` (no-op if session_id falsy or
  write fails — never raise into the lane path).

NOTE: the exact call site that produces a `LANE_ESCALATE` reply must call `record_escalation`. Read
`reasonix_gateway/harness.py` around the `harness_lane_reply` / the `LANE_ESCALATE:` f-string
(line ~61) and the engine_seam caller to find where `session_id` is available. If `session_id` is
not threaded to that function, thread it through from the caller (the gateway request handler has
it). If it genuinely cannot be obtained at that layer, write the ledger in the gateway request
handler instead, right after it receives an escalation/failed/hollow lane result — the requirement
is "ledger written when a lane escalates", not "written from this exact function".

- [ ] **Step 1: Write the failing test** (append to `tests/test-conductor-guard.py` before the summary)

```python
# --- Task 3: gateway records an escalation to the ledger ---
with tempfile.TemporaryDirectory() as td:
    os.environ["TMPDIR"] = td
    _h.record_escalation("sess-esc", "LANE_ESCALATE: status=exhausted")
    p = _h.escalation_ledger_path("sess-esc")
    check("record_escalation wrote ledger", p and os.path.isfile(p) and os.path.getsize(p) > 0)
    # no-op + no raise on falsy session
    try:
        _h.record_escalation(None, "x"); check("record_escalation(None) no-raise", True)
    except Exception:
        check("record_escalation(None) no-raise", False)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 tests/test-conductor-guard.py`
Expected: FAIL — `module 'reasonix_gateway.harness' has no attribute 'record_escalation'`

- [ ] **Step 3: Add `record_escalation` and call it at the escalation site**

Add to `reasonix_gateway/harness.py` (next to `escalation_ledger_path`):

```python
def record_escalation(session_id, note):
    """Append an escalation note to the per-session conductor ledger so the
    conductor-guard hook lifts the edit block (Opus may fix the broken lane).
    No-op + never raises if session_id is falsy or the write fails — this runs
    on the lane result path and must not break it."""
    path = escalation_ledger_path(session_id)
    if not path:
        return
    try:
        _os.makedirs(_os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write((note or "LANE_ESCALATE") + "\n")
    except Exception:
        pass
```

Then find the call site that returns a `LANE_ESCALATE`/failed/hollow lane reply and call
`record_escalation(session_id, <the escalate note>)` there. (Per the Interfaces note above, thread
`session_id` from the gateway handler if needed; if not feasible at the harness layer, place the
call in the gateway request handler right after an escalated/failed lane result is detected.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 tests/test-conductor-guard.py`
Expected: PASS — all checks ok.

- [ ] **Step 5: Run the engine-seam golden test to confirm the prompt-building bytes are unchanged**

Run: `python3 tests/test-engine-seam-byte-identical.py`
Expected: PASS — `3 passed` (the ledger write is a side effect, not a prompt-byte change).

- [ ] **Step 6: Commit**

```bash
git add reasonix_gateway/harness.py tests/test-conductor-guard.py
git commit -m "feat(conductor): gateway records lane escalations to the ledger the guard reads"
```

---

### Task 4: Wire the hook into settings + launcher flag

**Files:**
- Modify: `bridge-settings.json`
- Modify: `bin/claude-reasonix`
- Test: `tests/test-reasonix-fleet.sh` (extend its existing settings/structure assertions)

**Interfaces:**
- Consumes: `hooks/conductor-guard.py` (Task 2).
- Produces: a wired PreToolUse matcher + a launcher pass-through of the opt-in flag. No new code
  symbols.

- [ ] **Step 1: Write the failing test** (append near the other `bridge-settings.json` assertions in `tests/test-reasonix-fleet.sh`, inside the existing python heredoc that loads `settings`)

Add to that heredoc's assertions:

```python
# Conductor guard must be wired on the operator tools (Edit/Write/MultiEdit/Bash).
hook_cmds = [h.get("command", "") for g in settings.get("hooks", {}).get("PreToolUse", []) for h in g.get("hooks", [])]
matchers = [g.get("matcher", "") for g in settings.get("hooks", {}).get("PreToolUse", [])]
if not any("conductor-guard.py" in c for c in hook_cmds):
    raise SystemExit("bridge settings must wire the conductor-guard hook")
if not any("Edit" in m and "Write" in m for m in matchers):
    raise SystemExit("conductor-guard must match Edit|Write|MultiEdit|Bash")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash tests/test-reasonix-fleet.sh`
Expected: FAIL with `bridge settings must wire the conductor-guard hook`

- [ ] **Step 3: Add the matcher to `bridge-settings.json`**

Insert this object as the FIRST entry of the `PreToolUse` array (before the `Workflow` matcher), so the guard runs first:

```json
      {
        "matcher": "Edit|Write|MultiEdit|Bash",
        "hooks": [
          {
            "type": "command",
            "command": "/usr/bin/env python3 __INSTALL_HOME__/hooks/conductor-guard.py",
            "timeout": 30
          }
        ]
      },
```

- [ ] **Step 4: Export the opt-in flag in `bin/claude-reasonix`**

Inside the `if [[ "$CLAUDE_REASONIX_FLAVOR" == "reasonix" ]]; then` block (near the other
`: "${CLAUDE_REASONIX_GATEWAY_*:=...}"` exports, ~line 56-59), add a PASS-THROUGH (do NOT default it
on — default OFF means we only export if already set):

```bash
  # Conductor mode (default OFF). When the user sets it to 1, the conductor-guard
  # hook denies Opus's operator tools so work is delegated to the Reasonix fleet.
  # Pass through if set; never force-enable (measure-then-promote).
  [[ -n "${CLAUDE_REASONIX_CONDUCTOR_REVIEW_ONLY:-}" ]] && export CLAUDE_REASONIX_CONDUCTOR_REVIEW_ONLY
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `bash tests/test-reasonix-fleet.sh && bash -n bin/claude-reasonix && echo LAUNCHER_OK`
Expected: PASS (the fleet test) + `LAUNCHER_OK`.

- [ ] **Step 6: Verify the hook copies on install (the loop already copies hooks/*.py)**

Run: `T=$(mktemp -d); env -u DEEPSEEK_API_KEY HOME="$T" CLAUDE_REASONIX_SKIP_CLAUDE_CHECK=1 CLAUDE_REASONIX_FLEET_INSTALL_HOME="$T/ih" CLAUDE_REASONIX_BIN_DIR="$T/bin" DEEPSEEK_API_KEY=x bash install.sh >/dev/null 2>&1; ls "$T/ih/hooks/conductor-guard.py" && echo COPIED; rm -rf "$T"`
Expected: prints the path + `COPIED` (confirms `cp -f "$SRC/hooks/"*.py` carried the new hook).

- [ ] **Step 7: Commit**

```bash
git add bridge-settings.json bin/claude-reasonix tests/test-reasonix-fleet.sh
git commit -m "feat(conductor): wire guard hook on Edit|Write|MultiEdit|Bash + launcher opt-in flag"
```

---

### Task 5: Prompt cleanup (replace exhortation with structure)

**Files:**
- Modify: `system-prompt-reasonix.md`
- Test: `tests/test-reasonix-fleet.sh` (it already asserts prompt contents; add an assertion that the loophole line is gone)

**Interfaces:** none (prose change).

- [ ] **Step 1: Write the failing test** (add to the `RX_PROMPT` assertion block in `tests/test-reasonix-fleet.sh`)

```bash
grep -qi "one genuinely small" "$RX_PROMPT" && fail "conductor mode: the small-edit loophole line must be removed"
grep -qi "Banned excuses" "$RX_PROMPT" && fail "conductor mode: the Banned-excuses list must be removed (the hook enforces now)"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash tests/test-reasonix-fleet.sh`
Expected: FAIL with `the small-edit loophole line must be removed`

- [ ] **Step 3: Edit `system-prompt-reasonix.md`**

Make these exact changes (per spec §5):
1. In the DECIDING RULE list, DELETE bullet 3 ("One genuinely small, self-contained edit ... inline is fine") and the summary line "So: small-and-single = inline OK ...". Replace with: "Every change goes to a lane. The conductor-guard hook denies your operator tools; decompose and dispatch."
2. In "Claude keeps these", DELETE the bullet "ONE small self-contained edit (a single ≤2-line change ...)".
3. DELETE the entire "### Banned excuses" section (lines 53-59). Replace with a single line:
   "Your operator tools (Edit/Write/Bash-writes) are blocked by the conductor guard in this mode — there is nothing to rationalize; dispatch the work."
4. MOVE the "(measured failure: one lane read 833 files ...)" parenthetical out of the agent-first
   section and into the "How to split work" decomposition section only (it informs HOW to
   decompose, not WHETHER to delegate).

- [ ] **Step 4: Run tests to verify they pass**

Run: `bash tests/test-reasonix-fleet.sh`
Expected: PASS (loophole + banned-excuses assertions now hold; the existing prompt assertions like "claude-reasonix-flash", "atomic", "unlimited", "web search", "ALWAYS delegate" must STILL pass — verify they were not removed).

- [ ] **Step 5: Commit**

```bash
git add system-prompt-reasonix.md tests/test-reasonix-fleet.sh
git commit -m "docs(conductor): trim prompt loopholes — structure (the guard) now enforces delegation"
```

---

### Task 6: End-to-end subprocess test (real hook, real stdin)

**Files:**
- Create: `tests/test-conductor-guard-e2e.sh`

**Interfaces:**
- Consumes: `hooks/conductor-guard.py` (Task 2).
- Produces: an e2e test proving the hook behaves correctly when invoked exactly as Claude Code
  invokes it — `python3 hooks/conductor-guard.py` with JSON piped on stdin — asserting the process
  exit code (0 allow / 2 deny). Unit tests call `decide()` in-process; this proves the real CLI path
  (stdin parse, env read, exit code) end-to-end.

- [ ] **Step 1: Write the test**

Create `tests/test-conductor-guard-e2e.sh`:

```bash
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
```

- [ ] **Step 2: Make it executable and run it**

Run: `chmod +x tests/test-conductor-guard-e2e.sh && bash tests/test-conductor-guard-e2e.sh`
Expected: PASS — `8 passed, 0 failed`

- [ ] **Step 3: Run the FULL suite (nothing regressed)**

Run: `bash tests/run-all.sh 2>&1 | tail -3`
Expected: `=== summary: N passed, 0 failed ===` (N = prior count + the 2 new test files).

- [ ] **Step 4: Verify the guard is byte-inert when OFF (full suite with flag unset is unchanged)**

Run: `env -u CLAUDE_REASONIX_CONDUCTOR_REVIEW_ONLY bash tests/run-all.sh 2>&1 | tail -1`
Expected: `0 failed` (default-OFF means the new hook never blocks anything in the existing tests).

- [ ] **Step 5: Commit**

```bash
git add tests/test-conductor-guard-e2e.sh
git commit -m "test(conductor): e2e subprocess test of the guard (stdin->exit code, fail-open, valve)"
```

---

## Self-Review

**1. Spec coverage:**
- §3 conductor keeps Read/Grep/Glob + loses Edit/Write/MultiEdit/mutating-Bash → Task 2 ✓
- §4.1 always-on hook, deny operator tools, Bash classification → Task 2 ✓
- §4.2 escalation ledger valve + fail-open → Task 2 (read) + Task 3 (write) ✓
- §4.3 default-OFF flag + launcher → Task 2 (`_enabled`) + Task 4 ✓
- §5 prompt cleanup → Task 5 ✓
- §6 risks: fail-open (Task 2), escalation valve (Task 2/3), Bash-evasion (Task 2 `bash_mutates`), default-OFF (Task 2/4) ✓
- §8 success criteria: deny-when-on/allow-when-off/valve regression tests → Task 2 + Task 6 ✓

**2. Placeholder scan:** No TBD/TODO. Task 3 has a deliberate "find the call site" instruction (the exact line depends on live code the implementer reads) but specifies the exact function to add, the exact contract, and a fallback location — not a placeholder, a bounded discovery step.

**3. Type consistency:** `escalation_ledger_path(session_id)` (Task 1) ↔ replicated as `_ledger_path` in the hook (Task 2) ↔ used by `record_escalation` (Task 3) — same path formula, same dir name `reasonix-conductor-escalations`, verified identical across tasks. `decide(payload) -> (int, str)` consistent between hook impl and tests. Env var `CLAUDE_REASONIX_CONDUCTOR_REVIEW_ONLY` consistent across Task 2, 4.
