# Engine Stability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the reasonix fleet run reliably on every workflow — stop lanes that read too many files from silently timing out at 600s, and stop verify-lane timeouts from being silently counted as "rejected" — WITHOUT regressing the proven token savings.

**Architecture:** Three independent, env-flag-gated mechanisms, all default OFF and byte-inert when off: A1a (shim passes a lower `outlineThresholdBytes` to `buildCodeToolset` so big files summarize instead of dumping raw — no vendored-bundle edit); A2 (enable the already-built gateway `overscope_rejection`); A3 (a timed-out/errored lane surfaces a structured `LANE_UNVERIFIED` content reply instead of a bare null/error, plus one PREFIX_GUIDE line telling controllers an empty verify ≠ rejected).

**Tech Stack:** Node ESM shim (`engine/run-lane.mjs`), Python gateway (`reasonix-native-gateway.py`), Python hook (`hooks/reasonix-workflow.py`). Tests are the repo's `tests/test-*.py` convention: a `main()` returning 0/1, run directly with `python3`.

## Global Constraints

- **Default OFF + byte-inert when off.** With every new flag unset, the assembled prompt, the shim request, and the lane reply must be byte-identical to today. A flag-off path that changes one byte breaks the 96-99% prefix cache and is a Critical defect. (Copied from spec hard constraint.)
- **Stability must NOT regress token savings.** Each task notes its effect on output/input/cache. A change that fixes a timeout but balloons tokens or craters cache must be redesigned, not shipped. (spec: stability-must-keep-savings)
- **No vendored-engine bundle edit.** A1 uses the existing `buildCodeToolset({ outlineThresholdBytes })` option — do NOT modify `vendor/reasonix-engine/dist/index.js`.
- **env_truthy footgun:** `env_truthy(*names, default="")` — positional args are env var NAMES; pass the default as the `default=` keyword, never as a second positional. (process-lessons)
- **Tests must be e2e where the real lane path differs from unit** — the gateway has a `/v1/messages` and a `/v1/chat/completions` path; a fix verified only on one is not verified. (process-lessons)
- Use the `env_first`/`env_int`/`env_truthy` REASONIX-first-with-CODEX-fallback idiom already in the gateway.

---

### Task 1: A1a — shim passes a lower outline threshold (big files summarize)

**Files:**
- Modify: `engine/run-lane.mjs:137` (the `buildCodeToolset({ rootDir })` call)
- Test: `tests/test-lane-outline-threshold.mjs` (new — node test, run with `node`)

**Interfaces:**
- Consumes: `buildCodeToolset(opts)` from the vendored engine — accepts `opts.outlineThresholdBytes` (a number of bytes; files larger than it return outline mode instead of full content).
- Produces: when `REASONIX_LANE_OUTLINE_THRESHOLD_BYTES` is set to a positive integer, the shim passes it as `buildCodeToolset({ rootDir, outlineThresholdBytes: N })`. When unset/invalid, the shim passes `{ rootDir }` exactly as today (byte-inert).

**Token effect:** REDUCES input (a large file returns metadata+head+outline instead of the full raw dump that bloats every subsequent lane iteration).

- [ ] **Step 1: Write the failing test** `tests/test-lane-outline-threshold.mjs`

```javascript
// Verifies the shim resolves a SMALLER outline threshold ONLY when the env var is a
// positive int, and otherwise passes undefined (engine default 64 KiB = today). We test
// the pure resolver, not a live DeepSeek call.
import assert from "node:assert";

// The shim must export resolveOutlineThreshold(env) returning a positive int or undefined.
const { resolveOutlineThreshold } = await import("../engine/run-lane.mjs");

let p = 0, f = 0;
const chk = (c, m) => { if (c) { p++; console.log("  ok  ", m); } else { f++; console.log("  FAIL", m); } };

chk(resolveOutlineThreshold({}) === undefined, "unset -> undefined (engine default, byte-inert)");
chk(resolveOutlineThreshold({ REASONIX_LANE_OUTLINE_THRESHOLD_BYTES: "" }) === undefined, "empty -> undefined");
chk(resolveOutlineThreshold({ REASONIX_LANE_OUTLINE_THRESHOLD_BYTES: "0" }) === undefined, "0 -> undefined (no zero/negative cap)");
chk(resolveOutlineThreshold({ REASONIX_LANE_OUTLINE_THRESHOLD_BYTES: "-5" }) === undefined, "negative -> undefined");
chk(resolveOutlineThreshold({ REASONIX_LANE_OUTLINE_THRESHOLD_BYTES: "abc" }) === undefined, "non-numeric -> undefined");
chk(resolveOutlineThreshold({ REASONIX_LANE_OUTLINE_THRESHOLD_BYTES: "32768" }) === 32768, "32768 -> 32768");
console.log(`\n${p} passed, ${f} failed`);
process.exit(f ? 1 : 0);
```

- [ ] **Step 2: Run it, verify it fails**

Run: `node tests/test-lane-outline-threshold.mjs`
Expected: FAIL — `resolveOutlineThreshold` is not exported yet (import error or undefined).

- [ ] **Step 3: Implement** — add the exported resolver near the top of `engine/run-lane.mjs` (after the imports, before the `try` block at line 135), and use it at the `buildCodeToolset` call.

Add the resolver (export so the test can import it without running a lane):

```javascript
// A1a: when REASONIX_LANE_OUTLINE_THRESHOLD_BYTES is a positive int, a lane reading a
// file larger than it gets the engine's outline (metadata+head+symbol outline) instead
// of the full raw dump — the mechanical fix for lanes that ingest too many files and
// time out. Unset/invalid -> undefined -> engine default 64 KiB (today's behavior).
export function resolveOutlineThreshold(env) {
  const raw = (env && env.REASONIX_LANE_OUTLINE_THRESHOLD_BYTES) || "";
  const n = Number.parseInt(String(raw).trim(), 10);
  return Number.isFinite(n) && n > 0 ? n : undefined;
}
```

Change line 137 from:

```javascript
  const toolset = await buildCodeToolset({ rootDir });
```

to:

```javascript
  const _outlineThreshold = resolveOutlineThreshold(process.env);
  const toolset = await buildCodeToolset(
    _outlineThreshold !== undefined ? { rootDir, outlineThresholdBytes: _outlineThreshold } : { rootDir });
```

Note: the `export` at module top-level is fine — the shim still runs its body on direct invocation. The test imports the module; the module's top-level lane code runs only when invoked as the entry script, but importing it executes the module body. To keep import side-effect-free, guard the lane-execution body so it runs only as the entry point. If the shim body is not already guarded, wrap the lane-run section in:

```javascript
// run the lane only when executed directly, not when imported by a test
import { fileURLToPath } from "node:url";
const _isEntry = process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1];
if (_isEntry) {
  // ... existing lane body (the try/catch that builds toolset, runs loop, writes stdout) ...
}
```

(If guarding the whole body is too invasive for this task, instead extract `resolveOutlineThreshold` into a tiny sibling module `engine/lane-opts.mjs` and import it in BOTH the shim and the test — that avoids the entry-guard entirely. Pick whichever keeps the diff smallest; the test imports the resolver from wherever it lives.)

- [ ] **Step 4: Run it, verify it passes**

Run: `node tests/test-lane-outline-threshold.mjs`
Expected: `6 passed, 0 failed`.

- [ ] **Step 5: Shim still parses + byte-inert when off**

Run: `node --check engine/run-lane.mjs`
Expected: no output (valid). Then confirm the off-path: `node -e "import('./engine/run-lane.mjs').then(m=>console.log(m.resolveOutlineThreshold({})))"` prints `undefined`.

- [ ] **Step 6: Commit**

```bash
git add engine/run-lane.mjs tests/test-lane-outline-threshold.mjs
git commit -m "feat(stability): A1a — shim passes lower outline threshold so big files summarize (default off)"
```

---

### Task 2: A2 — enable + verify OVERSCOPE_REJECT plumbing

**Files:**
- Modify: none (the mechanism exists) — this task ADDS a guard test proving it fires and is byte-inert off.
- Test: `tests/test-overscope-fires.py` (new)

**Interfaces:**
- Consumes: `overscope_rejection(task_text, cwd) -> str | None` and `_overscope_on()` in `reasonix-native-gateway.py` (already built; flag `CLAUDE_REASONIX_GATEWAY_OVERSCOPE_REJECT`, default "0").
- Produces: a test asserting the over-broad audit-shaped lane (the level-3.1 failure shape) is rejected when on, and None when off. (No code change — A2 is "turn the flag on in the launcher", done in Task 4.)

**Token effect:** REDUCES (a rejected lane costs ~0 vs a 600s death).

- [ ] **Step 1: Write the failing test** `tests/test-overscope-fires.py`

```python
#!/usr/bin/env python3
"""A2: the over-broad audit-shaped lane (level-3.1 failure shape) must be rejected when
OVERSCOPE_REJECT is on, and None when off (byte-inert)."""
import importlib.util, os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("gw", ROOT / "reasonix-native-gateway.py")
gw = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(gw)
FLAG = "CLAUDE_REASONIX_GATEWAY_OVERSCOPE_REJECT"
CWD = str(ROOT)
_P = _F = 0
def chk(c, m):
    global _P, _F
    if c: _P += 1; print(f"  ok   {m}")
    else: _F += 1; print(f"  FAIL {m}")
def main():
    # the real level-3.1 audit lane that timed out — names a directory-wide scope
    audit_lane = "Review SECURITY của API mobile: audit the whole codebase under lib/mobile and app/api"
    os.environ.pop(FLAG, None)
    chk(gw.overscope_rejection(audit_lane, CWD) is None, "OFF: audit lane -> None (byte-inert)")
    os.environ[FLAG] = "1"
    r = gw.overscope_rejection(audit_lane, CWD)
    chk(isinstance(r, str) and "decompose" in r.lower(), "ON: bulk audit lane -> reject string")
    # a narrow real lane must still pass
    chk(gw.overscope_rejection("read lib/mobile/jwt.ts and check exp validation", CWD) is None,
        "ON: narrow 1-file lane -> None (no false reject)")
    os.environ.pop(FLAG, None)
    print(f"\n{_P} passed, {_F} failed")
    return 1 if _F else 0
if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run it**

Run: `python3 tests/test-overscope-fires.py`
Expected: it likely PASSES immediately (the mechanism exists). If the bulk-audit phrasing is not caught, that is a real gap → widen `_OVERSCOPE_BULK_RE` in `reasonix-native-gateway.py` to catch "audit the whole codebase under <dir>" (the level-3.1 shape) and re-run. Verify zero false-positive on the narrow lane.

- [ ] **Step 3: Full guard + suite**

Run: `python3 tests/test-no-codex-leftovers.py` (PASS) and the existing `python3 tests/test-overscope-reject.py` (still passes — no regression to the prior overscope tests).

- [ ] **Step 4: Commit**

```bash
git add -f reasonix-native-gateway.py 2>/dev/null; git add tests/test-overscope-fires.py
git commit -m "test(stability): A2 — overscope rejects the level-3.1 audit-shaped lane, byte-inert off"
```

---

### Task 3: A3 — a timed-out/errored lane surfaces a clear UNVERIFIED reply

**Files:**
- Modify: `reasonix-native-gateway.py` — the timeout/error path (around line 2132 / the `_run_attempts` caller) and the streaming hollow-guard region (around line 2804).
- Modify: `hooks/reasonix-workflow.py` — add ONE point to `PREFIX_GUIDE_TEXT`.
- Test: `tests/test-lane-unverified-marker.py` (new)

**Interfaces:**
- Consumes: `GatewayError(504, "reasonix_timeout", ...)` raised at line 2132; the streaming hollow-guard at line ~2804 that emits a marker text block when `emitted_real == 0`.
- Produces: a helper `lane_unverified_reply(reason: str) -> str` returning a fixed, machine-readable marker string beginning with `LANE_UNVERIFIED:` so a workflow can distinguish "could not verify (timeout/error)" from "verified = false". Gated by `CLAUDE_REASONIX_GATEWAY_LANE_FAIL_MARKER` (default "1" — surfacing a clear marker instead of a bare error is strictly safer; set 0 to restore the old bare-error behavior).

**Token effect:** neutral (only a short marker string on a lane that already failed).

- [ ] **Step 1: Write the failing test** `tests/test-lane-unverified-marker.py`

```python
#!/usr/bin/env python3
"""A3: a lane that times out/errors surfaces a LANE_UNVERIFIED marker (not a bare null),
so a workflow never mis-counts a verify timeout as a 'rejected' finding."""
import importlib.util, os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("gw", ROOT / "reasonix-native-gateway.py")
gw = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(gw)
FLAG = "CLAUDE_REASONIX_GATEWAY_LANE_FAIL_MARKER"
_P = _F = 0
def chk(c, m):
    global _P, _F
    if c: _P += 1; print(f"  ok   {m}")
    else: _F += 1; print(f"  FAIL {m}")
def main():
    # marker on (default): returns a machine-readable UNVERIFIED string carrying the reason
    os.environ.pop(FLAG, None)  # default on
    r = gw.lane_unverified_reply("engine shim timed out after 600s")
    chk(isinstance(r, str) and r.startswith("LANE_UNVERIFIED:"), "default on: starts with LANE_UNVERIFIED:")
    chk("timed out" in r, "carries the reason")
    chk("rejected" not in r.lower(), "must NOT say 'rejected' (the whole point)")
    # marker off: empty -> old bare behavior (caller falls back to raising)
    os.environ[FLAG] = "0"
    chk(gw.lane_unverified_reply("x") == "", "off: empty string (restore bare-error behavior)")
    os.environ.pop(FLAG, None)
    print(f"\n{_P} passed, {_F} failed")
    return 1 if _F else 0
if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run it, verify it fails**

Run: `python3 tests/test-lane-unverified-marker.py`
Expected: FAIL — `lane_unverified_reply` not defined.

- [ ] **Step 3: Implement the helper** in `reasonix-native-gateway.py` (near the hollow-guard / GatewayError region):

```python
def _lane_fail_marker_on() -> bool:
    return env_truthy("CLAUDE_REASONIX_GATEWAY_LANE_FAIL_MARKER",
                      "CLAUDE_CODEX_GATEWAY_LANE_FAIL_MARKER", default="1")


def lane_unverified_reply(reason: str) -> str:
    """A3: when a lane times out/errors, return a machine-readable marker so a workflow
    distinguishes 'could not verify' from 'verified=false'. A verify lane that gets this
    must be treated UNVERIFIED and its finding KEPT, never silently rejected (the
    level-3.1 bug: a timed-out verify with an empty verdict was counted as 'rejected').
    Returns '' when the flag is off (caller restores the old bare-error behavior)."""
    if not _lane_fail_marker_on():
        return ""
    return (f"LANE_UNVERIFIED: this lane did not complete ({reason}). "
            "Treat as UNVERIFIED (could not check), NOT as a rejected/false finding — "
            "keep the item and re-run with a smaller scope.")
```

- [ ] **Step 4: Wire it at the timeout path** — at line ~2132, instead of always raising on timeout, surface the marker as a successful lane reply when the flag is on. Change:

```python
            raise GatewayError(504, "reasonix_timeout", f"engine shim timed out after {timeout:g}s")
```

to:

```python
            _mk = lane_unverified_reply(f"engine shim timed out after {timeout:g}s")
            if _mk:
                return _mk, {"input_tokens": 0, "output_tokens": estimate_tokens({"text": _mk}),
                             "reasonix_cost_usd": 0.0, "reasonix_cache_pct": None}
            raise GatewayError(504, "reasonix_timeout", f"engine shim timed out after {timeout:g}s")
```

(This returns the marker as the lane's `(text, usage)` tuple — the same shape `_attempt` returns — so the lane "succeeds" with readable UNVERIFIED content instead of a null. Verify the surrounding function's return type is `tuple[str, JSON]`; match it exactly.)

- [ ] **Step 5: Add the PREFIX_GUIDE point** in `hooks/reasonix-workflow.py` — append to `PREFIX_GUIDE_TEXT` (after the last numbered point):

```python
    "10. VERIFY-FAIL IS NOT REJECTION — a verify/check lane that returns empty, errors, "
    "or carries a 'LANE_UNVERIFIED:' marker means the lane COULD NOT verify (e.g. timed "
    "out), NOT that the finding is false. Default to KEEPING such a finding marked "
    "'unverified'; never move it to a 'rejected' bucket on an empty/failed verdict. In "
    "code: treat `!verdict?.confirmed` as rejected ONLY when the verdict actually came "
    "back with confirmed:false — an absent/empty verdict is UNVERIFIED.\n"
```

- [ ] **Step 6: Run the test + guard + hook test**

Run: `python3 tests/test-lane-unverified-marker.py` → `4 passed, 0 failed`.
Run: `python3 tests/test-no-codex-leftovers.py` → PASS.
Run: `python3 -c "import importlib.util; from pathlib import Path; s=importlib.util.spec_from_file_location('h',Path('hooks/reasonix-workflow.py')); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); assert 'LANE_UNVERIFIED' in m.PREFIX_GUIDE_TEXT; print('guide point added')"` → `guide point added`.

- [ ] **Step 7: Commit**

```bash
git add -f reasonix-native-gateway.py hooks/reasonix-workflow.py
git add tests/test-lane-unverified-marker.py
git commit -m "feat(stability): A3 — lane timeout surfaces LANE_UNVERIFIED marker + guide so verify-fail != rejected"
```

---

### Task 4: Promote the stability flags in the launcher (measured-then-on)

**Files:**
- Modify: `bin/claude-reasonix` (the reasonix-flavor env-export block, near the A+broaden exports added earlier)
- Test: manual — `bash -n bin/claude-reasonix` + a flag-resolution check.

**Interfaces:**
- Consumes: `REASONIX_LANE_OUTLINE_THRESHOLD_BYTES` (Task 1), `CLAUDE_REASONIX_GATEWAY_OVERSCOPE_REJECT` (Task 2), `CLAUDE_REASONIX_GATEWAY_LANE_FAIL_MARKER` (Task 3, default on already).
- Produces: the launcher exports the stability flags so workflows get them. A1a threshold and A2 are turned ON here with overridable defaults; A3 marker is already default-on so it needs no export (but export it for observability).

**IMPORTANT — measure before this task flips A1a/A2 on:** do NOT hard-enable A1a/A2 in the launcher until the sub-project B audit re-run measures (a) timeouts gone, (b) token/cache not regressed, (c) precision held. Until then, keep this task's A1a/A2 exports COMMENTED with a note, or set to off, and only the A3 marker on. (This task ships the wiring; the on/off decision is the B measurement.)

- [ ] **Step 1: Add the exports** (after the existing `CLAUDE_REASONIX_GATEWAY_READ_SUMMARY` block):

```bash
  # STABILITY (sub-project A) — A3 marker on by default (strictly safer: a timed-out
  # lane surfaces UNVERIFIED instead of a bare error). A1a outline-threshold + A2
  # overscope-reject ship OFF here pending the vatlieu-kho audit measurement (must show
  # timeouts gone AND token/cache not regressed AND precision held before flipping on).
  : "${CLAUDE_REASONIX_GATEWAY_LANE_FAIL_MARKER:=1}"
  : "${CLAUDE_REASONIX_GATEWAY_OVERSCOPE_REJECT:=0}"
  : "${REASONIX_LANE_OUTLINE_THRESHOLD_BYTES:=0}"
  export CLAUDE_REASONIX_GATEWAY_LANE_FAIL_MARKER CLAUDE_REASONIX_GATEWAY_OVERSCOPE_REJECT REASONIX_LANE_OUTLINE_THRESHOLD_BYTES
```

(`:=0` / `:=0` keep A1a/A2 off until measured; `REASONIX_LANE_OUTLINE_THRESHOLD_BYTES=0` resolves to `undefined` in the shim = engine default = byte-inert.)

- [ ] **Step 2: Launcher parses**

Run: `bash -n bin/claude-reasonix`
Expected: no output.

- [ ] **Step 3: Flag-resolution check**

Run a sourced simulation of the `:=` block and assert: marker resolves "1", overscope "0", outline "0"; and an override (e.g. `CLAUDE_REASONIX_GATEWAY_OVERSCOPE_REJECT=1` set before) is respected.

- [ ] **Step 4: Commit**

```bash
git add bin/claude-reasonix
git commit -m "feat(stability): wire A1a/A2/A3 flags in launcher (A3 on; A1a/A2 off pending audit measurement)"
```

---

## Self-Review

- **Spec coverage:** A1a → Task 1; A1b → no work (available, not relied on — spec says so); A2 → Task 2 + Task 4 wiring; A3 → Task 3; launcher promotion + measure-gate → Task 4. The vatlieu-kho measurement is sub-project B (separate), referenced in Task 4's gate. Covered.
- **Placeholder scan:** every code step shows the exact code/diff; no TBD. The one judgment call (entry-guard vs sibling module in Task 1 Step 3) is spelled out with both concrete options.
- **Type consistency:** `resolveOutlineThreshold(env) -> number|undefined`, `lane_unverified_reply(reason) -> str`, `_lane_fail_marker_on() -> bool`, `_overscope_on()/overscope_rejection` (existing) used consistently. A3's timeout-path return matches `_attempt`'s `(text, usage)` tuple.
- **Token effect noted per task** (Global Constraint): T1 reduces input, T2 reduces, T3 neutral. None regress cache when off (all byte-inert).

## Execution note
A1a and A2 ship default-OFF and are NOT flipped on in the launcher until the sub-project B
audit re-run measures them (timeouts gone + token/cache held + precision held). That
measurement is the real-world test and the B deliverable.
