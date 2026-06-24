#!/usr/bin/env python3
"""Self-heal preflight for claude-reasonix UltraCode / Dynamic Workflow.

Runs from the PreToolUse(Workflow) hook BEFORE a workflow's agent() lanes fire.
The known failure modes are all infrastructure preconditions that are detectable
up front, so instead of letting a workflow run and stall (0-token/180s x6) and
then stop passively, we probe the routing infra, auto-fix what we safely can,
and hand the rest back to the main agent as actionable context.

Everything here is FAIL-OPEN: any internal error must never block the workflow.
`preflight()` always returns (possibly-modified script, context string, report).

Detected-and-reported (cannot be auto-fixed from a hook, surfaced as context):
  - No live gateway/proxy reachable -> tell the agent to restart claude-reasonix.
  - Reasonix CLI not found in PATH -> tell the agent to install/login.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import time
import urllib.error
import urllib.request

FLEET_HOME = Path(__file__).resolve().parent.parent
SELFHEAL_LOG = FLEET_HOME / "runtime" / "workflow-selfheal.log"


def _log(report: dict) -> None:
    """Append one JSON line per preflight. Never raises."""
    try:
        SELFHEAL_LOG.parent.mkdir(parents=True, exist_ok=True)
        with SELFHEAL_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        pass


def _read_port(glob_pat: str) -> int | None:
    try:
        ports = sorted(
            (FLEET_HOME / "runtime").glob(glob_pat),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for p in ports:
            txt = p.read_text(encoding="utf-8").strip()
            if txt.isdigit():
                return int(txt)
    except Exception:
        pass
    return None


def _candidate_health_urls() -> list[str]:
    """URLs to probe for a live gateway/proxy, most-reliable first.

    The launcher DELETES the gateway/ccr-proxy .port files right after reading the
    port (rm -f), so a port-file glob almost always finds nothing — that was the
    false 'gateway not reachable' that made every preflight think the gateway was
    dead. The reliable source is this hook's own ANTHROPIC_BASE_URL: in router
    mode the launcher sets it to the CCR proxy URL, and the hook runs inside that
    Claude process, so it inherits it.
    """
    urls: list[str] = []
    base = os.getenv("ANTHROPIC_BASE_URL", "").strip().rstrip("/")
    if base.startswith("http://127.0.0.1") or base.startswith("http://localhost"):
        # strip a trailing /v1/messages etc. down to host:port
        host = base.split("/v1", 1)[0]
        urls.append(host + "/health")
    # Fallbacks: any port file that happens to still exist.
    for pat in ("ccr-proxy.*.port", "gateway.*.port"):
        p = _read_port(pat)
        if p is not None:
            urls.append(f"http://127.0.0.1:{p}/health")
    return urls


def _gateway_reachable() -> tuple[bool, str]:
    """Probe a live gateway/proxy /health. Returns (ok, detail)."""
    urls = _candidate_health_urls()
    if not urls:
        return False, "no ANTHROPIC_BASE_URL and no port file (gateway probably not in router mode)"
    last = ""
    for url in urls:
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=4) as resp:
                if resp.status == 200:
                    return True, f"{url} healthy"
                last = f"{url} returned {resp.status}"
        except Exception as exc:  # noqa: BLE001 - probe must never raise
            last = f"{url} unreachable: {exc}"
    return False, last or "no reachable gateway"



def preflight(script: str, mode: str) -> tuple[str, str, dict]:
    """Probe infra, auto-fix what is safe, return (script, context, report).

    Never raises. `context` is appended to the hook's additionalContext.
    """
    report = {"mode": mode, "ts": time.time(), "checks": {}, "actions": []}
    notes: list[str] = []

    # If there's no usable script string, skip all script-mutating work but still
    # surface infra checks. Prevents AttributeError on .count() for a None script.
    if not isinstance(script, str):
        report["note"] = "no script string; infra-checks only"

    try:
        gw_ok, gw_detail = _gateway_reachable()
        report["checks"]["gateway"] = {"ok": gw_ok, "detail": gw_detail}
        if not gw_ok:
            notes.append(
                "SELF-HEAL: no live claude-reasonix gateway/proxy reachable "
                f"({gw_detail}). Worker lanes will stall. Action: restart "
                "`claude-reasonix router` (or `claude-reasonix` for native) so a fresh "
                "gateway with current code is started, then re-run the workflow."
            )

        if os.getenv("CLAUDE_REASONIX_FLAVOR", os.getenv("CLAUDE_CODEX_FLAVOR")) == "reasonix":
            reasonix_present = bool(shutil.which(os.getenv("REASONIX_BIN", "reasonix")))
            report["checks"]["reasonix_cli"] = {"present": reasonix_present}
            if not reasonix_present:
                notes.append(
                    "SELF-HEAL: Reasonix CLI not found in PATH "
                    f"(looked for {os.getenv('REASONIX_BIN', 'reasonix')!r}). "
                    "Reasonix worker lanes will fail. Action: install and log in to "
                    "the Reasonix CLI (`reasonix login`), then restart claude-reasonix."
                )
    except Exception as exc:  # noqa: BLE001 - fail open, never block the workflow
        report["error"] = repr(exc)

    _log(report)
    context = "\n".join(notes)
    return script, context, report
