# Reasonix Multi-Agent Cache & Synthesis — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make concurrent reasonix lanes in native dynamic workflow save tokens (high prompt-cache hit) AND let the heavy final synthesis lane complete for real via in-engine map-reduce — never an empty fallback, never an infinite loop.

**Architecture:** All logic lives at the gateway (`codex-native-gateway.py`) plus one reasonix skill + config — transparent to Claude Code, so UltraCode/Workflow/deep-research fan-out is unchanged. Five independently-gated components: E measurement, D map-reduce synthesis (the big win), C loop-breaker, B prime-gate, A prefix-stability check.

**Tech Stack:** Python 3.9+ (gateway, stdlib only — `threading`, `json`, `hashlib`, `time`), reasonix CLI (Go binary, acp mode), Markdown skill files. Tests are plain `python3 tests/<name>.py` scripts (no pytest) that import the gateway via `importlib` and use a local `expect(cond, msg)` helper printing `PASS:`.

## Global Constraints

- **Test convention (verbatim):** every test file imports the gateway with `spec = importlib.util.spec_from_file_location("gw", ROOT / "codex-native-gateway.py"); gw = importlib.util.module_from_spec(spec); spec.loader.exec_module(gw)` where `ROOT = Path(__file__).resolve().parent.parent`. Define `def expect(cond, msg): \n  if not cond: raise SystemExit(f"FAIL: {msg}")`. End with `if __name__ == "__main__":` calling each test then `print("PASS: <name>")`. Run with `python3 tests/<file>.py`.
- **Stdlib only** in the gateway — no new pip deps.
- **Every component has an env kill-switch**, default ON unless noted, so it can be disabled without code change.
- **Fail-open always:** any new gating must never hang a lane — bounded waits, `finally`-release, and a schema-valid last resort.
- **Do NOT modify** Claude Code workflow scripts (`/deep-research`, UltraCode) — they are harness-generated. Only the gateway + reasonix skill/config.
- **Gateway file:** `/Users/tatlatat/.claude/codex-fleet/codex-native-gateway.py`. Tests dir: `/Users/tatlatat/.claude/codex-fleet/tests/`. Reasonix reads skills from `~/.claude/skills/`.
- **Reasonix model names:** workers run `claude-reasonix-flash` → DeepSeek `deepseek-v4-flash` (cheap). Keep flash for sub-agents (priority: cost).
- **Commit message footer (verbatim, end every commit):**
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>` then `Claude-Session: https://claude.ai/code/session_01QuE5K2DJUFeynEoAWiRhwg`

---

## File Structure

| File | Responsibility | Tasks |
|---|---|---|
| `codex-native-gateway.py` (modify) | all gateway-side logic: measurement helper, synthesis detection+inject, loop counter, prime-gate refinements | 1,2,3,4,5,6 |
| `tests/test-cache-measure.py` (create) | unit-test the weighted-cache + miss-classification helper | 1 |
| `~/.claude/skills/map-reduce-synthesis/SKILL.md` (create) | the in-engine map-reduce subagent skill | 2 |
| `tests/test-synthesis-detect.py` (create) | unit-test detect-heavy-synthesis + prompt injection | 3 |
| `tests/test-loop-breaker.py` (create) | unit-test per-lane retry counter → forced fallback | 4 |
| `tests/test-prime-gate.py` (create) | unit-test refined prime-gate (32KB key, fail-open) | 5 |
| `tests/test-prefix-stability-check.py` (create) | assert prefix4k collapses to 1 family across agent types | 6 |

---

### Task 1: Cache measurement + miss-classification helper (Component E)

**Files:**
- Modify: `codex-native-gateway.py` — add two functions near `append_reasonix_cost` (~L800).
- Test: `tests/test-cache-measure.py` (create)

**Interfaces:**
- Produces: `weighted_cache(rows: list[dict]) -> dict` returning `{"weighted_pct": float, "total_in": int, "total_miss": int, "n": int}`; and `classify_miss(rows: list[dict]) -> dict` returning `{"cold_prefix": int, "loop_inflation": int, "unique_tail": int}` (token counts). Each row is a `reasonix-cost.jsonl` record with keys `input_tokens`, `cache_pct`.
- Consumes: nothing (pure functions).

- [ ] **Step 1: Write the failing test**

```python
# tests/test-cache-measure.py
from __future__ import annotations
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gw", ROOT / "codex-native-gateway.py")
gw = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(gw)


def expect(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")


def test_weighted_cache_basic():
    rows = [
        {"input_tokens": 1000, "cache_pct": 100.0},
        {"input_tokens": 1000, "cache_pct": 0.0},
    ]
    r = gw.weighted_cache(rows)
    expect(abs(r["weighted_pct"] - 50.0) < 1e-6, f"50% expected, got {r['weighted_pct']}")
    expect(r["total_in"] == 2000, "total_in")
    expect(r["total_miss"] == 1000, "total_miss")
    expect(r["n"] == 2, "n counts rows with cache data")


def test_weighted_cache_ignores_missing_cache():
    rows = [{"input_tokens": 500, "cache_pct": None}, {"input_tokens": 500, "cache_pct": 90.0}]
    r = gw.weighted_cache(rows)
    expect(r["n"] == 1, "only the row with numeric cache_pct counts")
    expect(abs(r["weighted_pct"] - 90.0) < 1e-6, "90%")


def test_weighted_cache_empty():
    r = gw.weighted_cache([])
    expect(r["weighted_pct"] == 0.0 and r["total_in"] == 0 and r["n"] == 0, "empty safe")


def test_classify_miss_buckets():
    rows = [
        {"input_tokens": 200_000, "cache_pct": 82.0},  # loop_inflation (>150k)
        {"input_tokens": 10_000, "cache_pct": 40.0},    # unique_tail (<60% & small)
        {"input_tokens": 10_000, "cache_pct": 85.0},    # cold_prefix (mid)
    ]
    c = gw.classify_miss(rows)
    expect(c["loop_inflation"] > 0, "big lane miss is loop_inflation")
    expect(c["unique_tail"] > 0, "low-cache small lane miss is unique_tail")
    expect(c["cold_prefix"] > 0, "mid-cache lane miss is cold_prefix")


if __name__ == "__main__":
    test_weighted_cache_basic()
    test_weighted_cache_ignores_missing_cache()
    test_weighted_cache_empty()
    test_classify_miss_buckets()
    print("PASS: cache measure")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 tests/test-cache-measure.py`
Expected: FAIL — `AttributeError: module 'gw' has no attribute 'weighted_cache'`

- [ ] **Step 3: Write minimal implementation**

Add to `codex-native-gateway.py` just before `def append_reasonix_cost`:

```python
def weighted_cache(rows: list[JSON]) -> JSON:
    """Weighted cache-hit rate over reasonix-cost rows: sum(in*cache%)/sum(in).
    Only rows with a numeric cache_pct count; returns zeros on empty."""
    total_in = 0
    hit = 0.0
    n = 0
    for r in rows:
        it = r.get("input_tokens") or 0
        cp = r.get("cache_pct")
        if isinstance(cp, (int, float)):
            total_in += it
            hit += it * cp / 100.0
            n += 1
    miss = total_in - hit
    return {
        "weighted_pct": (100.0 * hit / total_in) if total_in else 0.0,
        "total_in": total_in,
        "total_miss": int(round(miss)),
        "n": n,
    }


def classify_miss(rows: list[JSON]) -> JSON:
    """Bucket missed tokens into cold_prefix (fixable by prime gate), loop_inflation
    (big lanes re-fed history, fixable by loop-breaker/map-reduce), and unique_tail
    (genuinely novel content). Heuristic by input size + cache band."""
    cold = loop = unique = 0
    for r in rows:
        it = r.get("input_tokens") or 0
        cp = r.get("cache_pct")
        if not isinstance(cp, (int, float)):
            continue
        miss = int(round(it * (1 - cp / 100.0)))
        if it > 150_000:
            loop += miss
        elif cp < 60 and it < 30_000:
            unique += miss
        else:
            cold += miss
    return {"cold_prefix": cold, "loop_inflation": loop, "unique_tail": unique}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 tests/test-cache-measure.py`
Expected: `PASS: cache measure`

- [ ] **Step 5: Commit**

```bash
cd /Users/tatlatat/.claude/codex-fleet
git add codex-native-gateway.py tests/test-cache-measure.py
git commit -m "feat(gateway): weighted-cache + miss-classification helpers (Component E)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QuE5K2DJUFeynEoAWiRhwg"
```

---

### Task 2: map-reduce-synthesis skill (Component D, part 1 — the skill)

**Files:**
- Create: `~/.claude/skills/map-reduce-synthesis/SKILL.md`

**Interfaces:**
- Produces: a reasonix `runAs: subagent` skill named `map-reduce-synthesis`, invoked by reasonix as `run_skill({name:"map-reduce-synthesis", arguments:"<task>"})`. Its job: split a large claim/findings block into token-bounded groups, `spawn_subagent` per group (Map), then `spawn_subagent` once to merge into the requested JSON schema (Reduce), and return the raw JSON object only.
- Consumes: reasonix built-in `spawn_subagent` tool (already exists in the engine).

- [ ] **Step 1: Create the skill file**

```bash
mkdir -p ~/.claude/skills/map-reduce-synthesis
```

Create `~/.claude/skills/map-reduce-synthesis/SKILL.md`:

```markdown
---
name: map-reduce-synthesis
description: Synthesize a large set of items (research claims, review findings, search results) into one final structured JSON object WITHOUT a single oversized model turn. Use when a task asks you to merge/rank/summarize many items into a nested JSON schema and the input is large. Splits items into small groups (Map), summarizes each in an isolated subagent, then merges the group summaries into the final JSON (Reduce).
runAs: subagent
model: claude-reasonix-flash
allowed-tools: spawn_subagent
---

You are the map-reduce synthesizer. The parent handed you a large synthesis task: a
block of items (claims/findings/sources) plus a target JSON schema and instructions.
Doing it in one turn overflows and produces broken JSON. Instead:

## MAP
1. Split the items into groups of at most ~8 items (or ~6 KB of text) each, preserving item boundaries.
2. For EACH group, call `spawn_subagent` with a focused prompt: "Summarize and group these N items toward the research/review question. Merge semantic duplicates within the group. Return a compact JSON array of partial findings, each {claim, confidence, sources, evidence}." Pass only that group's text. Collect each subagent's returned JSON.

## REDUCE
3. Call `spawn_subagent` ONCE with all the partial findings concatenated: "Merge these partial findings across groups: combine duplicates, group into coherent findings, assign overall confidence, write the executive summary, caveats, and open questions. Return EXACTLY ONE JSON object matching this schema: <paste the target schema from the parent task>. No prose, no fences."

## RETURN
4. Return the reduce subagent's JSON object verbatim as your entire answer — nothing else.

Rules: keep each subagent task small so it completes in one turn. Never paste the whole item block into a single subagent. If a subagent returns invalid JSON, retry that ONE group/merge once with "return only the JSON object". Your final output is the raw JSON only.
```

- [ ] **Step 2: Verify reasonix can see the skill**

Run:
```bash
ls -la ~/.claude/skills/map-reduce-synthesis/SKILL.md
grep -c "runAs: subagent" ~/.claude/skills/map-reduce-synthesis/SKILL.md
grep -c "^description:" ~/.claude/skills/map-reduce-synthesis/SKILL.md
```
Expected: file exists; `runAs: subagent` count = 1; `description:` count = 1 (reasonix rejects skills with no `description:`).

- [ ] **Step 3: Commit**

```bash
cd /Users/tatlatat/.claude/codex-fleet
git add -f ~/.claude/skills/map-reduce-synthesis/SKILL.md 2>/dev/null || true
# The skill lives outside the repo; record it in the repo's docs instead:
mkdir -p skills-mirror && cp ~/.claude/skills/map-reduce-synthesis/SKILL.md skills-mirror/map-reduce-synthesis.SKILL.md
git add skills-mirror/map-reduce-synthesis.SKILL.md
git commit -m "feat(skill): map-reduce-synthesis subagent skill (Component D part 1)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QuE5K2DJUFeynEoAWiRhwg"
```

---

### Task 3: Detect heavy-synthesis lane + inject skill directive (Component D, part 2 — the gateway hook)

**Files:**
- Modify: `codex-native-gateway.py` — add detector near `structured_output_prompt_instruction` (~L537); call it inside `openai_messages_to_prompt` where the structured instruction is appended.
- Test: `tests/test-synthesis-detect.py` (create)

**Interfaces:**
- Consumes: `tool_schema_entries`, `is_structured_output_tool_name` (existing).
- Produces: `is_heavy_synthesis(tools, prompt_len: int) -> bool` (True when a StructuredOutput schema contains a nested array-of-objects AND prompt_len exceeds the threshold); `mapreduce_directive() -> str` (the text appended to the prompt instructing reasonix to use the skill). Threshold env: `CLAUDE_CODEX_GATEWAY_MAPREDUCE_MIN_PROMPT` (default 20000). Kill-switch env: `CLAUDE_CODEX_GATEWAY_MAPREDUCE_SYNTHESIS` (default "1").

- [ ] **Step 1: Write the failing test**

```python
# tests/test-synthesis-detect.py
from __future__ import annotations
import importlib.util, os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gw", ROOT / "codex-native-gateway.py")
gw = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(gw)


def expect(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")


NESTED = [{"name": "StructuredOutput", "input_schema": {
    "type": "object", "required": ["findings"], "properties": {
        "findings": {"type": "array", "items": {"type": "object",
            "properties": {"claim": {"type": "string"}, "sources": {"type": "array"}}}}}}}]
FLAT = [{"name": "StructuredOutput", "input_schema": {
    "type": "object", "properties": {"refuted": {"type": "boolean"}}}}]


def test_heavy_when_nested_and_large():
    expect(gw.is_heavy_synthesis(NESTED, 40000) is True, "nested + large -> heavy")


def test_not_heavy_when_small():
    expect(gw.is_heavy_synthesis(NESTED, 5000) is False, "nested but small -> not heavy")


def test_not_heavy_when_flat():
    expect(gw.is_heavy_synthesis(FLAT, 40000) is False, "flat schema -> not heavy even if large")


def test_directive_mentions_skill():
    d = gw.mapreduce_directive()
    expect("map-reduce-synthesis" in d, "directive names the skill")
    expect("run_skill" in d, "directive tells reasonix to run_skill")


def test_killswitch_off(monkeypatch=None):
    os.environ["CLAUDE_CODEX_GATEWAY_MAPREDUCE_SYNTHESIS"] = "0"
    try:
        expect(gw.is_heavy_synthesis(NESTED, 40000) is False, "killswitch off -> never heavy")
    finally:
        os.environ.pop("CLAUDE_CODEX_GATEWAY_MAPREDUCE_SYNTHESIS", None)


if __name__ == "__main__":
    test_heavy_when_nested_and_large()
    test_not_heavy_when_small()
    test_not_heavy_when_flat()
    test_directive_mentions_skill()
    test_killswitch_off()
    print("PASS: synthesis detect")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 tests/test-synthesis-detect.py`
Expected: FAIL — `module 'gw' has no attribute 'is_heavy_synthesis'`

- [ ] **Step 3: Write minimal implementation**

Add to `codex-native-gateway.py` right after `def structured_output_prompt_instruction(...)` returns (after ~L549):

```python
def _schema_has_nested_array_of_objects(schema: Any) -> bool:
    """True if the JSON schema contains an array whose items are objects (a
    nested structure DeepSeek-flash struggles to emit in one shot)."""
    if not isinstance(schema, dict):
        return False
    props = schema.get("properties")
    if isinstance(props, dict):
        for v in props.values():
            if isinstance(v, dict) and v.get("type") == "array":
                items = v.get("items")
                if isinstance(items, dict) and items.get("type") == "object":
                    return True
            if _schema_has_nested_array_of_objects(v):
                return True
    items = schema.get("items")
    if isinstance(items, dict) and _schema_has_nested_array_of_objects(items):
        return True
    return False


def is_heavy_synthesis(tools: Any, prompt_len: int) -> bool:
    """A forced StructuredOutput whose schema is nested AND whose prompt is large
    is a 'heavy synthesis' lane that flash loops on — route it to the map-reduce
    skill. Disabled by CLAUDE_CODEX_GATEWAY_MAPREDUCE_SYNTHESIS=0."""
    if os.getenv("CLAUDE_CODEX_GATEWAY_MAPREDUCE_SYNTHESIS", "1").lower() not in {"1", "true", "yes", "on"}:
        return False
    min_len = env_int("CLAUDE_CODEX_GATEWAY_MAPREDUCE_MIN_PROMPT", default=20000)
    if prompt_len < min_len:
        return False
    for entry in tool_schema_entries(tools):
        if is_structured_output_tool_name(str(entry.get("name") or "")):
            if _schema_has_nested_array_of_objects(entry.get("schema")):
                return True
    return False


def mapreduce_directive() -> str:
    return (
        "\n\nLARGE-SYNTHESIS NOTE: this is a big merge/summarize task with a nested "
        "JSON schema. Do NOT attempt it in one turn — call "
        "run_skill({name: \"map-reduce-synthesis\", arguments: <the full task above>}) "
        "to split the items, summarize each group in an isolated subagent, then merge "
        "into the final JSON object. Return the skill's JSON result as your answer."
    )
```

Then wire it into `openai_messages_to_prompt` — find the block (~L600) where `structured_instruction` is appended LAST and add the directive after it:

```python
    parts: list[str] = [*lead_system]
    if generic_tools_block:
        parts.append(generic_tools_block)
    parts.extend(rest)
    if structured_instruction:
        parts.append(structured_instruction)
        # Heavy nested-schema synthesis on a large prompt: tell reasonix to use the
        # in-engine map-reduce skill instead of looping on a single oversized turn.
        assembled_len = sum(len(p) for p in parts)
        if is_heavy_synthesis(tools, assembled_len):
            parts.append(mapreduce_directive())
    return "\n\n".join(parts).strip() or "Complete the requested Codex worker task."
```

Note: `tool_schema_entries`, `is_structured_output_tool_name`, `env_int`, and `os` already exist in the module.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 tests/test-synthesis-detect.py`
Expected: `PASS: synthesis detect`

- [ ] **Step 5: Run the existing prompt test to confirm no regression**

Run: `python3 tests/test-prefix-normalize.py`
Expected: `PASS: prefix normalize`

- [ ] **Step 6: Commit**

```bash
cd /Users/tatlatat/.claude/codex-fleet
git add codex-native-gateway.py tests/test-synthesis-detect.py
git commit -m "feat(gateway): detect heavy synthesis lane, route to map-reduce skill (Component D part 2)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QuE5K2DJUFeynEoAWiRhwg"
```

---

### Task 4: Per-lane loop-breaker (Component C)

**Files:**
- Modify: `codex-native-gateway.py` — add a module-level retry counter + helper near the prime-gate block (~L80), and consult it at the forced-StructuredOutput fallback points in `call_openai_compatible` (~L437) and `call_openai_chat_completion` (~L1251).
- Test: `tests/test-loop-breaker.py` (create)

**Interfaces:**
- Produces: `register_lane_attempt(prompt: str) -> int` (returns how many times a lane with this prefix+tail signature has been seen recently, monotonic per signature within a time window); `should_force_fallback(prompt: str) -> bool` (True when count ≥ `CLAUDE_CODEX_GATEWAY_MAX_LANE_RETRIES`, default 3).
- Consumes: `prefix_prime_key` (existing) for the signature.

- [ ] **Step 1: Write the failing test**

```python
# tests/test-loop-breaker.py
from __future__ import annotations
import importlib.util, os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gw", ROOT / "codex-native-gateway.py")
gw = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(gw)


def expect(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")


def test_counts_increment_per_signature():
    os.environ["CLAUDE_CODEX_GATEWAY_MAX_LANE_RETRIES"] = "3"
    p = "SAME LANE SIGNATURE " * 600
    c1 = gw.register_lane_attempt(p)
    c2 = gw.register_lane_attempt(p)
    expect(c2 == c1 + 1, f"count increments: {c1} -> {c2}")
    other = gw.register_lane_attempt("DIFFERENT LANE " * 600)
    expect(other == 1, "a different signature starts at 1")


def test_force_fallback_after_threshold():
    os.environ["CLAUDE_CODEX_GATEWAY_MAX_LANE_RETRIES"] = "3"
    p = "LOOPY LANE " * 600
    seen_force = False
    for _ in range(5):
        gw.register_lane_attempt(p)
        if gw.should_force_fallback(p):
            seen_force = True
    expect(seen_force, "force-fallback triggers once count reaches threshold")


def test_disabled_when_zero():
    os.environ["CLAUDE_CODEX_GATEWAY_MAX_LANE_RETRIES"] = "0"
    p = "NEVER FORCE " * 600
    for _ in range(10):
        gw.register_lane_attempt(p)
    expect(gw.should_force_fallback(p) is False, "threshold 0 disables loop-breaker")
    os.environ["CLAUDE_CODEX_GATEWAY_MAX_LANE_RETRIES"] = "3"


if __name__ == "__main__":
    test_counts_increment_per_signature()
    test_force_fallback_after_threshold()
    test_disabled_when_zero()
    print("PASS: loop breaker")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 tests/test-loop-breaker.py`
Expected: FAIL — `module 'gw' has no attribute 'register_lane_attempt'`

- [ ] **Step 3: Write minimal implementation**

Add near the prime-gate globals (after `_PRIME_GATES` ~L86) in `codex-native-gateway.py`:

```python
# --- Per-lane loop breaker -------------------------------------------------
# A lane whose model never emits valid JSON gets re-driven turn-by-turn by Claude
# Code, each turn re-feeding history (input 27K->227K, measured). We count repeats
# of the same lane signature; past the threshold, the forced-StructuredOutput path
# returns a schema-valid fallback so the workflow completes instead of looping.
_LANE_LOCK = threading.Lock()
_LANE_COUNTS: dict[str, int] = {}


def register_lane_attempt(prompt: str) -> int:
    key = prefix_prime_key(prompt)
    with _LANE_LOCK:
        _LANE_COUNTS[key] = _LANE_COUNTS.get(key, 0) + 1
        return _LANE_COUNTS[key]


def should_force_fallback(prompt: str) -> bool:
    limit = env_int("CLAUDE_CODEX_GATEWAY_MAX_LANE_RETRIES", default=3)
    if limit <= 0:
        return False
    key = prefix_prime_key(prompt)
    with _LANE_LOCK:
        return _LANE_COUNTS.get(key, 0) >= limit
```

Then at the `/v1/messages` forced-fallback site in `call_openai_compatible` (the block at ~L437 that does `if tool_input is None: ... if _tool_choice_forces(...)`), change the condition so an over-retried lane ALSO forces the fallback even if `_tool_choice_forces` would already cover it, and log a warning. Locate:

```python
        if structured_tool:
            tool_input = parse_json_object_from_text(text)
            if tool_input is None:
                if _tool_choice_forces(payload, structured_tool):
                    tool_input = structured_timeout_fallback(
                        payload.get("tools"), structured_tool,
                        "model did not emit a JSON object; schema-valid fallback used",
                    )
```

Replace with (note `register_lane_attempt(prompt)` must be called once per request — add it right after `prompt = openai_messages_to_prompt(...)` near the top of the reasonix branch, capturing the count):

```python
        if structured_tool:
            tool_input = parse_json_object_from_text(text)
            if tool_input is None:
                forced = _tool_choice_forces(payload, structured_tool)
                looping = should_force_fallback(prompt)
                if forced or looping:
                    if looping:
                        gateway_trace("lane_loop_break", model=requested_model,
                                      retries=env_int("CLAUDE_CODEX_GATEWAY_MAX_LANE_RETRIES", default=3))
                    tool_input = structured_timeout_fallback(
                        payload.get("tools"), structured_tool,
                        "schema-valid fallback (model narrated or lane looped)",
                    )
```

And near the top of the `if config.get("provider") == "reasonix_cli":` branch in `call_openai_compatible`, right after `prompt = openai_messages_to_prompt(messages, payload.get("tools"))`, add:

```python
        register_lane_attempt(prompt)
```

Apply the identical `register_lane_attempt(prompt)` call + `forced or looping` change at the `/v1/chat/completions` site in `call_openai_chat_completion` (~L1251), where `prompt = openai_messages_to_prompt(normalized, ...)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 tests/test-loop-breaker.py`
Expected: `PASS: loop breaker`

- [ ] **Step 5: Confirm no regression on structured-output path**

Run: `python3 tests/test-synthesis-detect.py`
Expected: `PASS: synthesis detect`

- [ ] **Step 6: Commit**

```bash
cd /Users/tatlatat/.claude/codex-fleet
git add codex-native-gateway.py tests/test-loop-breaker.py
git commit -m "feat(gateway): per-lane loop breaker forces schema-valid result after N retries (Component C)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QuE5K2DJUFeynEoAWiRhwg"
```

---

### Task 5: Refine prime-gate (Component B)

**Files:**
- Modify: `codex-native-gateway.py` — `prefix_prime_key` (~L89) to hash 32 KB; the waiter wait site (~L1165) to add a grace sleep after the gate opens.
- Test: `tests/test-prime-gate.py` (create)

**Interfaces:**
- Consumes/Produces: `prefix_prime_key(prompt)` now keys on the first 32768 chars (env `CLAUDE_CODEX_GATEWAY_PRIME_HEAD_BYTES`, raise default to 32768); `acquire_prime_role` unchanged signature. New env `CLAUDE_CODEX_GATEWAY_PRIME_GRACE_SECONDS` (default 1.5) for the post-open settle.

- [ ] **Step 1: Write the failing test**

```python
# tests/test-prime-gate.py
from __future__ import annotations
import importlib.util, os, threading, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gw", ROOT / "codex-native-gateway.py")
gw = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(gw)


def expect(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")


def test_key_uses_32kb_window():
    # Two prompts identical for the first 32KB but differing at 33KB must share a key.
    base = "X" * 33000
    a = base + "AAAA"
    b = base + "BBBB"
    expect(gw.prefix_prime_key(a) == gw.prefix_prime_key(b), "share key within 32KB head")
    # Differing within the first 32KB must NOT share a key.
    c = "C" * 100 + "X" * 32900
    expect(gw.prefix_prime_key(a) != gw.prefix_prime_key(c), "differ inside 32KB -> different key")


def test_primer_then_waiter_roles():
    os.environ["CLAUDE_CODEX_GATEWAY_PRIME_GATE"] = "1"
    p = "PRIME ROLE " * 4000
    isp1, g1 = gw.acquire_prime_role(p)
    isp2, g2 = gw.acquire_prime_role(p)
    expect(isp1 is True and isp2 is False, "first=primer, second=waiter")
    expect(g1 is g2, "same prefix -> same gate")


def test_disabled_passthrough():
    os.environ["CLAUDE_CODEX_GATEWAY_PRIME_GATE"] = "0"
    isp, g = gw.acquire_prime_role("anything")
    expect(isp is False and g is None, "disabled -> passthrough")
    os.environ["CLAUDE_CODEX_GATEWAY_PRIME_GATE"] = "1"


if __name__ == "__main__":
    test_key_uses_32kb_window()
    test_primer_then_waiter_roles()
    test_disabled_passthrough()
    print("PASS: prime gate")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 tests/test-prime-gate.py`
Expected: FAIL on `test_key_uses_32kb_window` (current default is 8192, so the 32KB-head assertion fails).

- [ ] **Step 3: Write minimal implementation**

In `codex-native-gateway.py`, change `prefix_prime_key` default head from 8192 to 32768:

```python
def prefix_prime_key(prompt: str) -> str:
    import hashlib
    head = env_int("CLAUDE_CODEX_GATEWAY_PRIME_HEAD_BYTES", default=32768)
    return hashlib.sha1(prompt[:head].encode("utf-8", "ignore")).hexdigest()[:16]
```

At the waiter wait site (~L1165), after `prime_gate.wait(...)`, add a bounded grace settle so the just-persisted prefix is fully written before the waiter fires:

```python
    is_primer, prime_gate = acquire_prime_role(prompt)
    if prime_gate is not None and not is_primer:
        wait_s = env_float("CLAUDE_CODEX_GATEWAY_PRIME_WAIT_SECONDS", default=20.0)
        opened = prime_gate.wait(timeout=wait_s)
        if opened:
            grace = env_float("CLAUDE_CODEX_GATEWAY_PRIME_GRACE_SECONDS", default=1.5)
            if grace > 0:
                _time.sleep(min(grace, 5.0))
```

(`_time` and `env_float` already exist in the module.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 tests/test-prime-gate.py`
Expected: `PASS: prime gate`

- [ ] **Step 5: Commit**

```bash
cd /Users/tatlatat/.claude/codex-fleet
git add codex-native-gateway.py tests/test-prime-gate.py
git commit -m "feat(gateway): prime-gate keys on 32KB head + post-open grace settle (Component B)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QuE5K2DJUFeynEoAWiRhwg"
```

---

### Task 6: Prefix-stability assertion (Component A — verification only)

**Files:**
- Create: `tests/test-prefix-stability-check.py`

**Interfaces:**
- Consumes: `anthropic_system_to_text`, `openai_messages_to_prompt` (existing). Asserts that two lanes whose only difference is the agent role/identity produce the same prefix-32k hash (i.e. unify worked).

- [ ] **Step 1: Write the test (this is a regression guard, expected to PASS immediately)**

```python
# tests/test-prefix-stability-check.py
from __future__ import annotations
import importlib.util, hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gw", ROOT / "codex-native-gateway.py")
gw = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(gw)


def expect(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")


def pfx32(s):
    return hashlib.sha1(s[:32768].encode("utf-8", "ignore")).hexdigest()[:12]


def test_same_shared_system_one_family():
    # Same system text + same tools, only the trailing user task differs ->
    # the leading 32KB (system + tools block) must be byte-identical.
    sys_text = "SYS COMMON " * 500
    tools = [{"name": "StructuredOutput", "input_schema": {"type": "object", "properties": {"x": {"type": "string"}}}}]
    a = gw.openai_messages_to_prompt([{"role": "system", "content": sys_text}, {"role": "user", "content": "lane A"}], tools)
    b = gw.openai_messages_to_prompt([{"role": "system", "content": sys_text}, {"role": "user", "content": "lane B different"}], tools)
    expect(pfx32(a) == pfx32(b), "shared system+tools -> identical 32KB prefix family")


if __name__ == "__main__":
    test_same_shared_system_one_family()
    print("PASS: prefix stability check")
```

- [ ] **Step 2: Run it**

Run: `python3 tests/test-prefix-stability-check.py`
Expected: `PASS: prefix stability check`. If it FAILS, the schema-instruction is being hoisted to the front again (regression) — re-check Task 3's ordering (structured instruction must stay LAST, only the directive appended after it).

- [ ] **Step 3: Commit**

```bash
cd /Users/tatlatat/.claude/codex-fleet
git add tests/test-prefix-stability-check.py
git commit -m "test(gateway): assert shared system+tools yields one prefix family (Component A)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QuE5K2DJUFeynEoAWiRhwg"
```

---

### Task 7: End-to-end live verification (all components together)

**Files:** none (operational verification + a results note).

**Interfaces:** Consumes the whole gateway. Produces a measured before/after in `docs/superpowers/plans/2026-06-23-results.md`.

- [ ] **Step 1: Run all unit tests**

Run:
```bash
cd /Users/tatlatat/.claude/codex-fleet
for t in tests/test-cache-measure.py tests/test-synthesis-detect.py tests/test-loop-breaker.py tests/test-prime-gate.py tests/test-prefix-stability-check.py tests/test-prefix-normalize.py tests/test-workflow-prefix-guide.py; do echo "== $t =="; python3 "$t" || break; done
```
Expected: every line ends `PASS: ...`.

- [ ] **Step 2: Smoke-test a real UltraCode fan-out (does not hang, lanes are reasonix)**

In a fresh tmux session running `claude-reasonix` in a repo, with `export CLAUDE_CODEX_GATEWAY_PREFIX_TRACE=1`, run a small `ultracode review <a small file>` task. While it runs, confirm from `runtime/reasonix-cost.jsonl` that lanes have `model: deepseek-v4-flash` and the run completes (no lane stuck >5 min).

- [ ] **Step 3: A/B the deep-research synthesis (the key result)**

Run `/deep-research <a question>` twice — once with `CLAUDE_CODEX_GATEWAY_MAPREDUCE_SYNTHESIS=0` (baseline) and once `=1` (treatment), fresh gateway each. For each, after completion, compute from `runtime/reasonix-cost.jsonl`:
```python
import json,time
rows=[json.loads(l) for l in open("runtime/reasonix-cost.jsonl") if l.strip()]
# (filter to the run window by ts), then:
# weighted via gw.weighted_cache(rows); classify via gw.classify_miss(rows)
```
Record: did synthesis COMPLETE (real report, not the salvage fallback)? total input tokens, weighted cache, and `classify_miss` buckets.

Expected (treatment vs baseline): synthesis completes for real; `loop_inflation` miss bucket drops sharply (the 62%-input loop is gone); total tokens lower; weighted cache higher.

- [ ] **Step 4: Write the results note + commit**

Create `docs/superpowers/plans/2026-06-23-results.md` with the measured baseline-vs-treatment numbers (weighted %, total tokens, miss buckets, synthesis-completed yes/no). Be honest — if weighted on research stays below 99.2% because of irreducible unique web content, state it and quote the `unique_tail` bucket.

```bash
cd /Users/tatlatat/.claude/codex-fleet
git add docs/superpowers/plans/2026-06-23-results.md
git commit -m "docs: measured before/after for reasonix multi-agent cache + map-reduce synthesis

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01QuE5K2DJUFeynEoAWiRhwg"
```

---

## Self-Review

**Spec coverage:**
- E (measurement) → Task 1 + used in Task 7. ✓
- D (map-reduce synthesis) → Task 2 (skill) + Task 3 (detect/inject). ✓
- C (loop breaker) → Task 4. ✓
- B (prime gate) → Task 5. ✓
- A (prefix-stable check) → Task 6. ✓
- Testing/verify → Task 7. ✓
- Every component has an env kill-switch: MAPREDUCE_SYNTHESIS, MAX_LANE_RETRIES, PRIME_GATE — ✓.

**Placeholder scan:** no TBD/TODO; all thresholds concrete (32768, 20000, 3, 1.5, 150000); all code blocks complete. ✓

**Type consistency:** `prefix_prime_key(prompt)` reused by Task 4 (loop) and Task 5 (gate) with same signature. `weighted_cache`/`classify_miss` row shape (`input_tokens`, `cache_pct`) matches the ledger and Task 7 usage. `is_heavy_synthesis(tools, prompt_len)` and `mapreduce_directive()` names match between Task 3 def and call site. `structured_timeout_fallback(tools, name, reason)` signature matches its existing definition. ✓

**Known follow-up the executor must watch:** Task 3 appends the directive AFTER the structured instruction (must stay last); Task 6 guards that ordering. If Task 6 fails, fix Task 3 before proceeding.
