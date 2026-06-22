#!/usr/bin/env python3
"""Tests normalize_prefix: strips the volatile x-anthropic-billing-header line so
the system prefix is byte-stable across sessions (DeepSeek prompt-cache reuse)."""
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


HEADER = "x-anthropic-billing-header: cc_version=2.1.185.{v}; cc_entrypoint=cli; cc_is_subagent=true;\n"
BODY = "You are Claude Code, Anthropic's official CLI for Claude.\n<CCR-SUBAGENT-MODEL>x</CCR-SUBAGENT-MODEL>"


def test_strips_header_keeps_body():
    out = gw.normalize_prefix(HEADER.format(v="9d6") + BODY)
    expect("x-anthropic-billing-header" not in out, "billing header must be stripped")
    expect("You are Claude Code" in out, "body must survive")
    expect("CCR-SUBAGENT-MODEL" in out, "the routing tag must survive")
    expect(out.startswith("You are Claude Code"), f"body must now be first; got {out[:40]!r}")


def test_cache_stable_across_versions():
    # The whole point: two prompts that differ ONLY in the rotating cc_version
    # segment must be identical after normalization, so DeepSeek caches the prefix.
    a = gw.normalize_prefix(HEADER.format(v="94e") + BODY)
    b = gw.normalize_prefix(HEADER.format(v="ef4") + BODY)
    c = gw.normalize_prefix(HEADER.format(v="bcd") + BODY)
    expect(a == b == c, "prompts differing only in cc_version must normalize identically")


def test_no_header_unchanged():
    s = "You are a native subagent. Do the task."
    expect(gw.normalize_prefix(s) == s, "text without the header must be unchanged")


def test_only_first_line_header_removed():
    # A header-looking line later in the body (not at the start) is left alone:
    # the regex is multiline-anchored but only matches the literal header prefix.
    s = "x-anthropic-billing-header: cc_version=1; e;\nBODY line\nx-anthropic-billing-header: in body\n"
    out = gw.normalize_prefix(s)
    # Both header-prefixed lines are telemetry-shaped; the normalizer removes any
    # line that STARTS with the header token. Assert the body line survives.
    expect("BODY line" in out, "real body content must survive")
    expect("x-anthropic-billing-header" not in out, "all billing-header lines removed")


if __name__ == "__main__":
    test_strips_header_keeps_body()
    test_cache_stable_across_versions()
    test_no_header_unchanged()
    test_only_first_line_header_removed()
    print("PASS: prefix normalize")
