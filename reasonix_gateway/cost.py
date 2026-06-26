import json
import time
from pathlib import Path

from .env import JSON


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


def append_reasonix_cost(ledger_path: str, usage: JSON, cwd: str = "", model: str = "",
                         claude_equiv: float | None = None, lane_type: str = "unknown") -> None:
    """Append one per-lane cost record to the session cost ledger (JSONL).

    Fail-open: a broken/unwritable ledger path must never break a lane.
    The reasonix CLI's own ~/.reasonix/usage.jsonl has session=null and no cwd,
    so it can't attribute cost to a session/project — this ledger adds cwd + ts.
    `lane_type` classifies the lane (read/edit/review/workflow/...); the caller
    passes "unknown" until Task 2 wires real classification.
    """
    try:
        record = {
            "ts": time.time(),
            "cost_usd": usage.get("reasonix_cost_usd"),
            "claude_equiv_usd": claude_equiv,
            "cache_pct": usage.get("reasonix_cache_pct"),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "lane_type": lane_type,
            "cwd": cwd,
            "model": model,
        }
        path = Path(ledger_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def summarize_reasonix_cost(ledger_path: str) -> JSON:
    """Aggregate the cost ledger into a summary dict. Missing/empty → zeros."""
    lanes = 0
    total = 0.0
    claude_equiv = 0.0
    in_tok = 0
    out_tok = 0
    cache_vals: list[float] = []
    try:
        with open(ledger_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                lanes += 1
                c = rec.get("cost_usd")
                if isinstance(c, (int, float)):
                    total += float(c)
                ce = rec.get("claude_equiv_usd")
                if isinstance(ce, (int, float)):
                    claude_equiv += float(ce)
                if isinstance(rec.get("input_tokens"), int):
                    in_tok += rec["input_tokens"]
                if isinstance(rec.get("output_tokens"), int):
                    out_tok += rec["output_tokens"]
                cp = rec.get("cache_pct")
                if isinstance(cp, (int, float)):
                    cache_vals.append(float(cp))
    except FileNotFoundError:
        pass
    except Exception:
        pass
    avg_cache = round(sum(cache_vals) / len(cache_vals), 1) if cache_vals else 0.0
    saved = claude_equiv - total
    saved_pct = round(100.0 * saved / claude_equiv, 1) if claude_equiv > 0 else 0.0
    return {
        "lanes": lanes,
        "total_usd": total,
        "claude_equiv_usd": claude_equiv,
        "saved_usd": saved,
        "saved_pct": saved_pct,
        "avg_cache_pct": avg_cache,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "avg_per_lane_usd": round(total / lanes, 6) if lanes else 0.0,
    }
