#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from workflow_selfheal import preflight as _selfheal_preflight
except Exception:  # noqa: BLE001 - self-heal is optional; never break rewriting
    _selfheal_preflight = None


MARKER = "__codexWorkflowAgent"


def find_matching_brace(text: str, open_index: int) -> int:
    depth = 0
    i = open_index
    quote = None
    escape = False
    line_comment = False
    block_comment = False
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""

        if line_comment:
            if ch == "\n":
                line_comment = False
            i += 1
            continue
        if block_comment:
            if ch == "*" and nxt == "/":
                block_comment = False
                i += 2
            else:
                i += 1
            continue
        if quote:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                quote = None
            i += 1
            continue

        if ch == "/" and nxt == "/":
            line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            block_comment = True
            i += 2
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            i += 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def insert_after_meta(script: str, injection: str) -> str:
    marker = "export const meta"
    start = script.find(marker)
    if start == -1:
        return injection + "\n" + script

    brace = script.find("{", start)
    if brace == -1:
        return injection + "\n" + script

    end = find_matching_brace(script, brace)
    if end == -1:
        return injection + "\n" + script

    while end + 1 < len(script) and script[end + 1] in " \t;":
        end += 1
    if end + 1 < len(script) and script[end + 1] == "\n":
        end += 1

    return script[: end + 1] + "\n" + injection + "\n" + script[end + 1 :]


def rewrite_agent_calls(script: str) -> tuple[str, int]:
    out = []
    i = 0
    count = 0
    quote = None
    escape = False
    line_comment = False
    block_comment = False

    while i < len(script):
        ch = script[i]
        nxt = script[i + 1] if i + 1 < len(script) else ""

        if line_comment:
            out.append(ch)
            if ch == "\n":
                line_comment = False
            i += 1
            continue
        if block_comment:
            out.append(ch)
            if ch == "*" and nxt == "/":
                out.append(nxt)
                block_comment = False
                i += 2
            else:
                i += 1
            continue
        if quote:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                quote = None
            i += 1
            continue

        if ch == "/" and nxt == "/":
            out.append(ch)
            out.append(nxt)
            line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            out.append(ch)
            out.append(nxt)
            block_comment = True
            i += 2
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            out.append(ch)
            i += 1
            continue

        if script.startswith("agent", i):
            before = script[i - 1] if i > 0 else ""
            after_index = i + len("agent")
            after = script[after_index] if after_index < len(script) else ""
            if not (before.isalnum() or before in "_$") and not (after.isalnum() or after in "_$"):
                j = after_index
                while j < len(script) and script[j].isspace():
                    j += 1
                if j < len(script) and script[j] == "(":
                    out.append(MARKER)
                    count += 1
                    i += len("agent")
                    continue

        out.append(ch)
        i += 1

    return "".join(out), count


def workflow_mode() -> str:
    mode = os.getenv("CLAUDE_CODEX_WORKFLOW_MODE", "fleet").lower()
    if mode in {"native", "gateway", "native-gateway", "native_gateway"}:
        return "native"
    if mode in {"router", "ccr", "claude-code-router", "claude_code_router"}:
        return "router"
    return "fleet"


def wrapper_source_native() -> str:
    flavor = os.getenv("CLAUDE_CODEX_FLAVOR", "codex")
    return r"""
// Injected by claude-codex: real Claude Code Workflow remains active, and
// each workflow worker lane is routed to a native Codex/DeepSeek subagent type.
const __claudeCodexFlavor = '""" + flavor + r"""'
const __claudeCodexNativeAgentType = (opts = {}) => {
  const explicit = String(opts.agentType || '')
  // Self-heal override: when DEEPSEEK_API_KEY is absent the preflight sets this
  // flag so every lane (including deepseek-routed hints) runs via codex-cli,
  // which needs no key, instead of 401-ing on claude-deepseek-pro.
  const forceCodexOnly = Boolean(globalThis.__claudeCodexForceCodexOnly)

  // NOTE: both codex AND reasonix flavors use the SAME codex-*/deepseek-* agentType
  // NAMES below. These names are just labels that must stay in sync with the
  // --agents definitions and the only-codex-fleet.py whitelist. In reasonix flavor
  // the launcher already points the codex-* / deepseek-* agent MODEL at
  // claude-reasonix-flash, so the lane runs on Reasonix while keeping the codex-*
  // label. Emitting reasonix-* names here (an agentType --agents never defines and
  // the hook never whitelists) was what broke reasonix lanes.

  if (explicit.startsWith('codex-')) return explicit
  if (explicit.startsWith('deepseek-')) return forceCodexOnly ? 'codex-worker' : explicit

  const hint = [opts.label, opts.phase, explicit].filter(Boolean).join(' ').toLowerCase()
  if (hint.includes('security')) return 'codex-security'
  if (hint.includes('verify') || hint.includes('test')) return 'codex-verify'
  if (hint.includes('review')) return 'codex-reviewer'
  if (!forceCodexOnly && (hint.includes('architecture') || hint.includes('infra') || hint.includes('devops'))) return 'deepseek-architecture'
  if (
    !forceCodexOnly && (
      hint.includes('database') ||
      hint.includes(' deep') ||
      hint.includes(':deep') ||
      hint.includes('mcp') ||
      hint.includes('extraction') ||
      hint.includes('stripe') ||
      hint.includes('signup')
    )
  ) return 'deepseek-deep'
  return 'codex-worker'
}

const __codexWorkflowAgent = async (prompt, opts = {}) => {
  const nextOpts = { ...(opts || {}) }
  nextOpts.agentType = __claudeCodexNativeAgentType(nextOpts)
  nextOpts.label = String(nextOpts.label || nextOpts.agentType)
  return agent(prompt, nextOpts)
}
""".strip()


def wrapper_source_fleet() -> str:
    return r"""
// Injected by claude-codex: real Claude Code Workflow remains active, but
// each workflow worker lane is routed through Codex Fleet.
const __codexWorkflowAgent = async (prompt, opts = {}) => {
  const originalOpts = opts || {}
  const label = String(originalOpts.label || originalOpts.phase || 'workflow-worker')
  const phaseName = originalOpts.phase ? String(originalOpts.phase) : ''
  const adapterOpts = { ...originalOpts }
  delete adapterOpts.agentType
  adapterOpts.label = 'codex:' + label

  const schemaText = originalOpts.schema
    ? '\n\nIf the original workflow requested structured output, return data matching this JSON Schema exactly after the Codex worker finishes:\n' + JSON.stringify(originalOpts.schema)
    : ''

  const codexPrompt = [
    'You are a Codex CLI worker invoked from Claude Code Dynamic Workflow.',
    'Do the actual worker-lane task below. Use repository tools as needed, respect existing user changes, and keep output concise.',
    phaseName ? 'Workflow phase: ' + phaseName : '',
    'Workflow lane label: ' + label,
    schemaText,
    'Original workflow agent prompt:\n' + String(prompt),
  ].filter(Boolean).join('\n\n')

  return agent(
    [
      'You are a thin Claude Code Workflow adapter for claude-codex.',
      'Do not solve the task yourself.',
      'Call mcp__codex_fleet__run_codex_worker exactly once with the Codex task below, wait for completion, then return the Codex result.',
      'If a schema is attached to this adapter call, shape the final answer to that schema using only facts returned by Codex.',
      'Codex task title: ' + label,
      'Codex task prompt:\n' + codexPrompt,
    ].join('\n\n'),
    adapterOpts,
  )
}
""".strip()


def rewrite_script(script: str, mode: str | None = None) -> tuple[str, int]:
    if MARKER in script:
        return script, 0
    rewritten, count = rewrite_agent_calls(script)
    if count == 0:
        return script, 0
    selected_mode = mode or workflow_mode()
    wrapper = wrapper_source_fleet() if selected_mode == "fleet" else wrapper_source_native()
    return insert_after_meta(rewritten, wrapper), count


def workflow_candidates(name: str, cwd: str | None) -> list[Path]:
    filename = name if name.endswith(".js") else f"{name}.js"
    candidates: list[Path] = []

    if cwd:
        current = Path(cwd).expanduser().resolve()
        for directory in [current, *current.parents]:
            candidates.append(directory / ".claude" / "workflows" / filename)

    candidates.append(Path.home() / ".claude" / "workflows" / filename)
    return candidates


def resolve_named_workflow(tool_input: dict, cwd: str | None) -> str | None:
    name = tool_input.get("name")
    if not isinstance(name, str) or not name:
        return None
    for candidate in workflow_candidates(name, cwd):
        try:
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8")
        except OSError:
            continue
    return None


def main() -> int:
    if os.getenv("CLAUDE_CODEX_WORKFLOW_REWRITE", "1").lower() in {"0", "false", "off", "no"}:
        return 0

    try:
        payload = json.load(sys.stdin)
    except Exception as exc:
        print(f"Codex Workflow hook got invalid JSON: {exc}", file=sys.stderr)
        return 2

    if payload.get("tool_name") != "Workflow":
        return 0

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0

    updated = dict(tool_input)
    script = updated.get("script")
    if not isinstance(script, str):
        script = resolve_named_workflow(updated, payload.get("cwd"))
        if script is None:
            return 0
        updated.pop("name", None)
        updated["script"] = script

    mode = workflow_mode()
    rewritten, count = rewrite_script(script, mode)
    if count == 0:
        return 0

    # Self-heal preflight: probe routing infra, auto-fix known issues (e.g. remap
    # deepseek lanes when DEEPSEEK_API_KEY is absent), surface the rest as context
    # so the workflow does not silently stall. Fail-open: never block on error.
    selfheal_context = ""
    if _selfheal_preflight is not None:
        try:
            rewritten, selfheal_context, _ = _selfheal_preflight(rewritten, mode)
        except Exception as exc:  # noqa: BLE001
            print(f"Codex Workflow self-heal skipped: {exc}", file=sys.stderr)

    updated["script"] = rewritten
    if mode == "fleet":
        additional_context = (
            "Workflow scripts are rewritten by claude-codex so each "
            "Workflow agent lane calls Codex Fleet instead of doing "
            "the worker task as a Claude subagent."
        )
    elif mode == "router":
        additional_context = (
            "Workflow scripts are rewritten by claude-codex so the real "
            "Claude Code Workflow/Dynamic Workflow runtime remains active, "
            "but each agent() lane runs as native Claude Code subagents. "
            "Claude Code Router routes the generated codex-* and deepseek-* "
            "native Claude Code subagent types to claude-codex-pro or "
            "claude-deepseek-pro. This hook does not auto-enable UltraCode."
        )
    else:
        additional_context = (
            "Workflow scripts are rewritten by claude-codex so each "
            "agent() lane runs as native Claude Code subagents backed by "
            "claude-codex-pro or claude-deepseek-pro through the local gateway."
        )
    if selfheal_context:
        additional_context = additional_context + "\n\n" + selfheal_context

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "updatedInput": updated,
                    "additionalContext": additional_context,
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
