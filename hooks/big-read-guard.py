#!/usr/bin/env python3
"""Big-read guard: stop the ORCHESTRATOR (Opus) from reading a huge file in one shot
into its OWN main context — the measured root cause of autocompact thrashing (one
vscode.d.ts read = ~185K tokens = 93% of the context window). Lanes are already
shielded by the engine's outline-threshold; Opus was not. This is that missing shield.

A PreToolUse hook on Read. If the target file is larger than the threshold AND the
Read is not already range-limited (no offset/limit), DENY (exit 2) with a message
telling Opus to read a range or delegate the read to a lane. A range-limited Read
(offset+limit) is allowed — that's the correct way to read part of a big file.

Default ON; CLAUDE_REASONIX_BIG_READ_GUARD=0 disables. Fail-OPEN: any uncertainty
(missing path, stat error, malformed JSON) → allow. Never wedge the user."""
import json
import os
import sys

DEFAULT_THRESHOLD = 51200  # 50 KiB (~12.8K tokens). Above this a single read starts
# to dominate context; vscode.d.ts (742KB) is 14x this. More generous than the lane's
# 14KB so Opus can still read normal source files whole.


def _threshold():
    raw = os.environ.get("CLAUDE_REASONIX_BIG_READ_THRESHOLD_BYTES", "").strip()
    try:
        v = int(raw)
        return v if v > 0 else DEFAULT_THRESHOLD
    except Exception:
        return DEFAULT_THRESHOLD


def _enabled():
    return os.environ.get("CLAUDE_REASONIX_BIG_READ_GUARD", "1").strip().lower() \
        not in {"0", "false", "no", "off"}


def decide(payload):
    """(exit_code, message). 0 = allow, 2 = deny."""
    if not _enabled():
        return 0, ""
    if str(payload.get("tool_name") or "") != "Read":
        return 0, ""
    ti = payload.get("tool_input")
    if not isinstance(ti, dict):
        return 0, ""
    # A range-limited read is the CORRECT way to read part of a big file → allow.
    if ti.get("offset") is not None or ti.get("limit") is not None:
        return 0, ""
    fp = ti.get("file_path")
    if not fp:
        return 0, ""  # fail-open
    # Resolve a relative file_path against the session cwd (Claude Code sends `cwd`
    # in the payload). The hook process runs with a different cwd, so a relative
    # path would otherwise fail to stat → fail-open → the guard silently misses the
    # big file (the bug that let vscode.d.ts through). Try payload cwd first.
    if not os.path.isabs(fp):
        base = payload.get("cwd") or os.getcwd()
        cand = os.path.join(base, fp)
        if os.path.exists(cand):
            fp = cand
    try:
        size = os.path.getsize(fp)
    except Exception:
        return 0, ""  # fail-open: can't stat → allow
    th = _threshold()
    if size <= th:
        return 0, ""
    approx_tok = size // 4
    return 2, (
        f"BIG READ BLOCKED: {fp} is {size} bytes (~{approx_tok} tokens) — reading it "
        f"whole would flood your context (this is the autocompact-thrashing cause). "
        f"Instead: (a) Read a RANGE with offset+limit, (b) Grep/search for what you "
        f"need, or (c) dispatch a Reasonix lane to read+summarize it and return only "
        f"the summary. Do NOT read the whole file into your own context."
    )


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # fail-open
    if not isinstance(payload, dict):
        return 0
    try:
        code, msg = decide(payload)
    except Exception:
        return 0  # fail-open: never wedge
    if code == 2:
        print(msg, file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
