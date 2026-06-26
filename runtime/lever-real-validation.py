#!/usr/bin/env python3
"""Real-world per-lever validation — the adversary-approved designs.

Each lever fires on a DIFFERENT trigger, so a single shared matrix can't validate
them (that's how F slipped through: its trigger was stubbed by the forced-tool edit
lane). This harness runs ONE adversary-approved real workload per lever against real
reasonix+DeepSeek and prints a verdict.

  python3 runtime/lever-real-validation.py A   # read-summary: cap + soft JSON layer
  python3 runtime/lever-real-validation.py B   # read_file_isolated (30-60KiB file)
  python3 runtime/lever-real-validation.py C   # shared read-cache (forced re-read)
  python3 runtime/lever-real-validation.py D   # pre-index semantic_search (embed model)
  python3 runtime/lever-real-validation.py E   # prefetch precision/recall (ACTUAL reads)

Adversary fixes baked in:
  A — cap-only third leg isolates the soft JSON layer from the 512 cap.
  B — 30-60KiB file (raw read_file returns FULL content; >64KiB auto-outlines);
      _input_by_type (input, not output); adoption from a free-choice lane.
  C — questions FORCE a file read + assert the baseline actually read it; >=8 lanes;
      delta derived from the run, not a constant.
  E — ground truth is ACTUAL reads from the shim read-trace sidecar, not the model's
      self-reported files_read (which is invented — the shim returns only final text).
"""
from __future__ import annotations
import importlib.util, json, os, signal, sys, time, glob
import concurrent.futures as cf
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("lmb", ROOT / "runtime" / "lever-matrix-bench.py")
lmb = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(lmb)
rwb = lmb.rwb
lane = rwb.lane
ledger_window = rwb.ledger_window

GW_FLAG = lmb._LEVER_ENV_MAP


def _pin_off_flags(flags: dict) -> dict:
    """Force every OTHER lever OFF so the one under test is the only variable
    (adversary B confound: shim_env=dict(os.environ) inherits ambient flags)."""
    base = {
        "CLAUDE_REASONIX_GATEWAY_OUTPUT_DISCIPLINE": "0",
        "CLAUDE_REASONIX_GATEWAY_READ_SUMMARY": "0",
        "CLAUDE_REASONIX_GATEWAY_READ_SUMMARY_CACHE": "0",
        "CLAUDE_REASONIX_PREINDEX": "0",
        "REASONIX_READ_ISOLATED": "0",
        "CLAUDE_REASONIX_PREFETCH_CONTEXT": "off",
    }
    base.update(flags)
    return base


def _run_lanes(flags: dict, prompts: list, schema=None, extra_env: dict | None = None) -> dict:
    """Start a fresh gateway with `flags` (+extra_env), run `prompts` concurrently,
    return {window:(t0,t1), lanes:[lane dicts]}. lane() keeps full text+tool_input."""
    eff = _pin_off_flags(flags)
    if extra_env:
        eff.update(extra_env)
    proc, port = lmb._start_gateway_with_flags(eff)
    try:
        t0 = time.time() - 0.1
        with cf.ThreadPoolExecutor(max_workers=len(prompts)) as ex:
            results = list(ex.map(lambda p: lane(port, p, schema), prompts))
        t1 = time.time() + 1
    finally:
        proc.send_signal(signal.SIGTERM)
        try: proc.wait(timeout=5)
        except Exception: proc.kill()
    return {"window": (t0, t1), "lanes": results}


# ---------------------------------------------------------------------------
# Lever A — read-summary: separate the 512 HARD cap from the soft JSON layer
# ---------------------------------------------------------------------------
A_FILES = [
    str(ROOT / "reasonix-native-gateway.py"),
    str(ROOT / "runtime" / "realworld-bench.py"),
    str(ROOT / "hooks" / "reasonix-workflow.py"),
]
def _a_prompt(f: str) -> str:
    return (f"Read the file {f} and explain in thorough detail everything it does. "
            "Walk through it function by function: for every function and class, describe its "
            "purpose, its inputs and outputs, the control flow and branches inside it, and how it "
            "relates to the rest of the file. Do not omit anything — narrate the whole file in full prose.")

def validate_A() -> dict:
    prompts = [_a_prompt(f) for f in A_FILES]
    print("A: 3 verbose free-form read lanes (want to dump prose). 3 legs: OFF / A-ON / cap-only.")
    off = _run_lanes({}, prompts)
    on = _run_lanes({GW_FLAG["READ_SUMMARY"]: "1"}, prompts)
    # cap-only: hard cap fires but soft JSON instruction suppressed via a no-op schema
    NOOP = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
    cap = _run_lanes({GW_FLAG["READ_SUMMARY"]: "1"}, prompts, schema=NOOP)

    def read_out(r): return lmb._output_by_type(r["window"]).get("read", 0)
    ro_off, ro_on, ro_cap = read_out(off), read_out(on), read_out(cap)
    # quality: A-ON lanes should still name the right file + give real findings.
    # Use text_full (NOT text_head[:80] — that truncation made names_file false-fail).
    # A 512-capped summary JSON may be cut mid-array → not parseable as tool_input, but
    # if it carries real findings text about the right file it is NOT a quality failure.
    quality = []
    for f, ln in zip(A_FILES, on["lanes"]):
        body = (ln.get("text_full") or ln.get("text_head") or "")
        names_file = Path(f).name.lower() in body.lower()
        has_findings = '"findings"' in body or "findings" in body.lower()
        real_empty = not body.strip()
        quality.append({"file": Path(f).name, "real_empty": real_empty,
                        "names_file": names_file, "has_findings": has_findings,
                        "body_len": len(body), "secs": ln.get("secs")})
    pct_drop = (ro_off - ro_on) / ro_off if ro_off else 0
    triggered = ro_off >= 1500  # off leg really wanted to dump
    return {
        "lever": "A", "read_out_off": ro_off, "read_out_on": ro_on, "read_out_caponly": ro_cap,
        "pct_drop": round(pct_drop, 3), "off_triggered": triggered,
        "cache_off": off and ledger_window(*off["window"]).get("weighted"),
        "cache_on": ledger_window(*on["window"]).get("weighted"),
        "quality": quality,
        "verdict_note": "PASS iff off_triggered AND pct_drop>=0.4 AND all quality.names_file AND no empty",
    }


# ---------------------------------------------------------------------------
# Lever B — read_file_isolated: 30-60KiB file (raw read returns FULL content)
# ---------------------------------------------------------------------------
B_FILE = str(ROOT / "docs" / "superpowers" / "plans" / "2026-06-23-reasonix-multiagent-cache.md")  # 36KB
def _b_prompt() -> str:
    return (f"Read the file {B_FILE} and answer: what is the single highest-impact "
            "cache-improvement it recommends, and why? Give a 2-3 sentence answer.")

def validate_B() -> dict:
    print(f"B: free-choice lane over a 36KB file (under 64KiB outline threshold → raw read dumps full).")
    p = [_b_prompt()]
    # OFF vs ON (free-choice, schema=None so the model can CHOOSE read_file_isolated)
    off = _run_lanes({}, p)
    on = _run_lanes({GW_FLAG["READ_ISOLATED"]: "1"}, p)
    # negative control: forced schema → model can't choose the tool → adoption must be 0
    NOOP = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
    neg = _run_lanes({GW_FLAG["READ_ISOLATED"]: "1"}, p, schema=NOOP)
    def in_read(r): return lmb._input_by_type(r["window"]).get("read", 0)
    in_off, in_on = in_read(off), in_read(on)
    drop = (in_off - in_on) / in_off if in_off else 0
    return {
        "lever": "B", "input_read_off": in_off, "input_read_on": in_on,
        "pct_input_drop": round(drop, 3), "abs_saved": in_off - in_on,
        "off_secs": [l.get("secs") for l in off["lanes"]],
        "on_secs": [l.get("secs") for l in on["lanes"]],
        "off_empty": [l.get("empty") for l in off["lanes"]],
        "on_empty": [l.get("empty") for l in on["lanes"]],
        "note": "adoption needs the read-trace dir (run B via diag_B_trace for actual tool-choice); "
                "input drop here is the parent-context signal. NET requires drop>=0.4 AND >=1500 saved.",
    }


# ---------------------------------------------------------------------------
# Lever C — shared read-cache: FORCE the file read + verify baseline read it
# ---------------------------------------------------------------------------
C_FILE = str(ROOT / "reasonix-native-gateway.py")
def _c_prompts(n: int) -> list:
    # Each lane MUST open the file (quote-verbatim question), same file → cache reuse
    qs = [
        "Quote verbatim the first line of the module docstring",
        "Quote verbatim the def line of the function named append_reasonix_cost",
        "Quote verbatim the line that defines DEFAULT for OUTPUT_DISCIPLINE_MAX_TOKENS_EDIT",
        "Quote verbatim the first line of the function classify_lane_type",
        "Quote verbatim the line where the gateway prints it is listening",
        "Quote verbatim the def line of lane_task_text",
        "Quote verbatim the first import statement in the file",
        "Quote verbatim the def line of build_preindex",
    ]
    return [f"Open {C_FILE} and {qs[i % len(qs)]}. Reply with only that line." for i in range(n)]

def validate_C() -> dict:
    N = 8
    print(f"C: {N} lanes ALL reading {Path(C_FILE).name} (forced quote-verbatim). One long-lived gateway per leg.")
    prompts = _c_prompts(N)
    # C OFF
    off = _run_lanes({}, prompts)
    # C ON — fresh empty cache so no cross-run leak
    cache_path = ROOT / "runtime" / "read-summary-cache.json"
    if cache_path.exists():
        cache_path.unlink()
    on = _run_lanes({GW_FLAG["READ_SUMMARY_CACHE"]: "1"}, prompts)
    # label-agnostic: these "Open X and quote..." prompts classify as 'unknown' (not
    # 'read'), but the INPUT signal is what matters — take ALL read-window rows
    # regardless of lane_type. _ledger_rows already filters to input>=500 within window.
    def all_inputs(r): return sorted(int(x.get("input_tokens") or 0) for x in lmb._ledger_rows(r["window"]))
    off_rows = all_inputs(off)
    on_rows = all_inputs(on)
    def med(xs): return sorted(xs)[len(xs)//2] if xs else 0
    return {
        "lever": "C", "n_lanes": N,
        "input_off_rows": off_rows, "input_on_rows": on_rows,
        "median_off": med(off_rows), "median_on": med(on_rows),
        "delta_median": med(off_rows) - med(on_rows),
        "pct_drop": round((med(off_rows) - med(on_rows)) / med(off_rows), 3) if med(off_rows) else 0,
        "baseline_read_ok": all(x >= 8000 for x in off_rows) if off_rows else False,
        "note": "PASS needs every OFF lane to actually read (input>=~file tokens) AND median_on materially < median_off.",
    }


# ---------------------------------------------------------------------------
# Lever D — pre-index: build index with the real embed model, query semantic_search
# ---------------------------------------------------------------------------
def validate_D() -> dict:
    print("D: build a real semantic index (nomic-embed-text) then a lane that should use semantic_search.")
    # confirm embed model reachable
    import urllib.request
    try:
        req = urllib.request.Request("http://localhost:11434/api/embeddings",
            data=json.dumps({"model": "nomic-embed-text", "prompt": "x"}).encode(),
            headers={"content-type": "application/json"})
        emb = json.loads(urllib.request.urlopen(req, timeout=10).read())
        dims = len(emb.get("embedding", []))
    except Exception as e:
        return {"lever": "D", "embed_ok": False, "err": str(e)[:120],
                "note": "embed model unreachable — D not testable"}
    p = ["Where in this codebase is the prefix-cache prime gate implemented and what does it do? "
         "Find the relevant code and explain briefly."]
    env = {"REASONIX_EMBED_PROVIDER": "ollama", "REASONIX_EMBED_MODEL": "nomic-embed-text",
           "REASONIX_READ_TRACE_DIR": str(ROOT / "runtime" / "dtrace")}
    (ROOT / "runtime" / "dtrace").mkdir(exist_ok=True)
    for f in glob.glob(str(ROOT / "runtime" / "dtrace" / "*.jsonl")):
        os.unlink(f)
    on = _run_lanes({GW_FLAG["PREINDEX"]: "1"}, p, extra_env=env)
    traces = []
    for f in glob.glob(str(ROOT / "runtime" / "dtrace" / "*.jsonl")):
        traces += [json.loads(l) for l in Path(f).read_text().splitlines() if l.strip()]
    return {
        "lever": "D", "embed_ok": True, "embed_dims": dims,
        "lane_empty": [l.get("empty") for l in on["lanes"]],
        "lane_secs": [l.get("secs") for l in on["lanes"]],
        "lane_head": [(l.get("text_head") or "")[:80] for l in on["lanes"]],
        "actual_reads": [t.get("path") for t in traces],
        "note": "D fail-open: build_preindex must not break the lane. Index built => lane can answer "
                "a find-the-code question. Headline: lane non-empty + answers correctly (index didn't break it).",
    }


# ---------------------------------------------------------------------------
# Lever E — prefetch precision/recall vs ACTUAL reads (shim trace), not self-report
# ---------------------------------------------------------------------------
E_TASKS = [
    ("Summarize what reasonix-native-gateway.py does at a high level.", ["reasonix-native-gateway.py"]),
    ("Explain the lane-spawning logic in engine/run-lane.mjs.", ["engine/run-lane.mjs"]),
    ("What does hooks/reasonix-workflow.py inject into a Workflow call? Read it and explain.", ["hooks/reasonix-workflow.py"]),
    ("Find where the cost ledger is written and what fields it stores. Read the relevant file and answer.", []),  # zero-named: must discover
    ("Read the README and the main gateway file, then summarize how a fan-out lane flows end to end.", ["README.md", "reasonix-native-gateway.py"]),
]
def validate_E() -> dict:
    print("E: advisory predict vs ACTUAL reads (shim read-trace). precision+recall on real reads, not self-report.")
    hook = lmb._hook
    predict = hook.predict_prefetch_files
    tdir = ROOT / "runtime" / "etrace"
    tdir.mkdir(exist_ok=True)
    for f in glob.glob(str(tdir / "*.jsonl")):
        os.unlink(f)
    prompts = [t[0] for t in E_TASKS]
    # advisory = zero prompt change; run free-form (schema=None) so the model loops tools freely
    env = {"REASONIX_READ_TRACE_DIR": str(tdir)}
    run = _run_lanes({"CLAUDE_REASONIX_PREFETCH_CONTEXT": "advisory"}, prompts, extra_env=env)
    # gather actual reads from all per-process trace files
    reads_by_prompt: dict = {}
    for f in glob.glob(str(tdir / "*.jsonl")):
        for l in Path(f).read_text().splitlines():
            if not l.strip(): continue
            o = json.loads(l)
            key = o.get("prompt", "")
            reads_by_prompt.setdefault(key, set()).add(Path(o["path"]).name)
    results = []
    tot_pred_hit = tot_pred = tot_actual_hit = tot_actual = 0
    for task, _ in E_TASKS:
        predicted = {Path(p).name for p in predict(task, str(ROOT))}
        # match actual reads by prompt prefix (trace stores prompt[:60])
        actual = set()
        for k, v in reads_by_prompt.items():
            if task.startswith(k) or k.startswith(task[:60]):
                actual |= v
        hit = predicted & actual
        results.append({"task": task[:50], "predicted": sorted(predicted),
                        "actual": sorted(actual), "hit": sorted(hit)})
        tot_pred_hit += len(hit); tot_pred += len(predicted)
        tot_actual_hit += len(hit); tot_actual += len(actual)
    precision = tot_pred_hit / tot_pred if tot_pred else None
    recall = tot_actual_hit / tot_actual if tot_actual else None
    return {"lever": "E", "pooled_precision": precision, "pooled_recall": recall,
            "per_task": results,
            "note": "advisory→inject worth it only if precision>=0.90 AND recall>=0.60 (else inject net-negative)."}


VALIDATORS = {"A": validate_A, "B": validate_B, "C": validate_C, "D": validate_D, "E": validate_E}

if __name__ == "__main__":
    which = sys.argv[1].upper() if len(sys.argv) > 1 else ""
    if which not in VALIDATORS:
        print("usage: lever-real-validation.py A|B|C|D|E"); sys.exit(2)
    t0 = time.time()
    res = VALIDATORS[which]()
    res["_secs"] = round(time.time() - t0, 1)
    print(json.dumps(res, indent=2, default=str))
