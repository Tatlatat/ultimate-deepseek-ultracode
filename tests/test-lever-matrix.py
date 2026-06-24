#!/usr/bin/env python3
"""Unit tests for the lever-matrix harness cost model + matrix shape.

These are MOCK-only: they exercise est_cost() and build_matrix() and never spawn
a gateway or call real DeepSeek. The real-DeepSeek baseline row is produced by
`python3 runtime/lever-matrix-bench.py --only baseline` (Step 6 of the brief).
"""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "lmb", Path(__file__).resolve().parent.parent / "runtime" / "lever-matrix-bench.py")
lmb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lmb)


def test_cost_model_weights_output_101x():
    # est_cost must price output ~101x a cache-hit and miss ~51x (owner split)
    c_hit = lmb.est_cost(input_tok=1000, cache_pct=100, output_tok=0)
    c_out = lmb.est_cost(input_tok=0, cache_pct=0, output_tok=1000)
    assert c_out / c_hit > 90, f"output must be ~101x a hit; got {c_out / c_hit:.0f}x"


def test_matrix_has_baseline_first():
    cfgs = lmb.build_matrix(levers=["OUTPUT_DISCIPLINE"])
    assert cfgs[0]["name"] == "baseline" and cfgs[0]["flags"] == {}


if __name__ == "__main__":
    test_cost_model_weights_output_101x()
    test_matrix_has_baseline_first()
    print("PASS: lever-matrix unit")
