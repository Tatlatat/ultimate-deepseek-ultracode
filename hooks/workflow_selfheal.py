#!/usr/bin/env python3
"""Self-heal preflight for claude-codex UltraCode / Dynamic Workflow.

Runs from the PreToolUse(Workflow) hook BEFORE a workflow's agent() lanes fire.
The known failure modes are all infrastructure preconditions that are detectable
up front, so instead of letting a workflow run and stall (0-token/180s x6) and
then stop passively, we probe the routing infra, auto-fix what we safely can,
and hand the rest back to the main agent as actionable context.

Everything here is FAIL-OPEN: any internal error must never block the workflow.
`preflight()` always returns (possibly-modified script, context string, report).

Auto-fixes applied (router/native mode only):
  - DeepSeek lanes with no DEEPSEEK_API_KEY -> remap deepseek-* agentTypes to
    claude-codex-pro in THIS run's rewritten script (codex-cli needs no key),
    so architecture/infra/deep lanes run instead of 401-ing.

Detected-and-reported (cannot be auto-fixed from a hook, surfaced as context):
  - No live gateway/proxy reachable -> tell the agent to restart claude-codex.
  - codex-cli ChatGPT token expired/near-expiry -> tell the agent to re-login.
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

# agentType -> backing model, mirrors runtime/ccr-agents.json
DEEPSEEK_AGENT_TYPES = ("deepseek-deep", "deepseek-architecture")


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


def _codex_auth_status() -> tuple[str, str]:
    """Returns (state, detail): state in {ok, expiring, expired, missing, apikey, unknown}."""
    auth = Path.home() / ".codex" / "auth.json"
    if not auth.is_file():
        return "missing", "~/.codex/auth.json not found"
    try:
        d = json.loads(auth.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return "unknown", f"auth.json unreadable: {exc}"
    if d.get("auth_mode") != "chatgpt":
        # API-key mode: no token to expire
        return "apikey", f"auth_mode={d.get('auth_mode')}"
    tok = (d.get("tokens") or {}).get("access_token") or ""
    if tok.count(".") != 2:
        return "unknown", "no decodable access_token"
    try:
        import base64

        payload_seg = tok.split(".")[1]
        payload_seg += "=" * (-len(payload_seg) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_seg))
        exp = payload.get("exp")
        if not exp:
            return "unknown", "token has no exp"
        remaining_h = (exp - time.time()) / 3600
        if remaining_h <= 0:
            return "expired", f"chatgpt token expired {abs(remaining_h):.1f}h ago"
        if remaining_h < 2:
            return "expiring", f"chatgpt token expires in {remaining_h:.1f}h"
        return "ok", f"chatgpt token valid for {remaining_h:.0f}h"
    except Exception as exc:  # noqa: BLE001
        return "unknown", f"token decode failed: {exc}"


def _deepseek_key_present() -> bool:
    return bool(os.getenv("DEEPSEEK_API_KEY") or os.getenv("CLAUDE_CODEX_DEEPSEEK_API_KEY"))


def _remap_deepseek_to_codex(script: str) -> tuple[str, int]:
    """Route deepseek-* lanes to codex-cli (no key needed) for this run.

    Enforcement is purely the __claudeCodexForceCodexOnly sentinel, which the
    native wrapper honours at runtime for BOTH explicit agentType:'deepseek-*'
    and label/phase hints that would route to deepseek. We deliberately do NOT
    string-replace 'deepseek-*' in the script body, because that body contains
    the injected wrapper source whose mapping table must stay intact (so routing
    reverts cleanly once a DEEPSEEK_API_KEY is present and the sentinel is off).

    Returns (script, n) where n counts explicit user-authored deepseek lanes
    detected (for reporting only).
    """
    # Guard non-str scripts (None / unexpected types) — return unchanged so the
    # caller never crashes on .count(); the sentinel can't be inserted anyway.
    if not isinstance(script, str):
        return script, 0
    # Count explicit deepseek lanes the USER wrote (opts.agentType:'deepseek-*'),
    # not the mapping literals inside the wrapper, for an honest report number.
    count = sum(script.count(f"agentType: '{at}'") + script.count(f"agentType:'{at}'")
                for at in DEEPSEEK_AGENT_TYPES)
    new = script
    # Check for the ASSIGNMENT, not the identifier — the wrapper source itself
    # references __claudeCodexForceCodexOnly (it reads the flag), so a bare
    # substring check would wrongly think the sentinel is already set.
    sentinel = "globalThis.__claudeCodexForceCodexOnly = true;"
    if sentinel not in new:
        # Insert AFTER the `export const meta = {...}` block, never before it.
        # Workflow requires meta to be the first statement; prepending the sentinel
        # to the top produced a "meta must be first / syntax error" on every run.
        new = _insert_after_meta(new, sentinel)
    return new, count


def _insert_after_meta(script: str, snippet: str) -> str:
    """Insert `snippet` on its own line right after the `export const meta = {...}`
    object literal. Falls back to prepending only if no meta block is found
    (a script with no meta won't run anyway, so syntax order doesn't matter)."""
    marker = "export const meta"
    start = script.find(marker)
    if start == -1:
        return snippet + "\n" + script
    brace = script.find("{", start)
    if brace == -1:
        return snippet + "\n" + script
    depth = 0
    i = brace
    in_str = None
    while i < len(script):
        ch = script[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == in_str:
                in_str = None
        elif ch in ("'", '"', "`"):
            in_str = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                # swallow a trailing semicolon/newline
                while end < len(script) and script[end] in " \t;":
                    end += 1
                if end < len(script) and script[end] == "\n":
                    end += 1
                return script[:end] + snippet + "\n" + script[end:]
        i += 1
    return snippet + "\n" + script


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
                "SELF-HEAL: no live claude-codex gateway/proxy reachable "
                f"({gw_detail}). Worker lanes will stall. Action: restart "
                "`claude-codex router` (or `claude-codex` for native) so a fresh "
                "gateway with current code is started, then re-run the workflow."
            )

        auth_state, auth_detail = _codex_auth_status()
        report["checks"]["codex_auth"] = {"state": auth_state, "detail": auth_detail}
        if auth_state in {"expired", "missing"}:
            notes.append(
                f"SELF-HEAL: codex-cli auth problem ({auth_detail}). claude-codex-pro "
                "lanes will fail. Action: run `codex login` (ChatGPT) before retrying."
            )
        elif auth_state == "expiring":
            notes.append(f"SELF-HEAL warning: {auth_detail}; lanes may fail mid-run.")

        # Auto-fix: deepseek lanes with no key -> route via codex-cli instead.
        if isinstance(script, str) and mode in {"router", "native"} and not _deepseek_key_present():
            new_script, n = _remap_deepseek_to_codex(script)
            report["checks"]["deepseek_key"] = {"present": False}
            if n > 0 or new_script != script:
                script = new_script
                report["actions"].append(
                    {"action": "remap_deepseek_to_codex", "explicit_lanes": n}
                )
                notes.append(
                    "SELF-HEAL applied: DEEPSEEK_API_KEY not set -> deepseek-* worker "
                    "lanes routed to claude-codex-pro (codex-cli) for this run so they "
                    "do not 401. Set DEEPSEEK_API_KEY and restart claude-codex to use "
                    "real DeepSeek."
                )
        else:
            report["checks"]["deepseek_key"] = {"present": _deepseek_key_present()}

        if os.getenv("CLAUDE_CODEX_FLAVOR") == "reasonix":
            reasonix_present = bool(shutil.which(os.getenv("REASONIX_BIN", "reasonix")))
            report["checks"]["reasonix_cli"] = {"present": reasonix_present}
            if not reasonix_present:
                notes.append(
                    "SELF-HEAL: Reasonix CLI not found in PATH "
                    f"(looked for {os.getenv('REASONIX_BIN', 'reasonix')!r}). "
                    "Reasonix worker lanes will fail. Action: install and log in to "
                    "the Reasonix CLI (`reasonix login`), then restart claude-codex."
                )
    except Exception as exc:  # noqa: BLE001 - fail open, never block the workflow
        report["error"] = repr(exc)

    _log(report)
    context = "\n".join(notes)
    return script, context, report
