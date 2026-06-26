#!/usr/bin/env python3
"""A/B the two orchestrator input-cut levers on real reasonix+DeepSeek.

  python3 runtime/input-cut-ab.py BROADEN   # READER_BROADEN + Lever A reach
  python3 runtime/input-cut-ab.py OVERSCOPE  # OVERSCOPE_REJECT input avoided

BROADEN: a read-heavy lane whose verb EVADES the old classifier ('analyze ...').
  OFF  = broaden off  -> lane classifies 'unknown' -> Lever A never fires -> full dump.
  ON   = broaden on + READ_SUMMARY on -> lane classifies 'read' -> Lever A caps it.
  Signal: read/unknown lane OUTPUT tokens (what A compresses) + the downstream input
  that output becomes. Quality: the ON answer must still be correct.

OVERSCOPE: an over-broad lane that names many files (the 532K-token failure shape).
  OFF  = lane runs, ingests every file -> huge input_tokens (bucket-3).
  ON   = lane is rejected (tiny reply) AND the controller re-dispatches as small
         per-file lanes -> we run those small lanes and sum their input.
  Signal: total input_tokens of the ONE giant lane (OFF) vs the rejection + the
  decomposed small lanes (ON). The input AVOIDED is the win.
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
GW = lmb._LEVER_ENV_MAP


def _start(flags: dict):
    return lmb._start_gateway_with_flags(flags)


def _run(flags: dict, prompts: list, schema=None) -> dict:
    proc, port = _start(flags)
    try:
        t0 = time.time() - 0.1
        with cf.ThreadPoolExecutor(max_workers=max(1, len(prompts))) as ex:
            res = list(ex.map(lambda p: lane(port, p, schema), prompts))
        t1 = time.time() + 1
    finally:
        proc.send_signal(signal.SIGTERM)
        try: proc.wait(timeout=5)
        except Exception: proc.kill()
    return {"window": (t0, t1), "lanes": res}


def _in_sum(w):  # raw input tokens in the window
    return sum(int(r.get("input_tokens") or 0) for r in lmb._ledger_rows(w))
def _out_by_type(w): return lmb._output_by_type(w)

def _eff_miss(w) -> dict:
    """Cache-WEIGHTED input — the real cost driver. raw input_tokens is misleading
    (most of it is the cached ~14K shared prefix). The billable part is the miss:
    sum(input * (1 - cache_pct/100)). The ledger field is `cache_pct` (NOT
    reasonix_cache_pct — that name does not exist; using it reads None and looks like
    0% cache, the harness bug this fixes)."""
    rows = [r for r in lmb._ledger_rows(w) if isinstance(r.get("cache_pct"), (int, float))]
    raw = sum(int(r.get("input_tokens") or 0) for r in rows)
    miss = sum(int(r["input_tokens"]) * (1 - r["cache_pct"] / 100) for r in rows)
    out = sum(int(r.get("output_tokens") or 0) for r in rows)
    weighted = round(100 * (1 - miss / raw), 2) if raw else None
    # cost units from the results-doc model: hit=1, miss=51, out=101
    cost = (raw - miss) * 1 + miss * 51 + out * 101
    return {"raw_in": raw, "weighted_cache": weighted, "eff_miss": round(miss),
            "out": out, "cost_units": round(cost), "lanes": len(rows)}


# --- BROADEN: read-heavy evading-verb lane, A off vs on -----------------------
GW_FILE = str(ROOT / "reasonix-native-gateway.py")
def _broaden_prompt(f):
    # 'analyze' EVADES the old reader regex -> 'unknown' when broaden off.
    return (f"Analyze the file {f} in thorough detail. Walk through every function and "
            "class: its purpose, inputs/outputs, control flow, and how it fits the whole "
            "file. Be comprehensive — long detailed prose is expected.")

def ab_broaden() -> dict:
    files = [GW_FILE, str(ROOT / "runtime" / "realworld-bench.py"), str(ROOT / "hooks" / "reasonix-workflow.py")]
    prompts = [_broaden_prompt(f) for f in files]
    print("BROADEN A/B: 3 'analyze ...' read-heavy lanes (evade old classifier).", flush=True)
    # OFF: broaden off + A off -> lane 'unknown', no cap
    off = _run({}, prompts)
    # ON: broaden on + A on -> lane 'read', Lever A caps + summary-instructs
    on = _run({GW["READER_BROADEN"] if "READER_BROADEN" in GW else "CLAUDE_REASONIX_GATEWAY_READER_BROADEN": "1",
               GW["READ_SUMMARY"]: "1"}, prompts)
    off_o = _out_by_type(off["window"]); on_o = _out_by_type(on["window"])
    # output of the read/unknown lanes (where the lever bites)
    off_read = off_o.get("read", 0) + off_o.get("unknown", 0)
    on_read = on_o.get("read", 0) + on_o.get("unknown", 0)
    qual = [{"file": Path(f).name, "empty": l.get("empty"),
             "names_file": Path(f).name.lower() in (l.get("text_full") or l.get("text_head") or "").lower(),
             "body_len": len(l.get("text_full") or l.get("text_head") or ""), "secs": l.get("secs")}
            for f, l in zip(files, on["lanes"])]
    return {"lever": "BROADEN", "out_off": off_read, "out_on": on_read,
            "pct_drop": round((off_read - on_read) / off_read, 3) if off_read else 0,
            "in_off": _in_sum(off["window"]), "in_on": _in_sum(on["window"]),
            "quality": qual,
            "note": "out_off is the verbose dump A could not reach before broaden; out_on is after. "
                    "Win = pct_drop with quality (names_file true, not empty)."}


# --- OVERSCOPE: one giant lane (OFF) vs reject + decomposed small lanes (ON) ---
def ab_overscope() -> dict:
    test_files = sorted(glob.glob(str(ROOT / "tests" / "test-*.py")))[:12]
    rel = [os.path.relpath(f, ROOT) for f in test_files]
    giant = ("Analyze all the test files and summarize what each one covers: " + " ".join(rel))
    print(f"OVERSCOPE A/B: 1 giant lane over {len(rel)} files vs reject+decompose.", flush=True)
    # OFF: the giant lane runs, ingests all files
    off = _run({}, [giant])
    off_cost = _eff_miss(off["window"])
    off_secs = [l.get("secs") for l in off["lanes"]]
    # ON: the giant lane is REJECTED (tiny reply). Then the controller re-dispatches as
    # small per-file lanes WITH a warm-up first (PREFIX_GUIDE point 8) so the shared
    # prefix caches across them — that warming is the whole reason decompose can win.
    on_reject = _run({"CLAUDE_REASONIX_GATEWAY_OVERSCOPE_REJECT": "1"}, [giant])
    rej_text = (on_reject["lanes"][0].get("text_full") or on_reject["lanes"][0].get("text_head") or "")
    rejected = "decompose" in rej_text.lower()
    small_prompts = [f"Read {r} and summarize in one sentence what it covers." for r in rel]
    small = _run({GW["READ_SUMMARY"]: "1"}, small_prompts)
    small_cost = _eff_miss(small["window"])
    return {"lever": "OVERSCOPE", "n_files": len(rel), "reject_fired": rejected,
            "GIANT_off": off_cost, "giant_secs": off_secs,
            "DECOMPOSED_on": small_cost,
            "verdict": ("compare eff_miss + cost_units, NOT raw_in (raw is mostly cached prefix). "
                        "decompose wins ONLY if its eff_miss/cost < the giant lane's. On a moderate "
                        "file count the giant lane's cold miss is small, so decompose may be a WASH or "
                        "WORSE (more output). The win is at the EXTREME (833-file/532K giant) where the "
                        "giant's cold miss is enormous. Reject MUST fire or unmeasured.")}


AB = {"BROADEN": ab_broaden, "OVERSCOPE": ab_overscope}
if __name__ == "__main__":
    w = sys.argv[1].upper() if len(sys.argv) > 1 else ""
    if w not in AB:
        print("usage: input-cut-ab.py BROADEN|OVERSCOPE"); sys.exit(2)
    t0 = time.time()
    r = AB[w]()
    r["_secs"] = round(time.time() - t0, 1)
    print(json.dumps(r, indent=2, default=str))
