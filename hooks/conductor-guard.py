#!/usr/bin/env python3
"""Conductor-mode guard: deny Opus's operator tools (Edit/Write/MultiEdit + clearly
mutating Bash) so the conductor delegates to the Reasonix fleet instead of doing the
work itself. Default OFF (CLAUDE_REASONIX_CONDUCTOR_REVIEW_ONLY). Fail-OPEN on any
uncertainty: the guard must never wedge the user. A pending escalation for the
session unlocks editing (Opus may fix a broken lane)."""
import json
import os
import re
import sys

_OPERATOR_TOOLS = {"Edit", "Write", "MultiEdit"}

# Clearly file-mutating Bash. Conservative: anything not matched is treated as a
# read/test/scope command and ALLOWED (fail-open).
_BASH_WRITE_RE = re.compile(
    r"(>>?)"            # output redirection > or >>
    r"|\bsed\s+-i\b"    # in-place sed
    r"|\btee\b"         # tee writes
    r"|\bperl\s+-i\b",  # in-place perl
)


def _truthy(name, fallback_name):
    v = os.environ.get(name)
    if v is None:
        v = os.environ.get(fallback_name, "")
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def _enabled():
    return _truthy("CLAUDE_REASONIX_CONDUCTOR_REVIEW_ONLY",
                   "CLAUDE_CODEX_CONDUCTOR_REVIEW_ONLY")


def _ledger_path(session_id):
    if not session_id:
        return None
    tmp = os.environ.get("TMPDIR") or "/tmp"
    return os.path.join(tmp, "reasonix-conductor-escalations", str(session_id))


def _has_unresolved_escalation(session_id):
    p = _ledger_path(session_id)
    if not p:
        return False
    try:
        return os.path.isfile(p) and os.path.getsize(p) > 0
    except Exception:
        return False


def bash_mutates(command):
    if not command:
        return False
    return bool(_BASH_WRITE_RE.search(command))


def decide(payload):
    """Returns (exit_code, message). 0 = allow, 2 = deny."""
    if not _enabled():
        return 0, ""
    tool = str(payload.get("tool_name") or "")
    if tool not in _OPERATOR_TOOLS and tool != "Bash":
        return 0, ""
    if tool == "Bash":
        cmd = ""
        ti = payload.get("tool_input")
        if isinstance(ti, dict):
            cmd = str(ti.get("command") or "")
        if not bash_mutates(cmd):
            return 0, ""
    # operator action detected; the only thing that unlocks it is a pending escalation
    sid = payload.get("session_id")
    if not sid:
        return 0, ""  # fail-open: can't key the valve, never wedge
    if _has_unresolved_escalation(sid):
        return 0, ""  # safety valve: a lane escalated/failed; Opus may fix it
    return 2, (
        "Conductor mode: you are the orchestrator, not the operator. Do NOT edit "
        "files yourself. Decompose this into lane(s) with an acceptanceTest and "
        "dispatch via mcp__reasonix_fleet__run_reasonix_worker (or an agent() lane "
        "in a Workflow). Reasonix workers write the files. (This block lifts "
        "automatically if a lane escalates/fails so you can intervene.)"
    )


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # fail-open: malformed hook JSON must never block the user
    code, msg = decide(payload)
    if code == 2:
        print(msg, file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
