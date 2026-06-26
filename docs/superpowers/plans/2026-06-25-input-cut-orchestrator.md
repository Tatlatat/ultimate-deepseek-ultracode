# Input-Cut (Orchestrator) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Cut lane INPUT tokens with two mechanical, adoption-independent gateway levers: (Task 1) broaden the read-lane classifier so the proven Lever A (read-summary, −94.7% output) reaches the read-heavy lanes that currently evade it; (Task 2) reject-on-overscope — the gateway refuses a lane whose declared file scope is too large and returns a structured error telling the controller to decompose.

**Architecture:** Both levers live in `reasonix-native-gateway.py`, default OFF, byte-inert when off (no shared-prefix/toolSpec change). Neither depends on flash adopting a tool (the B/C/D failure). Root cause they attack: a lane's big non-cached input is bucket-3 — the raw files it reads at runtime (measured max 532,469 tok, one 833-file lane); the orchestrator controls bucket-3 only via the per-lane task text (how much file territory the lane is told to cover).

**Tech Stack:** Python 3 (gateway), pytest-free check scripts (the repo's `tests/test-*.py` convention: a `main()` returning 0/1, run directly).

## Global Constraints

- Every new behavior is gated behind an env flag that DEFAULTS OFF, using the `env_first`/`env_int` REASONIX-first-with-CODEX-fallback idiom already in the file (see `_read_summary_on` line ~1394, `_read_cache_on` line ~347).
- When the flag is off the assembled prompt + engine request must be BYTE-IDENTICAL to today (no directive, no cap, no rejection). A flag-off path that changes one prompt byte breaks the 96-99% prefix cache and is a Critical defect.
- Classify on `lane_task_text(messages)` (raw task text), NEVER the assembled prompt (the F-trap: injected directives carry edit/read keywords).
- Classifier order is fixed: synthesize → edit → read → unknown. Synthesis-intent must keep winning ties over the new read verbs (so "review and merge the findings" stays synthesize, not read).
- No new toolSpec, no semantic_search, no prefix injection.
- Measure-then-promote: ship default-OFF; promotion is a separate, A/B-measured decision.

---

### Task 1: Broaden the read-lane classifier (extend Lever A's reach)

**Files:**
- Modify: `reasonix-native-gateway.py` — `_READER_INTENT_RE` (line ~1195) and a new gated wrapper around its use in `classify_lane_type` (line ~1240).
- Test: `tests/test-reader-classifier-broaden.py` (new)

**Interfaces:**
- Consumes: `classify_lane_type(tools, prompt_text)`, `_SYNTHESIS_INTENT_RE`, `_EDIT_INTENT_RE`, `is_synthesis_prompt`.
- Produces: a broadened read match that is FLAG-GATED (`CLAUDE_REASONIX_GATEWAY_READER_BROADEN`, default "0"). When off, `classify_lane_type` returns exactly today's labels. When on, the verbs analyze/review/examine/investigate/audit/find/inspect/summarize/study/trace/explain (when the lane is NOT synthesis-intent and NOT edit-intent) classify as `read`.

**Why gated:** broadening the classifier changes which lanes Lever A caps. That is a behavior change, so it must be opt-in and measured, not silently flipped on.

**The collision risk (the reason this needs a test + review):** several new verbs co-occur with synthesis ("review and merge", "examine across all findings"). Because `classify_lane_type` checks synthesize BEFORE read, synthesis-intent already wins — but the test must PROVE it: every new verb in a synthesis context must still classify `synthesize`, and in an edit context still `edit`.

- [ ] **Step 1: Write the failing test** `tests/test-reader-classifier-broaden.py`

```python
#!/usr/bin/env python3
"""Broadened read classifier — flag off = identical to today; flag on = read-heavy
verbs classify 'read' WITHOUT stealing synthesis/edit lanes."""
import importlib.util, os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("gw", ROOT / "reasonix-native-gateway.py")
gw = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(gw)
FLAG = "CLAUDE_REASONIX_GATEWAY_READER_BROADEN"
_P = _F = 0
def chk(c, m):
    global _P, _F
    if c: _P += 1; print(f"  ok   {m}")
    else: _F += 1; print(f"  FAIL {m}")
def main():
    os.environ.pop(FLAG, None)
    # OFF: today's behavior — these verbs are 'unknown'
    for v in ("analyze", "review", "audit", "find", "examine", "inspect", "summarize"):
        chk(gw.classify_lane_type(None, f"{v} the auth module") == "unknown",
            f"OFF: '{v} the auth module' -> unknown (unchanged)")
    chk(gw.classify_lane_type(None, "read the file src/x.py") == "read", "OFF: literal read still read")
    # ON: read-heavy verbs now classify 'read'
    os.environ[FLAG] = "1"
    for v in ("analyze", "review", "audit", "find", "examine", "inspect", "investigate", "study", "trace", "explain"):
        chk(gw.classify_lane_type(None, f"{v} the auth module in src/auth.py") == "read",
            f"ON: '{v} ...' -> read (Lever A now reaches it)")
    # ON: synthesis-intent STILL wins (no theft)
    chk(gw.classify_lane_type(None, "review and merge the findings into one report") == "synthesize",
        "ON: 'review and merge' stays synthesize")
    chk(gw.classify_lane_type(None, "examine all findings and consolidate across sources") == "synthesize",
        "ON: 'examine ... consolidate across sources' stays synthesize")
    # ON: edit-intent STILL wins
    chk(gw.classify_lane_type(None, "review and then refactor the module") == "edit",
        "ON: 'review and refactor' stays edit")
    os.environ.pop(FLAG, None)
    print(f"\n{_P} passed, {_F} failed")
    return 1 if _F else 0
if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run it, verify it fails** — `python3 tests/test-reader-classifier-broaden.py` → FAIL (broaden flag not implemented; ON cases classify 'unknown').

- [ ] **Step 3: Implement** — add the gated broadened verb set. Add near the other reader helpers:

```python
_READER_BROADEN_RE = re.compile(
    r"\b(analyze|analyse|review|examine|investigate|audit|inspect|study|trace|explain|summari[sz]e|find\b|walk through|describe what)\b",
    re.I)

def _reader_broaden_on() -> bool:
    return env_truthy("CLAUDE_REASONIX_GATEWAY_READER_BROADEN",
                      os.getenv("CLAUDE_CODEX_GATEWAY_READER_BROADEN", "0"))
```

In `classify_lane_type`, after the existing `_READER_INTENT_RE` check and before `return "unknown"`:

```python
    if _READER_INTENT_RE.search(pt):
        return "read"
    if _reader_broaden_on() and _READER_BROADEN_RE.search(pt):
        return "read"
    return "unknown"
```

(Synthesis and edit are checked earlier in the function, so they keep winning ties — the test proves it.)

- [ ] **Step 4: Run it, verify it passes** — `python3 tests/test-reader-classifier-broaden.py` → PASS.

- [ ] **Step 5: Run the guard + classifier regression** — `python3 tests/test-no-codex-leftovers.py` and `python3 tests/test-lane-classify.py` → both PASS (no existing label changed when flag off).

- [ ] **Step 6: Commit** — `git add -f reasonix-native-gateway.py && git add tests/test-reader-classifier-broaden.py && git commit`.

---

### Task 2: Reject-on-overscope (gateway refuses a too-broad lane)

**Files:**
- Modify: `reasonix-native-gateway.py` — a new `lane_file_scope_count(task_text, cwd)` + `overscope_rejection(task_text, cwd)` helper, called in the request path (near where the lane prompt is assembled / before the engine spawn, around line ~1543/1950).
- Test: `tests/test-overscope-reject.py` (new)

**Interfaces:**
- Consumes: the same literal-path resolver idea as `predict_prefetch_files` (port the exists-under-cwd resolve loop into the gateway — do NOT import the hook; copy the minimal resolver so the gateway stays standalone).
- Produces: `overscope_rejection(task_text, cwd) -> str | None` — returns None (no rejection) when the flag is off OR the lane's resolved scope is within threshold; returns a structured error STRING when on AND the lane names > `CLAUDE_REASONIX_GATEWAY_OVERSCOPE_MAX_FILES` (default 10) existing files OR matches a non-enumerable bulk-scope phrase (`audit the (whole |entire )?codebase`, `all files (in|under)`, `every file`, `the whole repo`). The caller returns this string to the controller as the lane result instead of spawning the lane.

**Why a structured error, not auto-split:** the JS workflow wrapper cannot call `parallel()` or rewrite the controller's await-DAG (verified — auto-split is unbuildable at the hook point). Fail-loud rejection forces the controller to re-decompose, which is the user-sanctioned shape (hard-cap was rejected; decomposition is required).

- [ ] **Step 1: Write the failing test** `tests/test-overscope-reject.py`

```python
#!/usr/bin/env python3
"""Reject-on-overscope: flag off = always None (byte-inert); flag on = reject a lane
naming >N existing files or a bulk-codebase scope, else None."""
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
    os.environ.pop(FLAG, None)
    # OFF: always None (no rejection, byte-inert)
    chk(gw.overscope_rejection("audit the entire codebase", CWD) is None, "OFF: bulk scope -> None")
    chk(gw.overscope_rejection("read README.md", CWD) is None, "OFF: small lane -> None")
    os.environ[FLAG] = "1"
    # ON: small/normal lane -> None (not rejected)
    chk(gw.overscope_rejection("read the file README.md and summarize it", CWD) is None,
        "ON: 1-file lane -> None (allowed)")
    # ON: bulk non-enumerable scope -> rejection string
    r = gw.overscope_rejection("audit the entire codebase for bugs", CWD)
    chk(isinstance(r, str) and "decompose" in r.lower(), "ON: 'audit the entire codebase' -> reject string")
    chk(isinstance(gw.overscope_rejection("review all files under src", CWD), str), "ON: 'all files under src' -> reject")
    # ON: >N explicit existing files -> rejection (build a prompt naming 11 real files)
    import glob
    many = [os.path.relpath(p, CWD) for p in glob.glob(str(ROOT / "tests" / "test-*.py"))][:11]
    if len(many) >= 11:
        prompt = "read these files: " + " ".join(many)
        chk(isinstance(gw.overscope_rejection(prompt, CWD), str), "ON: 11 named files -> reject")
    os.environ.pop(FLAG, None)
    print(f"\n{_P} passed, {_F} failed")
    return 1 if _F else 0
if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run it, verify it fails** — `python3 tests/test-overscope-reject.py` → FAIL (`overscope_rejection` not defined).

- [ ] **Step 3: Implement** — add the helper (resolver copied minimally, bulk-scope regex, threshold env):

```python
_OVERSCOPE_BULK_RE = re.compile(
    r"\b(audit|review|scan|analyze|check|read)\s+(the\s+)?(whole|entire|full)\s+(codebase|repo|repository|project)\b"
    r"|\ball\s+files?\s+(in|under|across)\b|\bevery\s+file\b|\bthe\s+whole\s+(repo|codebase)\b",
    re.I)

def _overscope_on() -> bool:
    return env_truthy("CLAUDE_REASONIX_GATEWAY_OVERSCOPE_REJECT",
                      os.getenv("CLAUDE_CODEX_GATEWAY_OVERSCOPE_REJECT", "0"))

def _overscope_max_files() -> int:
    return env_int("CLAUDE_REASONIX_GATEWAY_OVERSCOPE_MAX_FILES",
                   "CLAUDE_CODEX_GATEWAY_OVERSCOPE_MAX_FILES", default=10)

def lane_file_scope_count(task_text: str, cwd: str | None) -> int:
    """Count DISTINCT existing files the task text literally names under cwd (the same
    exists-under-cwd resolve as predict_prefetch_files, copied so the gateway is
    standalone). A token that does not resolve to a real file is not counted."""
    if not task_text or not cwd:
        return 0
    try:
        base = Path(cwd).expanduser().resolve()
    except Exception:
        return 0
    seen = set()
    for match in _PREFETCH_PATH_RE.finditer(task_text):
        token = match.group(1)
        candidate = (base / token) if not os.path.isabs(token) else Path(token)
        try:
            resolved = candidate.expanduser().resolve()
        except Exception:
            continue
        if resolved.is_file():
            seen.add(str(resolved))
    return len(seen)

def overscope_rejection(task_text: str, cwd: str | None) -> str | None:
    """None unless OVERSCOPE_REJECT is on AND the lane is over-broad (a bulk
    non-enumerable scope phrase, OR > max-files distinct named files). When it fires,
    returns a structured error telling the controller to decompose into per-file lanes."""
    if not _overscope_on():
        return None
    pt = task_text or ""
    bulk = bool(_OVERSCOPE_BULK_RE.search(pt))
    n = lane_file_scope_count(pt, cwd)
    if not bulk and n <= _overscope_max_files():
        return None
    reason = ("a bulk codebase/directory scope" if bulk
              else f"{n} named files (> {_overscope_max_files()})")
    return ("LANE REJECTED (overscope): this lane covers " + reason + ". A single DeepSeek-flash "
            "lane that ingests many files balloons input tokens and collapses cache (measured: one "
            "833-file lane = 532K tokens, 75% cache, 18 min). DECOMPOSE: emit one lane per file / "
            "module / focused question via parallel(), then one synthesize lane. Re-dispatch as "
            "narrow lanes.")
```

Wire it into the request path: the `PREFETCH_PATH_RE` must already exist in the gateway — if it lives only in the hook, copy the pattern into the gateway near the new helper (a literal-path regex; do not import the hook). The caller (where the lane prompt is finalized, before the engine spawn) does:

```python
    _rej = overscope_rejection(lane_task_text(normalized), cwd)
    if _rej is not None:
        return _rejection_response(_rej)   # return the structured error AS the lane result
```

`_rejection_response` formats the string into the same Anthropic/SSE shape a normal lane reply uses (reuse the existing response builder; the lane "succeeds" with the rejection text as its content so the controller reads it).

- [ ] **Step 4: Run it, verify it passes** — `python3 tests/test-overscope-reject.py` → PASS.

- [ ] **Step 5: Byte-inert check** — confirm `overscope_rejection(..)` returns None for ALL inputs when the flag is off (the test's OFF cases cover this), and that no caller path runs when off.

- [ ] **Step 6: Guard + suite** — `python3 tests/test-no-codex-leftovers.py` PASS; run the full `tests/test-*.py` sweep, no FAIL.

- [ ] **Step 7: Commit** — `git add -f reasonix-native-gateway.py && git add tests/test-overscope-reject.py && git commit`.

---

## Self-Review

- Spec coverage: Task 1 = classifier broaden (Lever A reach); Task 2 = reject-on-overscope (bucket-3 at source). Both default OFF, byte-inert, adoption-independent — matches the workflow verdict.
- Placeholders: none — full test + impl code in each step.
- Type consistency: `overscope_rejection -> str | None`, `lane_file_scope_count -> int`, `_reader_broaden_on/_overscope_on -> bool` used consistently.
- Open risk for the reviewer: the exact CALLER line for Task 2 (where to insert the rejection) and the `_rejection_response` builder must match the gateway's real response path — the implementer verifies against the live code, not this plan's line guesses.

---

## A/B Validation on real DeepSeek (2026-06-25)

Both levers were A/B-measured on real reasonix+DeepSeek via `runtime/input-cut-ab.py`.
A harness bug was found and fixed mid-run: the ledger cache field is `cache_pct`, NOT
`reasonix_cache_pct` (the latter reads None → looks like 0% cache). The honest signal is
EFFECTIVE-MISS input `sum(input*(1-cache_pct/100))`, not raw input_tokens (most of which
is the cached ~14K shared prefix).

### READER_BROADEN + Lever A — ✅ WORKS (with a cap caveat)

3 read-heavy lanes using the evading verb "analyze ..." (which classified `unknown`
before broaden, so Lever A never reached them):
- **Output: 26,861 → 1,536 tok = −94.3%.** Broaden routes the lanes to `read`, so Lever A's
  cap+summary fires — extending A's proven −94% win to the read-heavy verbs it used to miss.
- **Quality caveat (real, root-caused):** 2 of 3 ON lanes went HOLLOW ("returned no content")
  at A's default 512 cap on the LARGEST files (134KB gateway, 16KB bench). Root cause confirmed:
  raising the read cap to 2048 made the 134KB file return a correct `{findings}` summary (1281
  chars). So A's fixed 512 cap is too tight to summarize the largest files — flash truncates the
  JSON mid-array → hollow. FIX DIRECTION: bump A's read cap (768–1024) or retry a hollow read
  lane once at a higher cap. Broaden itself is correct; the limit is A's cap sizing on big files.

### OVERSCOPE_REJECT — ⚠️ FIRES correctly, but NET-WASH on a moderate lane

1 giant lane over 12 files (OFF) vs reject + 12 decomposed per-file lanes (ON):

| | raw input | weighted cache | eff_miss (cost driver) | output | cost units |
|---|---|---|---|---|---|
| GIANT 1-lane (OFF) | 26,053 | 57.0% | **11,203** | 2,184 | 806,776 |
| DECOMPOSED 12-lane (ON) | 185,463 | 94.0% | **11,118** | 1,917 | 934,963 |

- `reject_fired = true` — the lever works mechanically.
- On EFFECTIVE input the two are a **WASH** (11,203 vs 11,118, <1%); decompose's total cost is
  slightly HIGHER (more lanes = more fixed overhead, more summaries).
- **Why:** on a moderate file count the giant lane's COLD miss (11K) is small enough that 12
  small lanes' overhead cancels the win. OVERSCOPE only pays off at the EXTREME — the real
  833-file/532K-token lane, where the giant's cold miss is ~200K+ and 57% cache can't rescue it.
- **Honest scope:** OVERSCOPE is a GUARDRAIL against catastrophic lanes (the 532K/18-min shape),
  NOT a routine input-cut. Keep default OFF; it earns its keep only when a controller writes a
  genuinely catastrophic lane.

### Net for goal #2 (cut input)

- **READER_BROADEN is the real input lever** — it extends the proven Lever A (the only thing that
  actually cut tokens) to ~5× more lanes (read-heavy verbs were 20.2%→ most read lanes). Promote
  AFTER fixing A's cap so big-file summaries don't go hollow.
- **OVERSCOPE is a safety guardrail**, not a routine cut — promote only as a catastrophe-limiter.
