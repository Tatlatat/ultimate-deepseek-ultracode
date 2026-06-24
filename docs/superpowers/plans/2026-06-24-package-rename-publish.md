# Package + Rename + Publish `claude-reasonix` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename every `codex` reference to `reasonix` without breaking lane routing, then package the repo so a fresh clone runs via one `./install.sh`.

**Architecture:** Three risk-ordered rename layers (brand → agentType → cosmetics), each gated by the full test suite + a real bench; then a portable repo layout with `install.sh`/`uninstall.sh`/README and de-hardcoded paths.

**Tech Stack:** bash launcher, Python 3 stdlib (gateway/hooks/tests), reasonix CLI over DeepSeek, Claude Code host. `ccr` optional.

## Global Constraints

- New name `reasonix`: `codex`→`reasonix`, `CLAUDE_CODEX_`→`CLAUDE_REASONIX_`, repo `claude-codex-fleet`→`claude-reasonix-fleet`.
- agentType strings MUST match byte-for-byte across THREE sites at once: launcher `--agents`, `reasonix-workflow.py` emit, `only-reasonix-fleet.py` whitelist `startswith`. A lone change breaks lane routing.
- reasonix NATIVE flavor does NOT need `ccr` (uses `start_native_gateway` directly). `ccr` is optional (router mode).
- Env vars read with backward-compat: `env_first("CLAUDE_REASONIX_X", "CLAUDE_CODEX_X", default=...)` — new name preferred, old still honored.
- No machine-specific absolute paths in shipped code: resolve reasonix via `command -v reasonix` / `REASONIX_BIN`; `INSTALL_HOME` defaults to `$HOME/.claude/reasonix-fleet`.
- After EACH rename layer: `python3 -c "import ast; ast.parse(open(f).read())"` on changed `.py`, full suite (`for t in tests/test-*.py; do python3 "$t"; done`), and `python3 runtime/realworld-bench.py`. Proceed only if green.
- The dist patch (`REASONIX_ACP_EPHEMERAL_SESSION`) lives outside the repo and reverts on reasonix upgrade — `install.sh` re-applies idempotently.
- Do NOT commit `runtime/*.jsonl`, `*.port`, `__pycache__/`, `*.bak`, credentials.
- Work on a branch off the current `fix/reasonix-session-isolation` (or a fresh `package-publish` branch); never on a published default branch directly.

---

## File Structure

This plan operates on a COPY of the live system inside the repo working tree
(`~/.claude/codex-fleet`, which is a git repo). The launcher currently lives at
`~/.local/bin/claude-codex` (a symlink target outside the repo); Task 1 brings a
copy into the repo as `bin/claude-reasonix` so it is versioned and renamed there,
and `install.sh` (Task 8) is what re-deploys it to `~/.local/bin`.

- `bin/claude-reasonix` — the launcher (created in repo from the live one, renamed).
- `reasonix-native-gateway.py`, `reasonix-fleet-mcp.py` — renamed from `codex-*`.
- `hooks/reasonix-workflow.py`, `hooks/only-reasonix-fleet.py` — renamed from `codex-*`.
- `bridge-settings.json`, `system-prompt-reasonix.md` — hook path + env refs updated.
- `tests/test-no-codex-leftovers.py` — new guard test.
- `install.sh`, `uninstall.sh`, `README.md`, `patches/ephemeral-session.md` — new.
- `.gitignore` — runtime artifacts.

---

### Task 1: Bring the launcher into the repo (versioned, unrenamed yet)

**Files:**
- Create: `bin/claude-reasonix` (copy of `/Users/tatlatat/.local/bin/claude-codex`)

**Interfaces:**
- Produces: `bin/claude-reasonix` — the launcher script, byte-identical to the live one, so later tasks rename it in-repo under version control.

- [ ] **Step 1: Copy the live launcher into the repo**

```bash
cd ~/.claude/codex-fleet
mkdir -p bin
cp -p "$(readlink -f /Users/tatlatat/.local/bin/claude-codex)" bin/claude-reasonix
chmod +x bin/claude-reasonix
```

- [ ] **Step 2: Verify it is the real launcher**

Run: `grep -c "start_native_gateway" bin/claude-reasonix`
Expected: a non-zero count (the launcher defines `start_native_gateway`).

- [ ] **Step 3: Commit**

```bash
git add bin/claude-reasonix
git commit -m "chore: vendor the launcher into the repo as bin/claude-reasonix"
```

---

### Task 2: Guard test — no `codex` leftovers (write FIRST, expected to fail)

**Files:**
- Create: `tests/test-no-codex-leftovers.py`

**Interfaces:**
- Produces: a test that scans shipped source for `codex`/`CLAUDE_CODEX_` and fails until the rename is complete. It is the objective "done" signal for the rename layers (Tasks 3-5).

- [ ] **Step 1: Write the failing test**

```python
# tests/test-no-codex-leftovers.py
from __future__ import annotations
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Files that LEGITIMATELY still mention codex: this guard test (the pattern strings),
# historical docs/specs/plans, and the backward-compat fallback (CLAUDE_CODEX_ as the
# SECOND arg of env_first). Everything else must be codex-free.
ALLOW_SUBSTR = ("tests/test-no-codex-leftovers.py", "/docs/", "/patches/", "README")

def shipped_files():
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        s = str(p)
        if "/.git/" in s or "__pycache__" in s or s.endswith((".bak", ".port", ".jsonl")):
            continue
        if "/runtime/" in s:
            continue
        if any(a in s for a in ALLOW_SUBSTR):
            continue
        if p.suffix in (".py", ".sh", ".json", ".md", "") or p.name == "claude-reasonix":
            yield p

def test_no_codex_in_filenames():
    bad = [str(p) for p in shipped_files() if "codex" in p.name.lower()]
    assert not bad, f"files still named codex: {bad}"

def test_no_codex_identifiers_outside_fallback():
    # Allow `CLAUDE_CODEX_` ONLY when it is the 2nd+ argument of env_first / a getenv
    # fallback (backward-compat). Flag any other codex/CLAUDE_CODEX_ token.
    offenders = []
    for p in shipped_files():
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            low = line.lower()
            if "codex" not in low:
                continue
            # backward-compat fallback line is allowed
            if "claude_codex_" in low and ("env_first(" in low or "fallback" in low or "getenv" in low):
                continue
            offenders.append(f"{p}:{i}: {line.strip()[:80]}")
    assert not offenders, "codex references remain:\n" + "\n".join(offenders)

if __name__ == "__main__":
    test_no_codex_in_filenames()
    test_no_codex_identifiers_outside_fallback()
    print("PASS: no codex leftovers")
```

- [ ] **Step 2: Run it — expect FAIL**

Run: `python3 tests/test-no-codex-leftovers.py`
Expected: `FAIL` / AssertionError listing many `codex` files and identifiers (the rename hasn't happened yet).

- [ ] **Step 3: Commit the guard test**

```bash
git add tests/test-no-codex-leftovers.py
git commit -m "test: add no-codex-leftovers guard (currently failing, drives the rename)"
```

---

### Task 3: Layer A — rename files, dir default, and the 71 env vars (brand)

**Files:**
- Rename: `codex-native-gateway.py`→`reasonix-native-gateway.py`; `codex-fleet-mcp.py`→`reasonix-fleet-mcp.py`; `hooks/codex-workflow.py`→`hooks/reasonix-workflow.py`; `hooks/only-codex-fleet.py`→`hooks/only-reasonix-fleet.py`
- Modify: `bin/claude-reasonix`, `bridge-settings.json`, `system-prompt-reasonix.md`, every `.py` reading `CLAUDE_CODEX_*`, every internal reference to a renamed file/dir.

**Interfaces:**
- Consumes: `bin/claude-reasonix` (Task 1), the guard test (Task 2).
- Produces: brand-renamed files + env vars; agentType strings (`codex-*`) and comments are NOT yet touched (Layers B/C).

- [ ] **Step 1: git-rename the files (preserve history)**

```bash
cd ~/.claude/codex-fleet
git mv codex-native-gateway.py reasonix-native-gateway.py
git mv codex-fleet-mcp.py reasonix-fleet-mcp.py
git mv hooks/codex-workflow.py hooks/reasonix-workflow.py
git mv hooks/only-codex-fleet.py hooks/only-reasonix-fleet.py
```

- [ ] **Step 2: Add the backward-compat env fallback to env_first call sites**

In `reasonix-native-gateway.py` (and any `.py`/launcher reading env), change each
`env_first("CLAUDE_CODEX_X", ...)` to put the new name first and the old as fallback.
`env_first(*names, default)` already tries names in order, so:

```python
# before:  env_first("CLAUDE_CODEX_GATEWAY_PRIME_GATE", default="1")
# after:
env_first("CLAUDE_REASONIX_GATEWAY_PRIME_GATE", "CLAUDE_CODEX_GATEWAY_PRIME_GATE", default="1")
```

Apply to ALL 71 distinct `CLAUDE_CODEX_*` reads. For `os.getenv("CLAUDE_CODEX_X")`
sites, replace with `env_first("CLAUDE_REASONIX_X", "CLAUDE_CODEX_X", default=...)` or
`os.getenv("CLAUDE_REASONIX_X", os.getenv("CLAUDE_CODEX_X", default))`.

For the launcher (bash), each `${CLAUDE_CODEX_X:-default}` becomes:
```bash
"${CLAUDE_REASONIX_X:-${CLAUDE_CODEX_X:-default}}"
```
and each `export CLAUDE_CODEX_X` / `CLAUDE_CODEX_X=...` that the gateway/hook reads
must export BOTH names (new primary) so the child sees the new var:
```bash
CLAUDE_REASONIX_FLAVOR="reasonix"; export CLAUDE_REASONIX_FLAVOR
```

- [ ] **Step 3: Update internal file/dir references**

In `bin/claude-reasonix`: `INSTALL_HOME` default and every `$INSTALL_HOME/<file>`
reference to a renamed file:
```bash
INSTALL_HOME="${CLAUDE_REASONIX_FLEET_INSTALL_HOME:-${CLAUDE_CODEX_FLEET_INSTALL_HOME:-$HOME/.claude/reasonix-fleet}}"
GATEWAY_FILE="$INSTALL_HOME/reasonix-native-gateway.py"
PROMPT_FILE="$INSTALL_HOME/system-prompt-reasonix.md"
CCR_PROXY_FILE="$INSTALL_HOME/ccr-claude-proxy.py"
MCP_SERVER_FILE="$INSTALL_HOME/reasonix-fleet-mcp.py"
```
In `bridge-settings.json`: the hook command path → `.../reasonix-fleet/hooks/reasonix-workflow.py`.
In `reasonix-fleet-mcp.py`: the gateway import path → `reasonix-native-gateway.py`.

- [ ] **Step 4: Verify Python parses and the brand env still resolves**

Run:
```bash
python3 -c "import ast; ast.parse(open('reasonix-native-gateway.py').read()); ast.parse(open('reasonix-fleet-mcp.py').read()); ast.parse(open('hooks/reasonix-workflow.py').read()); ast.parse(open('hooks/only-reasonix-fleet.py').read()); print('ast OK')"
```
Expected: `ast OK`

- [ ] **Step 5: Run the full suite (test files still import the renamed module)**

Update each test's `spec_from_file_location("gw", ROOT / "reasonix-native-gateway.py")`
path (the tests load the gateway by filename). Then:
```bash
for t in tests/test-*.py; do echo "$t -> $(python3 "$t" 2>&1 | tail -1)"; done
```
Expected: every line `PASS:` or `MET`. Fix any test still pointing at `codex-native-gateway.py`.

- [ ] **Step 6: Run the real bench**

Run: `python3 runtime/realworld-bench.py`
Expected: `ALL GATES PASS` (or the known-good per-workload result).

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor(rename): Layer A — files, dir, 71 env vars codex->reasonix (back-compat fallback kept)"
```

---

### Task 4: Layer B — agentType rename + drop dead `deepseek-*` (highest risk)

**Files:**
- Modify: `bin/claude-reasonix` (the two `--agents` blocks), `hooks/reasonix-workflow.py` (emit logic), `hooks/only-reasonix-fleet.py` (whitelist)

**Interfaces:**
- Consumes: Layer A renames.
- Produces: agentTypes `reasonix-worker/reasonix-security/reasonix-reviewer/reasonix-verify`; `deepseek-deep`/`deepseek-architecture` removed (those hints fall through to `reasonix-worker`).

- [ ] **Step 1: Rename the 4 agentTypes in the launcher `--agents` blocks**

In `bin/claude-reasonix`, in BOTH flavor blocks, rename the dict keys:
`"codex-worker"`→`"reasonix-worker"`, `"codex-security"`→`"reasonix-security"`,
`"codex-reviewer"`→`"reasonix-reviewer"`, `"codex-verify"`→`"reasonix-verify"`.
DELETE the `"deepseek-deep"` and `"deepseek-architecture"` entries in both blocks.

- [ ] **Step 2: Update the emit logic in the hook**

In `hooks/reasonix-workflow.py`, the `__claudeCodexNativeAgentType` function:
```javascript
// rename the guard and returns:
if (explicit.startsWith('reasonix-')) return explicit
if (explicit.startsWith('deepseek-')) return 'reasonix-worker'   // legacy deepseek hint -> worker
const hint = [opts.label, opts.phase, explicit].filter(Boolean).join(' ').toLowerCase()
if (hint.includes('security')) return 'reasonix-security'
if (hint.includes('verify') || hint.includes('test')) return 'reasonix-verify'
if (hint.includes('review')) return 'reasonix-reviewer'
// DELETE the two branches that returned 'deepseek-architecture' / 'deepseek-deep'
return 'reasonix-worker'
```

- [ ] **Step 3: Update the whitelist in the policy hook**

In `hooks/only-reasonix-fleet.py`:
```python
# was: if lowered.startswith(("codex-", "deepseek-")):
if lowered.startswith(("reasonix-",)):
    return True
# and: if "agent(reasonix-" in lowered:
if "agent(reasonix-" in lowered:
    return True
```

- [ ] **Step 4: Verify parses**

Run:
```bash
python3 -c "import ast; ast.parse(open('hooks/only-reasonix-fleet.py').read()); print('py OK')"
node --check hooks/reasonix-workflow.py && echo "js OK"
bash -n bin/claude-reasonix && echo "bash OK"
```
Expected: `py OK`, `js OK`, `bash OK`.

- [ ] **Step 5: Run the unit suite**

Run: `for t in tests/test-*.py; do echo "$t -> $(python3 "$t" 2>&1 | tail -1)"; done`
Expected: every line `PASS:`/`MET`. (`test-mcp-reasonix.py` / `test-workflow-selfheal.py` exercise the hook/agentType paths.)

- [ ] **Step 6: Live routing check — agentType actually routes, nothing blocked**

Spawn the gateway and fire a fan-out whose hints include "architecture" and
"database" (the dropped-deepseek hints), confirm they route to `reasonix-worker`
and land in the ledger (not policy-blocked):
```bash
python3 runtime/realworld-bench.py   # exercises real fan-out via the renamed agentTypes
tail -5 runtime/reasonix-cost.jsonl | python3 -c "import sys,json;[print(json.loads(l).get('cache_pct')) for l in sys.stdin if l.strip()]"
```
Expected: bench `ALL GATES PASS`, ledger shows real lanes (0 empty/errored) — proving the renamed agentTypes route and the dropped deepseek hints fall through cleanly.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor(rename): Layer B — agentTypes codex-*->reasonix-*, drop dead deepseek-* (synced across 3 sites)"
```

---

### Task 5: Layer C — cosmetics (comments, docstrings, strings, README prose)

**Files:**
- Modify: all shipped `.py`/`.sh`/`.md` with remaining `codex`/`claude-codex` in comments/strings.

**Interfaces:**
- Consumes: Layers A+B.
- Produces: a codex-free shipped tree (except the guard test's own pattern strings and historical docs).

- [ ] **Step 1: Replace remaining codex tokens in comments/strings**

For each shipped source file (NOT docs/specs/plans, NOT the guard test), replace
`codex`→`reasonix`, `Codex`→`Reasonix`, `claude-codex`→`claude-reasonix` in
comments, docstrings, log messages, `server_version` strings. Leave the
backward-compat `CLAUDE_CODEX_` fallback args untouched (Layer A added them).

- [ ] **Step 2: The guard test now passes**

Run: `python3 tests/test-no-codex-leftovers.py`
Expected: `PASS: no codex leftovers`

- [ ] **Step 3: Full suite + bench**

Run:
```bash
for t in tests/test-*.py; do echo "$t -> $(python3 "$t" 2>&1 | tail -1)"; done
python3 runtime/realworld-bench.py
```
Expected: all `PASS`/`MET`; bench `ALL GATES PASS`.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor(rename): Layer C — cosmetics; no-codex-leftovers guard now passes"
```

---

### Task 6: De-hardcode machine-specific paths (portability)

**Files:**
- Modify: `reasonix-fleet-mcp.py`, `bridge-settings.json`, `tests/diag-prefix-stability.py`, `tests/test-prime-gate-scale.py`, `tests/test-codex-fleet.sh` (rename to `test-reasonix-fleet.sh`), `bin/claude-reasonix`

**Interfaces:**
- Consumes: renamed tree.
- Produces: no `/Users/tatlatat` or fnm-multishell absolute paths in shipped code; reasonix resolved via `command -v`/`REASONIX_BIN`; `INSTALL_HOME` via `$HOME`.

- [ ] **Step 1: Replace hardcoded reasonix paths with resolution**

In any file with a literal `/Users/tatlatat/.local/state/fnm_multishells/.../bin/reasonix`,
replace with: read `REASONIX_BIN` env, else `shutil.which("reasonix")` (Python) /
`command -v reasonix` (bash). Example (Python test):
```python
import os, shutil
RBIN = os.getenv("REASONIX_BIN") or shutil.which("reasonix") or "reasonix"
```

- [ ] **Step 2: Replace hardcoded INSTALL_HOME/home paths**

`/Users/tatlatat/.claude/...` → `os.path.expanduser("~/.claude/reasonix-fleet")` or
`$HOME` in bash; `bridge-settings.json` hook path → relative to `$INSTALL_HOME`
(install.sh rewrites it at install time, see Task 8).

- [ ] **Step 3: Verify no machine paths remain in shipped code**

Run:
```bash
grep -rn "/Users/tatlatat\|fnm_multishells" . --include="*.py" --include="*.sh" --include="*.json" | grep -vE "/docs/|/\.git/" || echo "CLEAN"
```
Expected: `CLEAN`

- [ ] **Step 4: Full suite (paths still resolve via env/which)**

Run: `for t in tests/test-*.py; do echo "$t -> $(python3 "$t" 2>&1 | tail -1)"; done`
Expected: all `PASS`/`MET`.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor(portability): resolve reasonix bin via command-v/REASONIX_BIN; INSTALL_HOME via \$HOME"
```

---

### Task 7: `.gitignore` + repo hygiene

**Files:**
- Create/Modify: `.gitignore`

**Interfaces:**
- Produces: a tree that never commits runtime artifacts or credentials.

- [ ] **Step 1: Write `.gitignore`**

```gitignore
runtime/*.jsonl
runtime/*.port
runtime/ccr-home/
__pycache__/
*.bak
*.preuniq.bak
.DS_Store
```

- [ ] **Step 2: Untrack any already-committed runtime artifacts**

```bash
git rm -r --cached runtime/*.jsonl runtime/*.port 2>/dev/null || true
git status --short | grep -E "\.jsonl|\.port" || echo "none tracked"
```

- [ ] **Step 3: Commit**

```bash
git add .gitignore
git commit -m "chore: gitignore runtime artifacts and backups"
```

---

### Task 8: `install.sh` (one-command install, fail-loud)

**Files:**
- Create: `install.sh`

**Interfaces:**
- Consumes: the renamed `bin/claude-reasonix`, `fleet/` content (the repo's renamed files), `patches/ephemeral-session.md`.
- Produces: a working install on a clean machine, or a clear actionable failure.

- [ ] **Step 1: Write `install.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail
REPO="$(cd "$(dirname "$0")" && pwd)"
INSTALL_HOME="${CLAUDE_REASONIX_FLEET_INSTALL_HOME:-$HOME/.claude/reasonix-fleet}"
BIN_DIR="$HOME/.local/bin"
fail() { echo "❌ $1" >&2; exit 1; }

echo "→ checking prerequisites"
command -v claude >/dev/null   || fail "Claude Code not found. Install it, then re-run ./install.sh"
command -v reasonix >/dev/null || fail "reasonix CLI not found. Install reasonix (npm), then re-run ./install.sh"
# DeepSeek login probe (reasonix prints a login hint if not authed)
if ! reasonix --version >/dev/null 2>&1; then
  fail "reasonix is installed but not usable. Run 'reasonix login' (DeepSeek), then re-run ./install.sh"
fi

echo "→ installing fleet to $INSTALL_HOME"
mkdir -p "$INSTALL_HOME"
# copy everything except the repo's own runtime/git
rsync -a --exclude '.git' --exclude 'runtime/*.jsonl' --exclude 'runtime/*.port' \
  "$REPO"/reasonix-native-gateway.py "$REPO"/reasonix-fleet-mcp.py \
  "$REPO"/ccr-claude-proxy.py "$REPO"/system-prompt-reasonix.md \
  "$REPO"/bridge-settings.json "$INSTALL_HOME"/ 2>/dev/null || \
  cp -p "$REPO"/reasonix-native-gateway.py "$REPO"/reasonix-fleet-mcp.py \
        "$REPO"/ccr-claude-proxy.py "$REPO"/system-prompt-reasonix.md \
        "$REPO"/bridge-settings.json "$INSTALL_HOME"/
mkdir -p "$INSTALL_HOME/hooks" "$INSTALL_HOME/runtime"
cp -p "$REPO"/hooks/*.py "$INSTALL_HOME/hooks/"
# rewrite the hook command path in the installed bridge-settings.json
python3 - "$INSTALL_HOME" <<'PY'
import json,sys,os
home=sys.argv[1]; f=os.path.join(home,"bridge-settings.json")
d=json.load(open(f))
def fix(o):
    if isinstance(o,dict):
        for k,v in o.items():
            if k=="command" and isinstance(v,str) and "reasonix-workflow.py" in v:
                o[k]=f"/usr/bin/env python3 {home}/hooks/reasonix-workflow.py"
            else: fix(v)
    elif isinstance(o,list):
        for v in o: fix(v)
fix(d); json.dump(d,open(f,"w"),indent=2)
PY

echo "→ linking launcher"
mkdir -p "$BIN_DIR"
ln -sf "$REPO/bin/claude-reasonix" "$BIN_DIR/claude-reasonix"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) echo "⚠️  add $BIN_DIR to PATH" ;; esac

echo "→ applying reasonix dist patch (ephemeral session)"
RBIN="$(command -v reasonix)"
DIST="$(dirname "$(readlink -f "$RBIN" 2>/dev/null || echo "$RBIN")")/../lib/node_modules/reasonix/dist/cli"
ACP="$(ls "$DIST"/acp-*.js 2>/dev/null | head -1 || true)"
if [[ -z "$ACP" ]]; then
  echo "⚠️  reasonix dist not found at $DIST — see patches/ephemeral-session.md to apply manually (system works at lower cache)."
elif grep -q "REASONIX_ACP_EPHEMERAL_SESSION" "$ACP"; then
  echo "   already patched"
else
  cp -p "$ACP" "$ACP.preuniq.bak"
  python3 "$REPO/patches/apply_ephemeral.py" "$ACP" && echo "   patched (backup: $ACP.preuniq.bak)"
fi

echo "→ smoke test"
PORT_FILE="$(mktemp)"
CLAUDE_REASONIX_GATEWAY_KEEPALIVE=0 python3 "$INSTALL_HOME/reasonix-native-gateway.py" --host 127.0.0.1 --port 0 --port-file "$PORT_FILE" >/tmp/reasonix-smoke.log 2>&1 &
GW=$!; sleep 2
PORT="$(cat "$PORT_FILE" 2>/dev/null || true)"
if [[ -n "$PORT" ]] && curl -s -m 60 "http://127.0.0.1:$PORT/v1/messages" \
   -H 'content-type: application/json' -H 'x-api-key: local' -H 'anthropic-version: 2023-06-01' \
   -d '{"model":"claude-reasonix-flash","max_tokens":8,"messages":[{"role":"user","content":"hi"}]}' \
   | grep -q "message_start"; then
  echo "✅ Ready — run 'claude-reasonix'"
else
  kill "$GW" 2>/dev/null || true
  echo "tail of gateway log:"; tail -20 /tmp/reasonix-smoke.log
  fail "smoke test failed — see the gateway log above (often: reasonix not logged in to DeepSeek)"
fi
kill "$GW" 2>/dev/null || true
```

- [ ] **Step 2: Write `patches/apply_ephemeral.py` (idempotent patch applier)**

```python
#!/usr/bin/env python3
import sys
p = sys.argv[1]
src = open(p, encoding="utf-8").read()
OLD = 'session: `acp-${timestampSuffix()}`'
NEW = ('session: (process.env.REASONIX_ACP_EPHEMERAL_SESSION === "1" ? null : '
       '`acp-${timestampSuffix()}`)')
if "REASONIX_ACP_EPHEMERAL_SESSION" in src:
    print("already patched"); sys.exit(0)
if OLD not in src:
    print("PATTERN NOT FOUND — reasonix version changed; see patches/ephemeral-session.md", file=sys.stderr)
    sys.exit(1)
open(p, "w", encoding="utf-8").write(src.replace(OLD, NEW, 1))
print("patched")
```

- [ ] **Step 3: Make scripts executable + dry-run check syntax**

```bash
chmod +x install.sh patches/apply_ephemeral.py
bash -n install.sh && echo "install.sh OK"
python3 -c "import ast; ast.parse(open('patches/apply_ephemeral.py').read()); print('applier OK')"
```
Expected: `install.sh OK`, `applier OK`.

- [ ] **Step 4: Test install.sh into a temp HOME (fresh-machine sim)**

```bash
CLAUDE_REASONIX_FLEET_INSTALL_HOME="$(mktemp -d)/reasonix-fleet" HOME="$(mktemp -d)" ./install.sh; echo "exit=$?"
```
Expected: stops at the first missing prereq with an actionable message (claude/reasonix won't be on the temp PATH), OR — if run with the real reasonix available — reaches `✅ Ready`. The point: it must FAIL LOUD with a clear message, never half-install.

- [ ] **Step 5: Commit**

```bash
git add install.sh patches/apply_ephemeral.py patches/ephemeral-session.md
git commit -m "feat: install.sh (one-command, fail-loud) + idempotent ephemeral dist patch"
```

---

### Task 9: `uninstall.sh` + `README.md` + `patches/ephemeral-session.md`

**Files:**
- Create: `uninstall.sh`, `README.md`, `patches/ephemeral-session.md`

**Interfaces:**
- Produces: clean uninstall (restore dist backup, remove symlink), user-facing docs.

- [ ] **Step 1: Write `uninstall.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail
BIN_DIR="$HOME/.local/bin"
RBIN="$(command -v reasonix || true)"
[[ -n "$RBIN" ]] && {
  DIST="$(dirname "$(readlink -f "$RBIN" 2>/dev/null || echo "$RBIN")")/../lib/node_modules/reasonix/dist/cli"
  for bak in "$DIST"/acp-*.js.preuniq.bak; do
    [[ -f "$bak" ]] && mv "$bak" "${bak%.preuniq.bak}" && echo "restored ${bak%.preuniq.bak}"
  done
}
rm -f "$BIN_DIR/claude-reasonix" && echo "removed launcher symlink"
echo "Left ~/.claude/reasonix-fleet in place (delete manually if desired)."
```

- [ ] **Step 2: Write `patches/ephemeral-session.md`**

Document what the patch is (one-line change to `acp-*.js` making the acp session
ephemeral via `REASONIX_ACP_EPHEMERAL_SESSION=1`), why (session-inheritance cache
bug), that it reverts on reasonix upgrade (re-run install.sh), and the exact manual
edit if `apply_ephemeral.py` can't find the pattern (the OLD/NEW strings from Task 8).

- [ ] **Step 3: Write `README.md`**

Sections: (1) what it is (DeepSeek fan-out backend for Claude Code, cache-optimized);
(2) Prerequisites — Claude Code, reasonix CLI + `reasonix login` (DeepSeek), node;
(3) Install — `git clone … && cd … && ./install.sh`, then `claude-reasonix`;
(4) ccr is OPTIONAL (router mode only — `npm i -g @musistudio/claude-code-router`);
(5) the dist patch reverts on reasonix upgrade → re-run `./install.sh`;
(6) Troubleshooting — map each install.sh failure message to its fix;
(7) Uninstall — `./uninstall.sh`.

- [ ] **Step 4: Syntax check + commit**

```bash
chmod +x uninstall.sh
bash -n uninstall.sh && echo "uninstall OK"
git add uninstall.sh README.md patches/ephemeral-session.md
git commit -m "docs: README, uninstall.sh, ephemeral-patch doc"
```

---

### Task 10: Publish to GitHub

**Files:** none (git/remote operations)

**Interfaces:**
- Consumes: the fully renamed, packaged, green repo.

- [ ] **Step 1: Final full verification**

```bash
python3 tests/test-no-codex-leftovers.py
for t in tests/test-*.py; do echo "$t -> $(python3 "$t" 2>&1 | tail -1)"; done
python3 runtime/realworld-bench.py
git status --short   # must be clean
```
Expected: guard PASS, all tests PASS/MET, bench ALL GATES PASS, working tree clean.

- [ ] **Step 2: Point the remote at the renamed repo**

```bash
# create the new repo (or rename on GitHub first), then:
git remote set-url origin git@github.com:Tatlatat/claude-reasonix-fleet.git
git remote -v
```
(If renaming the existing repo on GitHub, GitHub keeps a redirect from the old name.)

- [ ] **Step 3: Push the branch and open a PR (or push to a release branch)**

```bash
git push -u origin HEAD
gh pr create --title "claude-reasonix: rename + one-command packaging" \
  --body "Renames codex->reasonix (3 layers, all tests green), adds install.sh/uninstall.sh/README, de-hardcodes paths, drops dead deepseek-* agentTypes.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

- [ ] **Step 4: Confirm a clean clone installs**

```bash
TMP="$(mktemp -d)"; git clone git@github.com:Tatlatat/claude-reasonix-fleet.git "$TMP/c"
cd "$TMP/c" && ./install.sh; echo "exit=$?"
```
Expected: `✅ Ready` (with reasonix logged in) or a clear actionable prereq message — never a half-install or a codex-named file.

---

## Self-Review

**Spec coverage:** Layer A/B/C rename → Tasks 3/4/5; drop deepseek-* → Task 4; de-hardcode paths → Task 6; gitignore → Task 7; install.sh + dist patch → Task 8; uninstall/README → Task 9; publish → Task 10; backward-compat env → Task 3 Step 2; agentType 3-site sync → Task 4; guard test → Task 2. All spec sections covered.

**Placeholder scan:** no TBD/TODO; every code step has real code (env_first fallback, agentType edits, install.sh, patch applier). The only "for each of the 71/remaining" instructions are mechanical replacements with a shown exemplar — acceptable because the pattern is identical and exhaustively defined.

**Type/name consistency:** agentTypes `reasonix-worker/security/reviewer/verify` used identically in Task 4 Steps 1-3; renamed filenames identical across Tasks 3, 6, 8; `REASONIX_ACP_EPHEMERAL_SESSION` patch string identical in Task 8 Step 2 and Task 9 Step 2; `INSTALL_HOME` default `$HOME/.claude/reasonix-fleet` identical in Tasks 3, 8.
