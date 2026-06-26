#!/usr/bin/env python3
import json
import os
import re
import time
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from workflow_selfheal import preflight as _selfheal_preflight
except Exception:  # noqa: BLE001 - self-heal is optional; never break rewriting
    _selfheal_preflight = None


MARKER = "__reasonixWorkflowAgent"
PREFIX_GUIDE_TEXT = (
    "PROMPT-CACHE NOTE for this Dynamic Workflow: each agent() lane runs on\n"
    "DeepSeek via reasonix, where a cache MISS costs ~50x a hit. To keep lanes\n"
    "cheap, assemble each lane's prompt prefix-stable:\n"
    "1. Per-lane data scope: give a lane ONLY the data it needs (e.g. a verify\n"
    "   lane gets the ONE finding it checks, not the whole findings set). Smaller\n"
    "   unique payload = fewer missed tokens.\n"
    "2. Shared-first ordering: put content COMMON across same-role lanes (the\n"
    "   source file they all read, a fixed instruction template) at the START of\n"
    "   the lane prompt; put the lane-specific task/data LAST.\n"
    "3. Batch by shared data: when several lanes consume the same data set, give\n"
    "   them the same set in the same order so they share a cached prefix.\n"
    "4. DURABLE shared block — the #1 cache lever, measured: build ONE fixed\n"
    "   shared-context string ONCE (the full text of every file under review +\n"
    "   the common instructions), and pass that SAME string, byte-identical and\n"
    "   FIRST, into every lane's prompt — review lanes AND verify/skeptic lanes.\n"
    "   A verify lane's prompt must be: [identical shared block] + [the one short\n"
    "   finding to refute]. Do NOT let a lane re-read or re-quote a file into the\n"
    "   middle of its prompt — that injects a unique mid-prompt block that misses\n"
    "   the cache. Pass files through the shared block, never per-lane. In a real\n"
    "   run, the verify lanes that re-read files carried ~92% of all missed\n"
    "   tokens; routing those reads through one shared block lifts weighted cache\n"
    "   from ~94% to ~99%.\n"
    "5. STRICT ORDER inside each lane prompt — measured failure: a short per-lane\n"
    "   instruction (e.g. 'DIMENSION: CORRECTNESS' or a role/angle line) placed\n"
    "   BEFORE the big shared file content splits the prefix at ~5KB, so the whole\n"
    "   file (the largest shared block) lands AFTER the divergence and misses on\n"
    "   every lane. Order EXACTLY: (a) shared instruction template, (b) the full\n"
    "   shared file/source text, BYTE-IDENTICAL across lanes, (c) THEN the one\n"
    "   short lane-specific line (the dimension / finding / angle) LAST. The big\n"
    "   shared block must come before anything that differs between lanes.\n"
    "6. DECOMPOSE FINELY — the cost/speed/quality lever that dwarfs the rest. Each\n"
    "   agent() lane is DeepSeek-flash and CANNOT spawn its own sub-lanes, so a big\n"
    "   vague lane crams everything into one context (measured failure: one lane\n"
    "   read 833 files -> 532K tokens -> 75% cache, 18 min). One lane = one file /\n"
    "   one function / one module / one focused question — something done by reading\n"
    "   a HANDFUL of files, not a directory. If a lane's prompt would make it read\n"
    "   10+ files, that is 10+ lanes via parallel(). A workflow with only 2-5 lanes\n"
    "   is almost always under-decomposed: find the big lanes and split them. This\n"
    "   machine runs ~14 lanes at once — use them. More small lanes beats fewer big\n"
    "   lanes on cost AND speed AND quality. Then ONE synthesize lane merges the\n"
    "   short per-lane summaries.\n"
    "7. READ LANES RETURN SUMMARIES, NOT RAW FILES — the measured #1 lever against\n"
    "   the 500K-token / 75% cache collapse (this is exactly how Claude Code reaches\n"
    "   98%: Explore/Plan subagents read files in isolation and hand back a SHORT\n"
    "   summary, so the main agent never ingests raw file dumps). Make each read\n"
    "   lane's schema/output a CONDENSED summary (a few bullet points / a small\n"
    "   structured object), NOT the file contents. A downstream lane should consume\n"
    "   the previous lane's SUMMARY, never re-read the same file or paste its body\n"
    "   into the next prompt. Raw file bytes are unique-content that can never\n"
    "   cache-hit and that bloat every lane that carries them — keep them inside the\n"
    "   one lane that read the file and out of every other lane.\n"
    "8. WARM-UP LANE FOR SHARED-PREFIX REVIEW — when many lanes share one big\n"
    "   byte-identical prefix (a review/audit over the same file set), dispatch ONE\n"
    "   lane FIRST and await it, THEN fan out the rest. The first lane seeds DeepSeek's\n"
    "   prefix cache so every later lane hits it warm. Measured: a cold 16-lane review\n"
    "   burst weighted 98.4% (one cold primer dragging it), but with a warm-up lane\n"
    "   first the same burst hit 99.66% (all lanes >=99.1%). Only worth it for the\n"
    "   shared-prefix shape — unique-content lanes share nothing to warm. VERIFY the\n"
    "   warm-up lane actually returned a real (non-empty) result before fanning out:\n"
    "   if the single warm-up lane flakes (empty/error), the prefix is NOT seeded and\n"
    "   the whole burst goes cold (measured: a failed warm-up dropped a burst to\n"
    "   ~94.9%). Retry the warm-up until it lands a real reply.\n"
    "9. CROSS-WORKFLOW CACHE ACCUMULATION — when you run MANY workflows on the SAME\n"
    "   codebase/dataset from DIFFERENT angles (the long-haul pattern), cache builds\n"
    "   ACROSS workflows so later ones get cheaper, IF every lane of every workflow\n"
    "   puts the SAME byte-identical shared block (the codebase/files/context) FIRST\n"
    "   from token 0, and the per-lane angle/question LAST. Measured: codebase-first\n"
    "   accumulates 96.96%->97.57%->99.60% over 3 same-codebase workflows; putting the\n"
    "   angle BEFORE the codebase breaks the byte-prefix and flat-lines at ~78% with NO\n"
    "   accumulation. DeepSeek persists a common prefix only AFTER 1-2 requests pay full\n"
    "   price, then every later same-prefix request hits it. So: build the shared block\n"
    "   ONCE, reuse the EXACT bytes across all workflows, never let any per-workflow or\n"
    "   per-lane text precede it, and run same-codebase workflows back-to-back (long\n"
    "   idle gaps let DeepSeek evict the warm prefix; the win is best-effort high-90s,\n"
    "   not a guaranteed climb to 99.2). Switching to a DIFFERENT codebase resets the\n"
    "   accumulation — group same-context work together.\n"
    "10. VERIFY-FAIL IS NOT REJECTION — a verify/check lane that returns empty, errors, "
    "or carries a 'LANE_UNVERIFIED:' marker means the lane COULD NOT verify (e.g. timed "
    "out), NOT that the finding is false. Default to KEEPING such a finding marked "
    "'unverified'; never move it to a 'rejected' bucket on an empty/failed verdict. In "
    "code: treat `!verdict?.confirmed` as rejected ONLY when the verdict actually came "
    "back with confirmed:false — an absent/empty verdict is UNVERIFIED.\n"
    "11. HARD-TASK HARNESS — when a lane must EDIT code and pass tests (a real refactor/"
    "fix, not a read), make each lane a COMPLETE sub-task (it drafts + edits + verifies, "
    "not just drafts), and hand it an INSTANCE-LEVEL spec: a 1-2 sentence plan + the EXACT "
    "files it touches + one line `ACCEPTANCE_TEST: <shell command>` (e.g. "
    "`ACCEPTANCE_TEST: bun test path/x.test.ts`). The lane harness runs that command, and "
    "on failure makes the lane retry with a short lesson until the test passes or it "
    "stalls (then it returns LANE_ESCALATE for you to take over). Do NOT dump repo "
    "structure, file summaries, or few-shot examples into a lane — measured to HURT a weak "
    "executor (instance-level plan+files+test is what helps). Review the SHORT lane results "
    "(LANE_OK / LANE_ESCALATE); only take over the LANE_ESCALATE lanes yourself.\n"
    "This is advisory — correctness first; apply where it doesn't distort the work."
)


# === Lever E — SPECULATIVE CONTEXT PREFETCH (advisory mode first, Q7) ==========
# Concept: from the workflow script/task text, PREDICT which real files the fan-out
# lanes will read. Owner Q7: ship ADVISORY mode FIRST — predict + LOG a precision
# metric (predicted ∩ actually-read / predicted), with ZERO prompt/prefix change
# (no injection). Only a future 'inject' mode would place file summaries in the
# shared prefix; that is NOT this task. Advisory is pure measurement: does the
# prediction work well enough to justify inject later?
#
# HARD INVARIANT: advisory (and the inject stub) MUST NOT alter a single prompt or
# prefix byte. predict_prefetch_files only READS the task text; the advisory log is
# emitted to a side channel (stderr + a jsonl ledger), never into updatedInput or
# additionalContext. Zero cache risk.
_PREFETCH_MAX_FILES = 8
_PREFETCH_FILE_CAP_BYTES = 32768  # reserved for a future inject mode (summary cap)
_PREFETCH_TIMEOUT = 20            # reserved for a future inject summarize budget

# A path/filename token: an optional dir prefix + a basename with a known code/doc
# extension. Deliberately conservative — only tokens that LOOK like real files are
# considered, then each is confirmed to EXIST under cwd before being predicted.
_PREFETCH_PATH_RE = re.compile(
    r"""(?<![\w./-])             # not mid-token
        (                        # capture the path
          (?:[\w./-]+/)?         # optional dir segments
          [\w-]+                 # stem
          \.(?:py|pyx|pyi|js|mjs|cjs|ts|tsx|jsx|md|json|jsonl|sh|bash|zsh|
              txt|toml|yaml|yml|cfg|ini|rs|go|java|c|h|cpp|hpp|rb|php|sql|html|css)
        )
        (?![\w/])                # not followed by more path chars
    """,
    re.VERBOSE,
)


def predict_prefetch_files(task_text: str, cwd: str | None) -> list[str]:
    """Predict the files a workflow's lanes will read, from the task/script text.

    Returns a BOUNDED (<= _PREFETCH_MAX_FILES) de-duplicated list of ABSOLUTE paths
    that ACTUALLY EXIST under cwd. Pure regex over the text + a filesystem-exists
    check — NO grep-symbol fallback (Q7). A token that does not resolve to a real
    file under cwd is dropped, so a task naming no real file returns [].

    This function NEVER mutates the prompt; the caller logs its output to a side
    channel in advisory mode.
    """
    if not task_text or not cwd:
        return []
    try:
        base = Path(cwd).expanduser().resolve()
    except Exception:  # noqa: BLE001
        return []

    seen: set[str] = set()
    out: list[str] = []
    for match in _PREFETCH_PATH_RE.finditer(task_text):
        token = match.group(1)
        # Resolve relative to cwd; also try the bare basename anywhere is NOT done —
        # we only honor the path as written (relative-to-cwd or already absolute),
        # so the prediction is grounded in the literal reference.
        candidate = (base / token) if not os.path.isabs(token) else Path(token)
        try:
            resolved = candidate.expanduser().resolve()
        except Exception:  # noqa: BLE001
            continue
        if not resolved.is_file():
            continue
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
        if len(out) >= _PREFETCH_MAX_FILES:
            break
    return out


def prefetch_mode() -> str:
    """off | advisory | inject. Default off (Q7: advisory ships first, then off
    is the safe default until precision justifies promotion)."""
    mode = os.getenv("CLAUDE_REASONIX_PREFETCH_CONTEXT", "off").strip().lower()
    if mode in {"advisory", "inject"}:
        return mode
    return "off"


def _prefetch_log_path() -> Path:
    return Path(__file__).resolve().parent.parent / "runtime" / "prefetch-advisory.jsonl"


def run_prefetch_advisory(script: str, cwd: str | None) -> list[str]:
    """ADVISORY MODE side effect only — predict the files the lanes will read and
    LOG them. Returns the predicted list (for callers/tests). Emits NOTHING into the
    prompt: it writes to stderr + a jsonl ledger so a later step can compute precision
    (predicted ∩ actually-read / predicted) against the files the lanes really read.

    'inject' mode is a documented STUB this task: it predicts + logs exactly like
    advisory but does NOT place anything in the shared prefix (that is a future task).
    """
    mode = prefetch_mode()
    if mode == "off":
        return []
    predicted = predict_prefetch_files(script, cwd)
    record = {
        "ts": time.time(),
        "mode": mode,
        "cwd": cwd,
        "predicted": predicted,
        "predicted_count": len(predicted),
        # inject is a stub: flag that no prefix change was made.
        "injected": False,
    }
    line = json.dumps(record, ensure_ascii=False)
    # stderr (visible in hook logs) — never stdout, which carries the hook protocol.
    print(f"reasonix-prefetch[{mode}]: predicted {len(predicted)} files: {predicted}",
          file=sys.stderr)
    try:
        log = _prefetch_log_path()
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception as exc:  # noqa: BLE001 - logging must never break the hook
        print(f"reasonix-prefetch log skipped: {exc}", file=sys.stderr)
    return predicted


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
    mode = os.getenv("CLAUDE_REASONIX_WORKFLOW_MODE", os.getenv("CLAUDE_CODEX_WORKFLOW_MODE", "fleet")).lower()
    if mode in {"native", "gateway", "native-gateway", "native_gateway"}:
        return "native"
    return "fleet"


def wrapper_source_native() -> str:
    flavor = os.getenv("CLAUDE_REASONIX_FLAVOR", os.getenv("CLAUDE_CODEX_FLAVOR", "reasonix"))
    return r"""
// Injected by claude-reasonix: real Claude Code Workflow remains active, and
// each workflow worker lane is routed to a native Reasonix/DeepSeek subagent type.
const __claudeReasonixFlavor = '""" + flavor + r"""'
const __claudeReasonixNativeAgentType = (opts = {}) => {
  const explicit = String(opts.agentType || '')
  // Self-heal override: when DEEPSEEK_API_KEY is absent the preflight sets this
  // flag so every lane (including deepseek-routed hints) runs via reasonix-cli,
  // which needs no key, instead of 401-ing on claude-deepseek-pro.
  const forceReasonixOnly = Boolean(globalThis.__claudeReasonixForceReasonixOnly)

  // NOTE: these agentType NAMES are just labels that must stay in sync with the
  // --agents definitions (launcher) and the only-reasonix-fleet.py whitelist. The
  // launcher points each reasonix-* agent MODEL at claude-reasonix-flash, so the
  // lane runs on Reasonix. Emitting a name --agents never defines / the hook never
  // whitelists breaks the lane — keep all three sites byte-identical.
  // Back-compat: a caller may still pass an explicit legacy agentType name
  // (a session whose launcher predates this rename); pass it through.

  if (explicit.startsWith('reasonix-')) return explicit
  if (explicit.startsWith('codex-')) return explicit
  if (explicit.startsWith('deepseek-')) return forceReasonixOnly ? 'reasonix-worker' : explicit

  const hint = [opts.label, opts.phase, explicit].filter(Boolean).join(' ').toLowerCase()
  if (hint.includes('security')) return 'reasonix-security'
  if (hint.includes('verify') || hint.includes('test')) return 'reasonix-verify'
  if (hint.includes('review')) return 'reasonix-reviewer'
  // architecture/infra lanes fold into the reviewer (weighs system boundaries);
  // deep/database/mcp lanes fold into the general worker. The dedicated
  // deepseek-architecture / deepseek-deep agentTypes were dropped. Under the
  // force-only sentinel (a degraded session), collapse role-inference to the
  // plain worker so every lane takes the simplest keyless route.
  if (!forceReasonixOnly && (hint.includes('architecture') || hint.includes('infra') || hint.includes('devops'))) return 'reasonix-reviewer'
  return 'reasonix-worker'
}

const __reasonixWorkflowAgent = async (prompt, opts = {}) => {
  const nextOpts = { ...(opts || {}) }
  nextOpts.agentType = __claudeReasonixNativeAgentType(nextOpts)
  nextOpts.label = String(nextOpts.label || nextOpts.agentType)
  return agent(prompt, nextOpts)
}
""".strip()


def wrapper_source_fleet() -> str:
    return r"""
// Injected by claude-reasonix: real Claude Code Workflow remains active, but
// each workflow worker lane is routed through Reasonix Fleet.
const __reasonixWorkflowAgent = async (prompt, opts = {}) => {
  const originalOpts = opts || {}
  const label = String(originalOpts.label || originalOpts.phase || 'workflow-worker')
  const phaseName = originalOpts.phase ? String(originalOpts.phase) : ''
  const adapterOpts = { ...originalOpts }
  delete adapterOpts.agentType
  adapterOpts.label = 'reasonix:' + label

  const schemaText = originalOpts.schema
    ? '\n\nIf the original workflow requested structured output, return data matching this JSON Schema exactly after the Reasonix worker finishes:\n' + JSON.stringify(originalOpts.schema)
    : ''

  const reasonixPrompt = [
    'You are a Reasonix CLI worker invoked from Claude Code Dynamic Workflow.',
    'Do the actual worker-lane task below. Use repository tools as needed, respect existing user changes, and keep output concise.',
    phaseName ? 'Workflow phase: ' + phaseName : '',
    'Workflow lane label: ' + label,
    schemaText,
    'Original workflow agent prompt:\n' + String(prompt),
  ].filter(Boolean).join('\n\n')

  return agent(
    [
      'You are a thin Claude Code Workflow adapter for claude-reasonix.',
      'Do not solve the task yourself.',
      'Call mcp__reasonix_fleet__run_reasonix_worker exactly once with the Reasonix task below, wait for completion, then return the Reasonix result.',
      'If a schema is attached to this adapter call, shape the final answer to that schema using only facts returned by Reasonix.',
      'Reasonix task title: ' + label,
      'Reasonix task prompt:\n' + reasonixPrompt,
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
    if os.getenv("CLAUDE_REASONIX_WORKFLOW_REWRITE", os.getenv("CLAUDE_CODEX_WORKFLOW_REWRITE", "1")).lower() in {"0", "false", "off", "no"}:
        return 0

    try:
        payload = json.load(sys.stdin)
    except Exception as exc:
        print(f"Reasonix Workflow hook got invalid JSON: {exc}", file=sys.stderr)
        return 2

    if payload.get("tool_name") != "Workflow":
        return 0

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0

    updated = dict(tool_input)
    script = updated.get("script")
    if not isinstance(script, str):
        # A workflow can be passed three ways: inline `script`, a saved `name`, or
        # a `scriptPath` file. Without handling scriptPath, a skill-provided script
        # file (e.g. qwen-workflow-research's qwen-research.js) is NOT rewritten, so
        # its bare agent() lanes default to the MAIN model (Opus) instead of the
        # reasonix subagent — they then fail/throttle. Read the file and inline it
        # so the same rewrite applies; drop scriptPath so the runtime uses our script.
        script_path = updated.get("scriptPath")
        if isinstance(script_path, str) and script_path:
            try:
                with open(os.path.expanduser(script_path), "r", encoding="utf-8") as _sf:
                    script = _sf.read()
            except Exception as exc:  # noqa: BLE001
                print(f"Reasonix Workflow hook could not read scriptPath {script_path}: {exc}", file=sys.stderr)
                return 0
            updated.pop("scriptPath", None)
            updated["script"] = script
        else:
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
            print(f"Reasonix Workflow self-heal skipped: {exc}", file=sys.stderr)

    updated["script"] = rewritten
    if mode == "fleet":
        additional_context = (
            "Workflow scripts are rewritten by claude-reasonix so each "
            "Workflow agent lane calls Reasonix Fleet instead of doing "
            "the worker task as a Claude subagent."
        )
    else:
        additional_context = (
            "Workflow scripts are rewritten by claude-reasonix so each "
            "agent() lane runs as native Claude Code subagents backed by "
            "claude-reasonix-flash through the local gateway."
        )
    if selfheal_context:
        additional_context = additional_context + "\n\n" + selfheal_context
    if os.getenv("CLAUDE_REASONIX_WORKFLOW_PREFIX_GUIDE", os.getenv("CLAUDE_CODEX_WORKFLOW_PREFIX_GUIDE", "1")).lower() in {"1", "true", "yes", "on"}:
        additional_context = additional_context + "\n\n" + PREFIX_GUIDE_TEXT

    # Lever E — speculative context prefetch, ADVISORY MODE (Q7). Predict + LOG the
    # files the lanes will read; ZERO prompt change. This runs AFTER updated and
    # additional_context are fully assembled and MUST NOT mutate either — it only
    # writes to a side channel (stderr + jsonl) so precision can be measured later.
    # Fail-open: a prediction/log error never breaks the hook output.
    try:
        run_prefetch_advisory(rewritten, payload.get("cwd"))
    except Exception as exc:  # noqa: BLE001
        print(f"reasonix-prefetch advisory skipped: {exc}", file=sys.stderr)

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
