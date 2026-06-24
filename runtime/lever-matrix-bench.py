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
DEFAULT_ON_LEVERS = [
    "OUTPUT_DISCIPLINE",
]


def build_matrix(levers: list[str]) -> list[dict]:
    """Return matrix configs: baseline first, then one per lever, then best_combo.

    Each config: {"name": str, "flags": {ENV_NAME: "1", ...}}.
    The baseline MUST be first and MUST have empty flags (the reference row).
    """
    configs: list[dict] = [{"name": "baseline", "flags": {}}]
    for lv in levers:
        configs.append({"name": lv, "flags": {lv: "1"}})
    best = {lv: "1" for lv in levers if lv in DEFAULT_ON_LEVERS}
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
        row = {
            "config": cfg["name"],
            "flags": cfg["flags"],
            "lanes": led.get("lanes", 0),
            "cache_weighted": cache_w,
            "cache_median": cache_m,
            "input_tok": in_tok,
            "output_tok": out_tok,
            "est_cost": round(cost, 1),
            "quality_pass": quality.get("passed"),
        }
        rows.append(row)
        if not json_out:
            _print_row(row)
    return rows


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


# --- Printing ------------------------------------------------------------------
_HEADER = ("config", "lanes", "cache_w%", "cache_med%", "in_tok", "out_tok",
           "est_cost", "quality")


def _print_header() -> None:
    print("\n=== LEVER MATRIX (real reasonix+DeepSeek) ===")
    print("  {:<14} {:>5} {:>9} {:>10} {:>9} {:>8} {:>9} {:>7}".format(*_HEADER))


def _print_row(row: dict) -> None:
    if not getattr(_print_row, "_did_header", False):
        _print_header()
        _print_row._did_header = True
    print("  {:<14} {:>5} {:>9} {:>10} {:>9} {:>8} {:>9} {:>7}".format(
        row["config"], row["lanes"],
        f"{row['cache_weighted']}" if row["cache_weighted"] is not None else "-",
        f"{row['cache_median']}" if row["cache_median"] is not None else "-",
        row["input_tok"], row["output_tok"], row["est_cost"],
        "PASS" if row["quality_pass"] else "FAIL"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None,
                    help="run only the named config (e.g. 'baseline')")
    ap.add_argument("--levers", default="OUTPUT_DISCIPLINE",
                    help="comma-separated lever flag names to include in the matrix")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    levers = [x for x in (args.levers.split(",") if args.levers else []) if x.strip()]
    configs = build_matrix(levers)
    if args.only:
        configs = [c for c in configs if c["name"] == args.only]
        if not configs:
            print(f"no config named {args.only!r}", file=sys.stderr)
            return 2

    rows = run_matrix(configs, json_out=args.json)
    if args.json:
        print(json.dumps({"rows": rows}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
