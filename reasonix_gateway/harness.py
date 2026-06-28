import os as _os
import re
from typing import Any
from .env import JSON, env_truthy
from .text import lane_task_text


def escalation_ledger_path(session_id):
    """Absolute path to the per-session conductor escalation ledger, or None.

    Both the conductor-guard hook (reads) and the gateway (appends) compute this
    the same way so they agree on the file. Does NOT create the file. Returns None
    for a falsy session_id (the caller then fails open — see conductor-guard.py)."""
    if not session_id:
        return None
    tmp = _os.environ.get("TMPDIR") or "/tmp"
    return _os.path.join(tmp, "reasonix-conductor-escalations", str(session_id))


def record_escalation(session_id, note):
    """Append an escalation note to the per-session conductor ledger so the
    conductor-guard hook lifts the edit block (Opus may fix the broken lane).
    No-op + never raises if session_id is falsy or the write fails — this runs
    on the lane result path and must not break it."""
    path = escalation_ledger_path(session_id)
    if not path:
        return
    try:
        _os.makedirs(_os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write((note or "LANE_ESCALATE") + "\n")
    except Exception:
        pass


def _lane_fail_marker_on() -> bool:
    return env_truthy("CLAUDE_REASONIX_GATEWAY_LANE_FAIL_MARKER",
                      "CLAUDE_CODEX_GATEWAY_LANE_FAIL_MARKER", default="1")


def lane_unverified_reply(reason: str) -> str:
    """A3: when a lane times out/errors, return a machine-readable marker so a workflow
    distinguishes 'could not verify' from 'verified=false'. A verify lane that gets this
    must be treated UNVERIFIED and its finding KEPT, never silently rejected (the
    level-3.1 bug: a timed-out verify with an empty verdict was counted as 'rejected').
    Returns '' when the flag is off (caller restores the old bare-error behavior)."""
    if not _lane_fail_marker_on():
        return ""
    return (f"LANE_UNVERIFIED: this lane did not complete ({reason}). "
            "Treat as UNVERIFIED (could not check), NOT as a false/disproven finding — "
            "keep the item and re-run with a smaller scope.")


# --- C3: weak-executor harness helpers (CLAUDE_REASONIX_GATEWAY_LANE_HARNESS, default ON) ---
# The shim (engine/run-lane.mjs) runs an acceptance-test retry loop and returns a
# terse `__HARNESS__:<status>:<attempts>:<lesson>` text.  The gateway parses it into
# a SHORT structured lane reply (<200 chars) so the Opus orchestrator reviews only
# failures (ESCALATE marker) and never re-reads raw files — fixing the 97% cache-read
# blowup.  When the flag is off every path is byte-identical to today.

def _lane_harness_on() -> bool:
    return env_truthy("CLAUDE_REASONIX_GATEWAY_LANE_HARNESS",
                      "CLAUDE_CODEX_GATEWAY_LANE_HARNESS", default="1")


def parse_harness_result(text: str) -> JSON | None:
    """Parse the shim's harness summary text `__HARNESS__:<status>:<attempts>:<lesson>`.
    Returns None for a normal (non-harness) reply so the gateway passes it through
    unchanged (byte-inert when the harness is off)."""
    if not isinstance(text, str) or not text.startswith("__HARNESS__:"):
        return None
    parts = text.split(":", 3)  # ['__HARNESS__', status, attempts, lesson]
    if len(parts) < 3:
        return None
    try:
        attempts = int(parts[2])
    except (TypeError, ValueError):
        attempts = 0
    return {"status": parts[1], "attempts": attempts, "lesson": parts[3] if len(parts) > 3 else ""}


def harness_lane_reply(parsed: JSON) -> str:
    """A SHORT structured lane reply for the orchestrator. A passed lane returns a terse
    OK; a stagnated/exhausted lane carries an ESCALATE marker + the lesson so Opus reviews
    ONLY the failures (never re-reading raw files — the 97% cache-read fix)."""
    st = parsed.get("status")
    att = parsed.get("attempts")
    if st == "pass":
        return f"LANE_OK pass: completed in {att} attempt(s), acceptance test green."
    return (f"LANE_ESCALATE: status={st} after {att} attempt(s). "
            f"Could not finish; orchestrator should take over this lane. Lesson: {parsed.get('lesson','')}")


def lane_acceptance_test(messages: Any) -> str:
    txt = lane_task_text(messages)
    for line in txt.splitlines():
        s = line.strip()
        if s.upper().startswith("ACCEPTANCE_TEST:"):
            return _clean_acceptance_command(s.split(":", 1)[1].strip())
    return ""


# Prose-markers the model tends to APPEND to (or wrap) an ACCEPTANCE_TEST command
# (measured on the deno_lint run): "cargo test --lib x passes WITH the added cases",
# "cargo test x (must pass) AND cargo check (must stay green)". lane_acceptance_test
# fed the WHOLE tail to the shim execSync -> `cargo test x passes ...` -> "unexpected
# argument 'passes'" -> the acceptance FAILED for a spurious reason and the lane
# stagnated though the code was correct. We extract just the runnable command(s).
_PROSE_CUT_RE = re.compile(
    r"\s+(?:passes\b|should\b|must\b|to (?:verify|confirm|ensure)\b|with the\b|"
    r"and (?:confirm|verify|ensure|it )\b)",
    re.I)


def _clean_acceptance_command(raw: str) -> str:
    """Extract the runnable shell command from an ACCEPTANCE_TEST value the model may
    have polluted with prose. Strategy: unwrap backticks/quotes; split on ' AND ' into
    candidate commands; from each, drop a trailing parenthetical "(must pass)" and any
    prose tail starting at a prose-marker; keep candidates that look like a command
    (contain a known runner or a '/'); join multiple with '&&'. Falls back to the
    cut-at-first-prose-marker of the whole string if nothing parses."""
    raw = raw.strip()
    # unwrap a single surrounding backtick/quote pair
    if len(raw) >= 2 and raw[0] in "`'\"" and raw[-1] == raw[0]:
        raw = raw[1:-1].strip()
    runners = ("bun ", "cargo ", "npm ", "pnpm ", "yarn ", "node ", "deno ",
               "pytest", "python ", "go ", "make ", "jest", "vitest", "./")
    def _looks_cmd(c: str) -> bool:
        cl = c.lower()
        return any(cl.startswith(r) or (" " + r) in (" " + cl) for r in runners)
    parts = re.split(r"\s+\bAND\b\s+", raw, flags=re.I)
    cleaned = []
    for p in parts:
        p = p.strip()
        p = re.sub(r"\s*\([^)]*\)\s*$", "", p).strip()   # drop trailing "(must pass)"
        cut = _PROSE_CUT_RE.search(p)
        if cut:
            p = p[:cut.start()].strip()
        p = re.sub(r"\s*\([^)]*\)\s*$", "", p).strip()   # again after the cut
        if p and _looks_cmd(p):
            cleaned.append(p)
    if cleaned:
        return " && ".join(cleaned)
    # fallback: cut the whole string at the first prose marker / trailing paren
    p = re.sub(r"\s*\([^)]*\)\s*$", "", raw).strip()
    cut = _PROSE_CUT_RE.search(p)
    return (p[:cut.start()].strip() if cut else p)
