#!/usr/bin/env python3
"""Lever-matrix harness — run ONE fixed workload through an on/off matrix of
token-reduction LEVERS so every later lever is MEASURED, not guessed.

For each matrix config we spawn a FRESH gateway with that config's lever flags as
env, run the SAME fixed WORKLOAD_SPEC (read + edit + review + workflow lanes),
read the cost ledger window (token-weighted + median cache, input + output
tokens), estimate cost in owner-relative price units, grade quality with the
realworld gates, and print one row. The baseline row (no flags) is the reference
every lever is compared against.

This file REUSES realworld-bench internals — it does NOT reimplement the gateway
lifecycle, the ledger reader, the per-lane HTTP client, or the quality grader:
  start_gateway / lane / ledger_window / grade  <- realworld-bench.py

Run:
  python3 runtime/lever-matrix-bench.py                 # full matrix
  python3 runtime/lever-matrix-bench.py --only baseline  # just the baseline row
  python3 runtime/lever-matrix-bench.py --json           # machine-readable
"""
from __future__ import annotations
import argparse
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import concurrent.futures as cf
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# --- Reuse realworld-bench internals (hyphenated filename → load by path) -------
_spec = importlib.util.spec_from_file_location(
    "realworld_bench", ROOT / "runtime" / "realworld-bench.py")
rwb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rwb)
lane = rwb.lane
ledger_window = rwb.ledger_window
grade = rwb.grade
start_gateway = rwb.start_gateway  # reused via _start_gateway_with_flags below

# Lever E predictor (hooks/reasonix-workflow.py is hyphenated → load by path). The
# bench measures PREDICTION PRECISION on the workflow-shaped lane: predict the files
# the lane references, compare to what it actually referenced. Advisory makes no
# prompt change, so this is pure measurement on top of an unchanged run.
_hook_spec = importlib.util.spec_from_file_location(
    "reasonix_workflow_hook", ROOT / "hooks" / "reasonix-workflow.py")
_hook = importlib.util.module_from_spec(_hook_spec)
_hook_spec.loader.exec_module(_hook)
predict_prefetch_files = _hook.predict_prefetch_files


# --- Cost model (owner-relative price units) -----------------------------------
# Owner split: a cache HIT is the cheap unit (1); an input MISS costs ~51x a hit;
# an OUTPUT token costs ~101x a hit (output dominates because it is never cached
# and is billed at the full uncached rate). These are RELATIVE units, not USD —
# they let us rank levers by where the cost actually goes.
P_HIT = 1
P_MISS = 51
P_OUT = 101


def est_cost(input_tok: int, cache_pct: float, output_tok: int) -> float:
    """Estimate one lane's cost in owner-relative units.

    hit  = input_tok * cache_fraction        (cheap, P_HIT)
    miss = input_tok * (1 - cache_fraction)  (expensive, P_MISS)
    out  = output_tok                        (most expensive, P_OUT)
    """
    cache = max(0.0, min(100.0, cache_pct)) / 100.0
    hit = input_tok * cache
    miss = input_tok * (1.0 - cache)
    return hit * P_HIT + miss * P_MISS + output_tok * P_OUT


# --- Fixed workload ------------------------------------------------------------
# ONE fixed workload, run identically under every config so differences are the
# lever, not the prompt. Shapes mirror real fleet traffic:
#   READ     (8) : StructuredOutput {summary, file} over one real file each.
#   EDIT     (2) : write/modify a scratch file under runtime/ (deterministic).
#   REVIEW   (6) : ONE byte-identical 12K shared block + a 1-word per-lane suffix
#                  (the shared-prefix shape whose cache ceiling is highest).
#   WORKFLOW (1) : a Dynamic-Workflow-shaped decompose lane (StructuredOutput,
#                  carries the workflow prefix-guide preamble like a real lane).
N_READ = 8
N_EDIT = 2
N_REVIEW = 6
N_WORKFLOW = 1

_SCRATCH = ROOT / "runtime" / "lmb-scratch"

# Files the READ lanes summarize (cycled to reach N_READ).
_READ_FILES = [
    "reasonix-native-gateway.py",
    "runtime/realworld-bench.py",
    "system-prompt-reasonix.md",
    "README.md",
    "hooks/reasonix-workflow.py",
    "hooks/only-reasonix-fleet.py",
]

_READ_SCHEMA = {
    "type": "object",
    "required": ["summary", "file"],
    "properties": {
        "summary": {"type": "string"},
        "file": {"type": "string"},
    },
}
_SYN_SCHEMA = {
    "type": "object",
    "required": ["summary"],
    "properties": {"summary": {"type": "string"}},
}

# A real shared review block: ONE byte-identical large file FIRST, tiny per-lane
# dimension word LAST — the only shape where a high cache ceiling is reachable.
_SHARED = (ROOT / "reasonix-native-gateway.py").read_text(errors="ignore")[:12000]
_SHARED_BLOCK = (
    "You are reviewing this file for one concern. SHARED FILE (identical "
    "for every lane):\n" + _SHARED + "\n\nReply with one terse sentence.\n")
_REVIEW_DIMS = ["races", "errors", "naming", "deadcode", "edges", "validation",
                "cleanup", "concurrency"]

# Workflow prefix-guide preamble (mirrors the additionalContext a real
# Dynamic-Workflow lane carries, so the workflow lane is shaped like production).
_WORKFLOW_PREAMBLE = (
    "PROMPT-CACHE NOTE for this Dynamic Workflow: each agent() lane shares this "
    "immutable preamble FIRST so the prefix caches across lanes; put the unique "
    "per-lane instruction LAST.\n\n")


def _read_spec(i: int) -> dict:
    f = _READ_FILES[i % len(_READ_FILES)]
    return {
        "type": "read",
        "prompt": (f"Read ONLY {ROOT}/{f} and return a StructuredOutput object "
                   f"with a one-sentence summary of its purpose and the file path."),
        "schema": _READ_SCHEMA,
    }


def _edit_spec(i: int) -> dict:
    target = _SCRATCH / f"edit-lane-{i}.txt"
    return {
        "type": "edit",
        "_target": str(target),
        "prompt": (
            "You are an edit lane. A scratch file exists. Return a StructuredOutput "
            "object describing a one-line modification to make to it: set `file` to "
            f"the path {target} and `summary` to the single line to append "
            f"(exactly: 'edited-by-lane-{i}'). Do not include anything else."),
        "schema": _READ_SCHEMA,
    }


def _review_spec(i: int) -> dict:
    return {
        "type": "review",
        "prompt": _SHARED_BLOCK + f"\nLANE concern: {_REVIEW_DIMS[i % len(_REVIEW_DIMS)]}.",
        "schema": None,
    }


def _workflow_spec(i: int) -> dict:
    return {
        "type": "workflow",
        "prompt": (
            _WORKFLOW_PREAMBLE
            + "Decompose this goal into a StructuredOutput object: set `summary` to "
              "a one-sentence plan to measure token cost per lane type, and `file` "
              "to 'runtime/reasonix-cost.jsonl'."),
        "schema": _READ_SCHEMA,
    }


def build_workload() -> list[dict]:
    """The fixed WORKLOAD_SPEC: 8 read + 2 edit + 6 review + 1 workflow lanes."""
    specs: list[dict] = []
    specs += [_read_spec(i) for i in range(N_READ)]
    specs += [_edit_spec(i) for i in range(N_EDIT)]
    specs += [_review_spec(i) for i in range(N_REVIEW)]
    specs += [_workflow_spec(i) for i in range(N_WORKFLOW)]
    return specs


# WORKLOAD_SPEC: the canonical fixed workload (read+edit+review+workflow).
WORKLOAD_SPEC = build_workload()


# --- Lever matrix --------------------------------------------------------------
# Every lever defaults OFF in the baseline (flags={}). build_matrix returns:
#   [ baseline (no flags),
#     one row per lever (that lever's flag ON),
#     best_combo (the union of all default-ON levers) ].
# Task 1 ships the harness; later tasks add the real lever flag→behavior wiring.
# DEFAULT_ON lists the levers that the best_combo turns on together.
#
# REAL-DEEPSEEK VERDICT (16 F runs + 8 baseline, 2026-06-25): only READ_SUMMARY (A)
# is promoted. OUTPUT_DISCIPLINE (F) was DROPPED after measuring it end-to-end:
#   - F's read-lane saving is real but small (read lanes are already terse).
#   - On FREE-FORM edit lanes (the real workload, NOT the bench's forced-
#     StructuredOutput edit lane) F's "emit a real diff" directive INCREASES edit
#     output +43% avg vs baseline (3807→6410, 4712→9657, ...) — it makes the model
#     emit a usable diff instead of a one-line description, which is correct
#     behavior but costs MORE output, the opposite of the lever's goal here.
#   - F also nudged the heavy review lane's bad-lane rate up (3 bad lanes / 16 F runs
#     vs 0 / 8 baseline) — its extra prompt block pushes the already-tail-latency
#     review lane over the empty/slow edge ~1% of lanes.
# Net: F is NOT measured-positive on the real fan-out, so per measure-then-promote
# it stays OFF. Re-promote only after measuring F on a FREE-FORM edit lane (re-tune
# the edit cap to that lane's real P95 and confirm net output drops). See
# docs/.../token-reduction-results.md "Real-DeepSeek correction".
DEFAULT_ON_LEVERS = [
    "READ_SUMMARY",
]

# Short lever name → full gateway env var name.  The gateway reads the full name;
# the bench's --levers arg uses the short name for readability.  Levers absent
# from this map use the short name directly as the env var (legacy/future levers).
_LEVER_ENV_MAP: dict[str, str] = {
    "OUTPUT_DISCIPLINE": "CLAUDE_REASONIX_GATEWAY_OUTPUT_DISCIPLINE",
    "READ_SUMMARY": "CLAUDE_REASONIX_GATEWAY_READ_SUMMARY",
    # Lever C — gateway shared read-summary cache. Lives in the long-lived gateway:
    # a file summarized by one lane is reused by later lanes referencing it. NOT in
    # DEFAULT_ON until Scenario C2 measures it positive (run-2 cache >= run-1 + 5pts).
    "READ_SUMMARY_CACHE": "CLAUDE_REASONIX_GATEWAY_READ_SUMMARY_CACHE",
    # Lever B — sub-agent read-in-isolation. This flag is read by the VENDORED
    # ENGINE (buildCodeToolset in run-lane.mjs), not the gateway proper: the
    # gateway copies its full env into the lane shim (shim_env = dict(os.environ)),
    # so injecting REASONIX_READ_ISOLATED here reaches buildCodeToolset and
    # registers read_file_isolated. NOT in DEFAULT_ON until adoption is proven
    # (the model must actually CALL the tool).
    "READ_ISOLATED": "REASONIX_READ_ISOLATED",
    # Lever E — speculative context PREFETCH (Q7 advisory mode first). The env var
    # is tri-state (off|advisory|inject), NOT a 1/0 flag — so it carries its VALUE
    # ('advisory') via _LEVER_VALUES below. Advisory makes NO prompt change: it only
    # predicts which files the lanes will read and logs precision, so cache/tokens
    # must be UNCHANGED vs baseline. The only output is the prediction-precision number.
    "PREFETCH_CONTEXT": "CLAUDE_REASONIX_PREFETCH_CONTEXT",
    # Lever D — pre-index. Build a semantic index ONCE (gateway is the sole build
    # trigger; per-lane is read-only indexCompatible()), exposed via the EXISTING
    # semantic_search query tool — NO prefix injection, so it sidesteps
    # byte-stability. Read by the GATEWAY (build_preindex). FAIL-OPEN when no
    # embedding model is reachable. UNMEASURED this round (owner pulled NO embed
    # model — Ollama runs with 0 models), so D stays OFF and is deliberately NOT
    # in DEFAULT_ON until a model exists and Scenario D measures the read-lane
    # input drop with zero output effect.
    "PREINDEX": "CLAUDE_REASONIX_PREINDEX",
}

# Levers whose env var is NOT a 1/0 flag but takes a string VALUE.
_LEVER_VALUES: dict[str, str] = {
    "PREFETCH_CONTEXT": "advisory",
}


def _lever_flags(lever_name: str) -> dict[str, str]:
    """Return the {env_var: value} dict for a single lever.

    Most levers are on/off (value '1'). A few are tri-state and carry a string
    VALUE from _LEVER_VALUES (e.g. PREFETCH_CONTEXT -> 'advisory')."""
    env = _LEVER_ENV_MAP.get(lever_name, lever_name)
    return {env: _LEVER_VALUES.get(lever_name, "1")}


def build_matrix(levers: list[str]) -> list[dict]:
    """Return matrix configs: baseline first, then one per lever, then best_combo.

    Each config: {"name": str, "flags": {ENV_NAME: "1", ...}}.
    The baseline MUST be first and MUST have empty flags (the reference row).
    """
    configs: list[dict] = [{"name": "baseline", "flags": {}}]
    for lv in levers:
        configs.append({"name": lv, "flags": _lever_flags(lv)})
    best: dict[str, str] = {}
    for lv in levers:
        if lv in DEFAULT_ON_LEVERS:
            best.update(_lever_flags(lv))
    configs.append({"name": "best_combo", "flags": best})
    return configs


# --- Gateway-per-config (reuses realworld start_gateway, adds lever env) --------
def _start_gateway_with_flags(flags: dict) -> tuple[subprocess.Popen, int]:
    """Spawn a fresh gateway carrying `flags` as env, reusing realworld's launcher.

    We temporarily inject the lever flags into os.environ, call the reused
    start_gateway (which snapshots os.environ for the child), then restore — so
    the child gets the flags and our process env is left clean for the next row.
    """
    saved = {k: os.environ.get(k) for k in flags}
    try:
        for k, v in flags.items():
            os.environ[k] = str(v)
        return start_gateway()
    finally:
        for k, old in saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


def _ensure_scratch() -> None:
    _SCRATCH.mkdir(parents=True, exist_ok=True)
    for i in range(N_EDIT):
        (_SCRATCH / f"edit-lane-{i}.txt").write_text(f"scratch file for edit lane {i}\n")


def _cleanup_scratch() -> None:
    if _SCRATCH.exists():
        shutil.rmtree(_SCRATCH, ignore_errors=True)


def _run_workload(port: int) -> dict:
    """Run the fixed workload against `port`. A shared warm-up lane runs FIRST
    (fixed order) to seed the review shared-prefix, then lanes fan out by type."""
    _ensure_scratch()
    out: dict = {}

    # Shared warm-up lane (fixed order, before the burst) — seeds the shared
    # review prefix so the burst measures steady-state cache, not a cold miss.
    for _ in range(3):
        wu = lane(port, _SHARED_BLOCK + "\nLANE concern: warm-up.")
        if not wu.get("errored") and not wu.get("empty"):
            break
    time.sleep(2)

    t0 = time.time()
    groups: dict[str, list[dict]] = {"read": [], "edit": [], "review": [], "workflow": []}
    for s in WORKLOAD_SPEC:
        groups[s["type"]].append(s)

    def _dispatch(spec: dict) -> dict:
        r = lane(port, spec["prompt"], schema=spec.get("schema"))
        r["_lane_type"] = spec["type"]
        return r

    # Per-group windows so the reused grade() can score the RIGHT cache ceiling per
    # workload: review (shared-prefix, high ceiling) vs the unique-content fan-out
    # (read+edit+workflow, the ~92-98% ceiling). Run review LAST and tightly windowed.
    windows: dict[str, tuple[float, float]] = {}
    order = ["read", "edit", "workflow", "review"]
    for gtype in order:
        specs = groups[gtype]
        if not specs:
            continue
        gw0 = time.time() - 0.1
        with cf.ThreadPoolExecutor(max_workers=len(specs)) as ex:
            out[gtype] = list(ex.map(_dispatch, specs))
        time.sleep(0.5)
        windows[gtype] = (gw0, time.time() + 0.5)

    time.sleep(1.5)
    out["_ledger"] = ledger_window(t0 - 0.1, time.time() + 1)
    out["_window"] = (t0 - 0.1, time.time() + 1)
    # Feed grade() its expected keys: review window = shared-prefix gate; the
    # unique-content fan-out (read+edit+workflow) = the realistic ceiling gate.
    if "review" in windows:
        out["_ledger_review"] = ledger_window(*windows["review"])
    uniq = [windows[g] for g in ("read", "edit", "workflow") if g in windows]
    if uniq:
        out["_ledger_unique"] = ledger_window(min(w[0] for w in uniq),
                                              max(w[1] for w in uniq))
    _cleanup_scratch()
    return out


def run_c2_twice(json_out: bool = False) -> dict:
    """Scenario C2 — run the SAME fan-out workload TWICE against ONE long-lived gateway
    with Lever C ON. Run-1 populates the gateway's shared read-cache; run-2 reuses it
    (miss->hit), so run-2's cache should be >= run-1 + 5pts. The gateway stays up across
    both runs (the cache is a gateway module-level dict — an ephemeral per-run gateway
    would share nothing). Returns {run1, run2, delta, pass}."""
    flags = {"CLAUDE_REASONIX_GATEWAY_READ_SUMMARY_CACHE": "1"}
    # Fresh persisted cache so run-1 is a true cold start.
    cache_file = ROOT / "runtime" / "read-summary-cache.json"
    try:
        cache_file.unlink()
    except FileNotFoundError:
        pass
    proc, port = _start_gateway_with_flags(flags)
    runs: list[dict] = []
    try:
        for _ in range(2):
            out = _run_workload(port)
            led = out.get("_ledger", {}) or {}
            runs.append({
                "lanes": led.get("lanes", 0),
                "cache_weighted": led.get("weighted"),
                "cache_median": led.get("median"),
            })
            time.sleep(1.0)
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    c1 = runs[0].get("cache_weighted") or 0.0
    c2 = runs[1].get("cache_weighted") or 0.0
    delta = round(c2 - c1, 1)
    result = {
        "scenario": "C2",
        "run1_cache": c1,
        "run2_cache": c2,
        "delta": delta,
        "pass": delta >= 5.0,
        "runs": runs,
    }
    if json_out:
        print(json.dumps(result, indent=2))
    else:
        print("\n=== Scenario C2 (twice-run, Lever C ON) ===")
        print(f"  run-1 cache_weighted: {c1}%")
        print(f"  run-2 cache_weighted: {c2}%")
        print(f"  delta:                {delta:+} pts   "
              f"({'PASS' if result['pass'] else 'FAIL'}: need run-2 >= run-1 + 5)")
    return result


def _files_referenced_by_lane(result: dict) -> set:
    """The files a workflow-shaped lane actually referenced — its ground truth for
    precision. The bench's workflow lane returns StructuredOutput {summary, file};
    the `file` field is the file it points at. Resolved to absolute under ROOT."""
    refs: set = set()
    ti = result.get("tool_input")
    if isinstance(ti, dict):
        for key in ("file", "files", "files_read"):
            v = ti.get(key)
            if isinstance(v, str):
                v = [v]
            if isinstance(v, list):
                for f in v:
                    if not isinstance(f, str) or not f.strip():
                        continue
                    p = Path(f)
                    if not p.is_absolute():
                        p = ROOT / f
                    try:
                        refs.add(str(p.expanduser().resolve()))
                    except Exception:
                        pass
    return refs


def _prefetch_precision(out: dict) -> dict:
    """Measure Lever E prediction PRECISION on the workflow-shaped lane.

    precision = |predicted ∩ actually-referenced| / |predicted|.
    Returns {predicted, referenced, hit, precision} or None if there is no
    workflow lane / nothing was predicted (precision undefined on an empty set)."""
    wf = (out.get("workflow") or [])
    if not wf:
        return None
    # The single workflow lane's prompt is the prediction source (same text the
    # advisory hook would see). Recover it from the fixed spec.
    wf_spec = next((s for s in WORKLOAD_SPEC if s["type"] == "workflow"), None)
    if wf_spec is None:
        return None
    predicted = set(predict_prefetch_files(wf_spec["prompt"], str(ROOT)))
    referenced: set = set()
    for r in wf:
        referenced |= _files_referenced_by_lane(r)
    if not predicted:
        return {"predicted": [], "referenced": sorted(referenced),
                "hit": [], "precision": None,
                "note": "no files predicted (precision undefined)"}
    hit = predicted & referenced
    return {
        "predicted": sorted(predicted),
        "referenced": sorted(referenced),
        "hit": sorted(hit),
        "precision": round(len(hit) / len(predicted), 3),
    }


def run_matrix(configs: list[dict], json_out: bool = False) -> list[dict]:
    """Run each config through a fresh gateway, measure, grade, return + print rows."""
    rows: list[dict] = []
    for cfg in configs:
        proc, port = _start_gateway_with_flags(cfg["flags"])
        try:
            out = _run_workload(port)
        finally:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()

        led = out.get("_ledger", {}) or {}
        # output tokens over the window (ledger_window doesn't carry it; sum it).
        out_tok = _sum_output_tokens(out.get("_window"))
        in_tok = _sum_input_tokens(out.get("_window"))
        cache_w = led.get("weighted")
        cache_m = led.get("median")
        cost = est_cost(in_tok, cache_w if cache_w is not None else 0.0, out_tok)
        quality = grade(out)
        # Per-lane-type output: TOTAL out_tok is dominated by the 2 EDIT lanes whose
        # output is high-variance (10-3000+ tok, model-dependent) — it is NOT a reliable
        # lever signal on a single run. The levers (F/A) cap READ-lane output, so the
        # READ-lane output sum is the honest, low-variance signal of whether a cap fired.
        by_type = _output_by_type(out.get("_window"))
        row = {
            "config": cfg["name"],
            "flags": cfg["flags"],
            "lanes": led.get("lanes", 0),
            "cache_weighted": cache_w,
            "cache_median": cache_m,
            "input_tok": in_tok,
            "output_tok": out_tok,
            "read_out": by_type.get("read", 0),
            "edit_out": by_type.get("edit", 0),
            "est_cost": round(cost, 1),
            "quality_pass": quality.get("passed"),
        }
        # Lever E — when PREFETCH_CONTEXT is the config under test (advisory), measure
        # prediction PRECISION on the workflow-shaped lane. Advisory changes no prompt
        # byte, so cache/tokens must match baseline — the only new output is precision.
        if cfg["name"] == "PREFETCH_CONTEXT" or "CLAUDE_REASONIX_PREFETCH_CONTEXT" in (cfg["flags"] or {}):
            row["prefetch_precision"] = _prefetch_precision(out)
        rows.append(row)
        if not json_out:
            _print_row(row)
            if row.get("prefetch_precision") is not None:
                _print_prefetch(row["prefetch_precision"])
    return rows


def _print_prefetch(pp: dict) -> None:
    if pp is None:
        return
    prec = pp.get("precision")
    print("\n  --- Lever E (PREFETCH_CONTEXT, advisory) prediction precision ---")
    print(f"    predicted  : {pp.get('predicted')}")
    print(f"    referenced : {pp.get('referenced')}")
    print(f"    hit        : {pp.get('hit')}")
    if prec is None:
        print(f"    precision  : n/a  ({pp.get('note', 'undefined')})")
    else:
        print(f"    precision  : {prec}  (predicted ∩ referenced / predicted)")


def _ledger_rows(window) -> list[dict]:
    led = ROOT / "runtime" / "reasonix-cost.jsonl"
    if window is None or not led.exists():
        return []
    t0, t1 = window
    rows = []
    for l in led.read_text(errors="ignore").splitlines():
        try:
            r = json.loads(l)
        except Exception:
            continue
        if t0 <= r.get("ts", 0) <= t1 and r.get("input_tokens", 0) >= 500:
            rows.append(r)
    return rows


def _sum_output_tokens(window) -> int:
    return sum(int(r.get("output_tokens") or 0) for r in _ledger_rows(window))


def _sum_input_tokens(window) -> int:
    return sum(int(r.get("input_tokens") or 0) for r in _ledger_rows(window))


def _output_by_type(window) -> dict:
    """Output-token sum split by lane_type. The READ-lane sum is the low-variance
    signal a per-type cap (F/A) actually moves; the EDIT-lane sum is high-variance
    noise that dominates the total and must not be read as a lever effect."""
    out: dict = {}
    for r in _ledger_rows(window):
        lt = r.get("lane_type") or "unknown"
        out[lt] = out.get(lt, 0) + int(r.get("output_tokens") or 0)
    return out


def _input_by_type(window) -> dict:
    """INPUT-token sum split by lane_type — the B/C signal. Levers B (read-isolation)
    and C (shared read-cache) cut the INPUT a read lane ingests, not its output, so
    the read-lane INPUT sum is their headline. Added per the lever-validation adversary:
    ledger_window only returns cache aggregates, _output_by_type only sums output —
    there was no per-type INPUT helper, so any 'B/C cut input' claim was unmeasurable."""
    out: dict = {}
    for r in _ledger_rows(window):
        lt = r.get("lane_type") or "unknown"
        out[lt] = out.get(lt, 0) + int(r.get("input_tokens") or 0)
    return out


def _input_rows_by_type(window, lane_type: str) -> list[int]:
    """Per-lane INPUT tokens for one lane_type, as a list (so the diag can report the
    DISTRIBUTION + median, not just a sum — the adversary required counterbalanced,
    per-lane comparison for C/B so stochastic read/no-read variance is separable)."""
    return [int(r.get("input_tokens") or 0)
            for r in _ledger_rows(window)
            if (r.get("lane_type") or "unknown") == lane_type]


# --- Printing ------------------------------------------------------------------
_HEADER = ("config", "lanes", "cache_w%", "cache_med%", "in_tok", "out_tok",
           "read_out", "edit_out", "est_cost", "quality")


def _print_header() -> None:
    print("\n=== LEVER MATRIX (real reasonix+DeepSeek) ===")
    print("  NOTE: out_tok total is dominated by high-variance EDIT lanes; read_out")
    print("  (READ-lane output, where F/A caps fire) is the reliable lever signal.")
    print("  {:<14} {:>5} {:>9} {:>10} {:>9} {:>8} {:>8} {:>8} {:>9} {:>7}".format(*_HEADER))


def _print_row(row: dict) -> None:
    if not getattr(_print_row, "_did_header", False):
        _print_header()
        _print_row._did_header = True
    print("  {:<14} {:>5} {:>9} {:>10} {:>9} {:>8} {:>8} {:>8} {:>9} {:>7}".format(
        row["config"], row["lanes"],
        f"{row['cache_weighted']}" if row["cache_weighted"] is not None else "-",
        f"{row['cache_median']}" if row["cache_median"] is not None else "-",
        row["input_tok"], row["output_tok"],
        row.get("read_out", 0), row.get("edit_out", 0), row["est_cost"],
        "PASS" if row["quality_pass"] else "FAIL"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None,
                    help="comma-separated config names to run (e.g. 'baseline,READ_SUMMARY')")
    ap.add_argument("--levers", default="OUTPUT_DISCIPLINE,READ_SUMMARY",
                    help="comma-separated lever flag names to include in the matrix")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--c2", action="store_true",
                    help="Scenario C2: run the fan-out workload TWICE under Lever C ON "
                         "(one long-lived gateway) and assert run-2 cache >= run-1 + 5pts")
    args = ap.parse_args()

    if args.c2:
        result = run_c2_twice(json_out=args.json)
        return 0 if result.get("pass") else 1

    levers = [x for x in (args.levers.split(",") if args.levers else []) if x.strip()]
    configs = build_matrix(levers)
    if args.only:
        only_names = {x.strip() for x in args.only.split(",") if x.strip()}
        configs = [c for c in configs if c["name"] in only_names]
        if not configs:
            print(f"no config named {args.only!r}", file=sys.stderr)
            return 2

    rows = run_matrix(configs, json_out=args.json)
    if args.json:
        print(json.dumps({"rows": rows}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
