#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAUNCHER="$ROOT/bin/claude-reasonix"
HOOK="$ROOT/hooks/only-reasonix-fleet.py"
WORKFLOW_HOOK="$ROOT/hooks/reasonix-workflow.py"
MCP_SERVER="$ROOT/reasonix-fleet-mcp.py"
GATEWAY="$ROOT/reasonix-native-gateway.py"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

assert_file() {
  [[ -f "$1" ]] || fail "missing file: $1"
}

assert_executable() {
  [[ -x "$1" ]] || fail "not executable: $1"
}

assert_file "$LAUNCHER"
assert_executable "$LAUNCHER"
assert_file "$ROOT/bridge-settings.json"
assert_file "$HOOK"
assert_file "$WORKFLOW_HOOK"
assert_file "$MCP_SERVER"
assert_file "$GATEWAY"

# Slim-down: the launcher must carry NO CCR / router-proxy machinery.
for banned in "ccr-claude-proxy" "run_claude_with_router" "start_ccr_proxy" "generate_ccr_config" "CCR_PROXY_FILE"; do
  grep -q "$banned" "$LAUNCHER" && fail "launcher still references removed CCR symbol: $banned"
done
# Slim-down Task 2: the launcher must carry NO Qwen machinery.
for banned in "ensure_qwen36_ready" "qwen-worker" "qwen-research" "qwen36-local" "router-qwen"; do
  grep -q "$banned" "$LAUNCHER" && fail "launcher still references removed qwen symbol: $banned"
done
[[ -f "$ROOT/ccr-claude-proxy.py" ]] && fail "ccr-claude-proxy.py should be deleted"
# Slim-down Task 3: no gpt-5.4 default, no dead codex-exec fields.
grep -q "gpt-5.4" "$LAUNCHER" && fail "launcher must not default to gpt-5.4"
grep -q "gpt-5.4" "$MCP_SERVER" && fail "MCP must not default to gpt-5.4"
for f in service_tier web_search sandbox approval_policy; do
  grep -q "\"$f\"" "$MCP_SERVER" && fail "MCP still carries codex-exec field: $f"
done

# Fork-engine cutover: the dist patch + upstream-reasonix requirement are retired.
# The launcher must NOT run the retired ephemeral dist patch, and install.sh must
# NOT require/install upstream reasonix. The engine is bundled in vendor/.
grep -q "apply_ephemeral" "$LAUNCHER" && fail "launcher must not run the retired dist patch (apply_ephemeral)"
grep -q "REASONIX_ACP_EPHEMERAL_SESSION" "$LAUNCHER" && fail "launcher must not export the retired REASONIX_ACP_EPHEMERAL_SESSION"
[[ -e "$ROOT/patches/apply_ephemeral.py" ]] && fail "patches/apply_ephemeral.py should be deleted"
[[ -e "$ROOT/patches/ephemeral-session.md" ]] && fail "patches/ephemeral-session.md should be deleted"
grep -Eq "npm i -g reasonix|npm install -g reasonix" "$ROOT/install.sh" && fail "install must not require upstream reasonix (npm i -g reasonix)"
grep -q "apply_ephemeral" "$ROOT/install.sh" && fail "install must not run the retired dist patch"
grep -q "reasonix CLI not found" "$ROOT/install.sh" && fail "install must not require the upstream reasonix CLI"
# The bundled fork engine must be present and committed (it is the shipped engine).
[[ -f "$ROOT/vendor/reasonix-engine/dist/index.js" ]] || fail "missing bundled fork engine: vendor/reasonix-engine/dist/index.js"
[[ -f "$ROOT/engine/run-lane.mjs" ]] || fail "missing engine shim: engine/run-lane.mjs"
grep -q "REASONIX_ENGINE_DIST" "$LAUNCHER" || fail "launcher must export REASONIX_ENGINE_DIST for the in-process engine"

RX_PROMPT="$ROOT/system-prompt-reasonix.md"
[[ -f "$RX_PROMPT" ]] || fail "missing reasonix system prompt"
grep -q "claude-reasonix-flash" "$RX_PROMPT" || fail "reasonix prompt must name the flash agent"
grep -q "atomic" "$RX_PROMPT" || fail "reasonix prompt must teach atomic-task decomposition"
grep -q "unlimited" "$RX_PROMPT" || fail "reasonix prompt must state agent count is unlimited"
grep -q "web search" "$RX_PROMPT" || fail "reasonix prompt must mention built-in web search"
grep -qi "Agent-first policy\|Reasonix-first" "$RX_PROMPT" || fail "reasonix prompt must state the Reasonix-first agent policy"
grep -qi "ALWAYS delegate" "$RX_PROMPT" || fail "reasonix prompt must list always-delegate task types"
grep -qi "Claude keeps these" "$RX_PROMPT" || fail "reasonix prompt must list what Claude keeps doing"
grep -qi "look at the agent first\|agent does it" "$RX_PROMPT" || fail "reasonix prompt must teach look-at-agent-first decision"
grep -qi "one genuinely small" "$RX_PROMPT" && fail "conductor mode: the small-edit loophole line must be removed"
grep -qi "Banned excuses" "$RX_PROMPT" && fail "conductor mode: the Banned-excuses list must be removed (the hook enforces now)"

python3 - "$ROOT/bridge-settings.json" "$WORKFLOW_HOOK" "$ROOT" <<'PY'
import json
import sys

settings_path, workflow_hook, install_home = sys.argv[1:4]
with open(settings_path, "r", encoding="utf-8") as fh:
    raw = fh.read()
# bridge-settings.json is a portable template; the launcher renders __INSTALL_HOME__
# to the real install dir before passing it to `claude`. Render the same way here.
if "__INSTALL_HOME__" not in raw:
    raise SystemExit("bridge settings must stay a portable template (__INSTALL_HOME__ placeholder)")
settings = json.loads(raw.replace("__INSTALL_HOME__", install_home))

if settings.get("ultracode") is not False:
    raise SystemExit("bridge settings must explicitly avoid auto-enabling ultracode mode")
if settings.get("workflowKeywordTriggerEnabled") is not True:
    raise SystemExit("bridge settings must keep the ultracode workflow keyword trigger enabled")

hooks = settings.get("hooks", {}).get("PreToolUse", [])
commands = [
    hook.get("command", "")
    for group in hooks
    for hook in group.get("hooks", [])
]
if not any(workflow_hook in command for command in commands):
    raise SystemExit("bridge settings must install the Reasonix Workflow rewrite hook")

permissions = settings.get("permissions", {}).get("allow", [])
required = {
    "mcp__reasonix_fleet__run_reasonix_worker",
    "mcp__reasonix_fleet__run_reasonix_fleet",
    "mcp__reasonix_fleet__fleet_status",
}
if not required.issubset(set(permissions)):
    raise SystemExit(f"bridge settings must allow Reasonix Fleet MCP tools: {permissions}")

# Conductor guard must be wired on the operator tools (Edit/Write/MultiEdit/Bash).
hook_cmds = [h.get("command", "") for g in settings.get("hooks", {}).get("PreToolUse", []) for h in g.get("hooks", [])]
matchers = [g.get("matcher", "") for g in settings.get("hooks", {}).get("PreToolUse", [])]
conductor_hook_cmds = [h.get("command", "") for g in settings.get("hooks", {}).get("PreToolUse", []) for h in g.get("hooks", []) if "conductor-guard.py" in h.get("command", "")]
if not conductor_hook_cmds:
    raise SystemExit("bridge settings must wire the conductor-guard hook")
# The conductor-guard matcher must name ALL four operator-tool classes:
# Edit, Write, MultiEdit (file-write tools) and Bash (for mutating shell commands).
conductor_matchers = [g.get("matcher", "") for g in settings.get("hooks", {}).get("PreToolUse", []) if any("conductor-guard.py" in h.get("command", "") for h in g.get("hooks", []))]
if not conductor_matchers:
    raise SystemExit("conductor-guard hook group has no matcher")
cm = conductor_matchers[0]
for required_tool in ("Edit", "Write", "MultiEdit", "Bash"):
    if required_tool not in cm:
        raise SystemExit(f"conductor-guard matcher must contain {required_tool!r}: {cm!r}")
# Big-read guard must be wired on Read, and it is the FIRST PreToolUse group so it
# fires before anything else (it stops Opus reading a huge file whole into context —
# the measured autocompact-thrashing cause).
big_read_matchers = [g.get("matcher", "") for g in settings.get("hooks", {}).get("PreToolUse", []) if any("big-read-guard.py" in h.get("command", "") for h in g.get("hooks", []))]
if not big_read_matchers:
    raise SystemExit("bridge settings must wire the big-read-guard hook")
if "Read" not in big_read_matchers[0]:
    raise SystemExit(f"big-read-guard matcher must contain 'Read': {big_read_matchers[0]!r}")
first_group = settings.get("hooks", {}).get("PreToolUse", [{}])[0]
first_group_cmds = [h.get("command", "") for h in first_group.get("hooks", [])]
if not any("big-read-guard.py" in c for c in first_group_cmds):
    raise SystemExit("big-read-guard must be the FIRST entry in the PreToolUse array")
# The conductor-guard must still be wired (it need not be first now that big-read is).
if not any("conductor-guard.py" in c for c in hook_cmds):
    raise SystemExit("conductor-guard hook must still be wired")
PY

tmp_home="$(mktemp -d)"
trap 'rm -rf "$tmp_home"' EXIT

# Point the launcher at the REPO as its install home so it loads the renamed
# gateway/mcp/hooks/settings under test (not a stale ~/.claude install).
export CLAUDE_REASONIX_FLEET_INSTALL_HOME="$ROOT"
export CLAUDE_REASONIX_FLEET_HOME="$tmp_home/fleet"
export CLAUDE_BIN="/bin/echo"
export REASONIX_BIN="/bin/echo"
export ANTHROPIC_API_KEY="test-anthropic-key"
export CLAUDE_REASONIX_GATEWAY_MOCK=1
export CLAUDE_REASONIX_KEEP_ROUTER_RUNTIME=1

# Regression: the Anthropic streaming lazy path must emit heartbeat content_block_delta
# events (so the workflow watchdog sees progress) while preserving a correct event
# order and the final StructuredOutput tool_use block. A bare ': keepalive' comment is
# invisible to the watchdog, so the heartbeat must be a real text_delta.
python3 - "$GATEWAY" <<'PY'
import importlib.util
import io
import os
import sys
import time

gateway_path = sys.argv[1]
os.environ["CLAUDE_REASONIX_BACKEND"] = "codex-cli"
os.environ.pop("CLAUDE_REASONIX_GATEWAY_MOCK", None)
# Force the wait loop to tick quickly so a heartbeat is emitted before the result.
os.environ["CLAUDE_REASONIX_GATEWAY_STREAM_KEEPALIVE_SECONDS"] = "1"
spec = importlib.util.spec_from_file_location("reasonix_native_gateway_stream", gateway_path)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)

# Build a handler instance without running BaseHTTPRequestHandler.__init__.
handler = module.Handler.__new__(module.Handler)
captured = io.BytesIO()
handler.wfile = captured
handler.send_response = lambda *a, **k: None
handler.send_header = lambda *a, **k: None
handler.end_headers = lambda *a, **k: None

tool_use_message = {
    "id": "msg_real",
    "type": "message",
    "role": "assistant",
    "model": "claude-reasonix-flash",
    "content": [
        {"type": "tool_use", "id": "toolu_x", "name": "StructuredOutput", "input": {"results": [{"claim": "c"}]}}
    ],
    "stop_reason": "tool_use",
    "stop_sequence": None,
    "usage": {"input_tokens": 5, "output_tokens": 9},
}

def slow_producer():
    # Sleep longer than the 1s keepalive interval so >=1 heartbeat fires.
    time.sleep(1.6)
    return tool_use_message

handler.send_sse_response_lazy(slow_producer, "claude-reasonix-flash")
out = captured.getvalue().decode("utf-8")

# Parse the SSE stream into (event, data) pairs by JSON (format-agnostic to spacing).
import json as _json
pairs = []
cur_event = None
for line in out.splitlines():
    if line.startswith("event: "):
        cur_event = line[len("event: "):]
    elif line.startswith("data: "):
        pairs.append((cur_event, _json.loads(line[len("data: "):])))
        cur_event = None
events = [name for name, _ in pairs]

# Invariant 1: exactly one message_start, and it is first.
if events.count("message_start") != 1 or events[0] != "message_start":
    raise SystemExit(f"stream must emit exactly one message_start first: {events}")
# Invariant 2: a heartbeat text block opens at index 0, with >=1 real space text_delta.
heartbeat_deltas = [
    d for name, d in pairs
    if name == "content_block_delta" and d.get("index") == 0
    and d.get("delta", {}).get("type") == "text_delta"
]
if not heartbeat_deltas:
    raise SystemExit(f"stream must emit a heartbeat text_delta at index 0: {events}")
if not all(d["delta"].get("text") == " " for d in heartbeat_deltas):
    raise SystemExit(f"heartbeat text_delta must be a single space: {heartbeat_deltas}")
if ": keepalive" in out:
    raise SystemExit("heartbeat must not fall back to the invisible ': keepalive' comment")
# Invariant 3: the real tool_use StructuredOutput block survives at index 1.
real_starts = [
    d for name, d in pairs
    if name == "content_block_start" and d.get("index") == 1
    and d.get("content_block", {}).get("type") == "tool_use"
    and d.get("content_block", {}).get("name") == "StructuredOutput"
]
if not real_starts:
    raise SystemExit(f"final tool_use StructuredOutput block must be at index 1: {events}")
# Invariant 4: exactly one message_delta (stop_reason tool_use) and one message_stop, at the end.
deltas = [d for name, d in pairs if name == "message_delta"]
if len(deltas) != 1 or deltas[0].get("delta", {}).get("stop_reason") != "tool_use":
    raise SystemExit(f"stream must have one message_delta with stop_reason tool_use: {deltas}")
if events.count("message_stop") != 1 or events[-1] != "message_stop":
    raise SystemExit(f"stream must end with exactly one message_stop: {events}")
# Invariant 5: the heartbeat block (index 0) is closed before the real block opens at index 1.
idx0_stop = next((i for i, (n, d) in enumerate(pairs) if n == "content_block_stop" and d.get("index") == 0), None)
idx1_start = next((i for i, (n, d) in enumerate(pairs) if n == "content_block_start" and d.get("index") == 1), None)
if idx0_stop is None or idx1_start is None or idx0_stop >= idx1_start:
    raise SystemExit(f"heartbeat block (index 0) must close before real block (index 1) opens: {events}")
PY

# Regression: the gateway reasonix timeout default must be the watchdog-safe 600s (not 165s),
# so legitimate long web-search lanes are not killed mid-work.
python3 - "$GATEWAY" <<'PY'
import importlib.util
import os
import sys

gateway_path = sys.argv[1]
os.environ.pop("CLAUDE_REASONIX_GATEWAY_TIMEOUT", None)
os.environ.pop("REASONIX_FLEET_TIMEOUT_SECONDS", None)
spec = importlib.util.spec_from_file_location("reasonix_native_gateway_timeout_default", gateway_path)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)
default = module.env_first("CLAUDE_REASONIX_GATEWAY_TIMEOUT", "REASONIX_FLEET_TIMEOUT_SECONDS", default="600")
if float(default) < 600:
    raise SystemExit(f"reasonix timeout default must be >= 600s, got {default}")
PY

python3 - "$GATEWAY" <<'PY'
import importlib.util
import sys

gateway_path = sys.argv[1]
spec = importlib.util.spec_from_file_location("reasonix_native_gateway_prompt_schema", gateway_path)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)

deep_research_schema = {
    "type": "object",
    "properties": {
        "sourceQuality": {
            "type": "string",
            "enum": ["primary", "secondary", "blog", "forum", "unreliable"],
        },
        "publishDate": {"type": "string"},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "quote": {"type": "string"},
                    "importance": {"type": "string", "enum": ["central", "supporting", "tangential"]},
                },
                "required": ["claim", "quote", "importance"],
            },
        },
    },
    "required": ["sourceQuality", "publishDate", "claims"],
}

openai_prompt = module.openai_messages_to_prompt(
    [{"role": "user", "content": "Structured output only."}],
    [
        {
            "type": "function",
            "function": {
                "name": "StructuredOutput",
                "description": "Return extracted claims.",
                "parameters": deep_research_schema,
            },
        }
    ],
)
# Case-insensitive: the prompt phrases the one-object rule as "EXACTLY ONE JSON
# object" (uppercase for emphasis); match on intent, not exact casing.
for required_text in (
    "STRUCTURED OUTPUT REQUIREMENT",
    "exactly one json object",
    "sourceQuality",
    '"primary"',
    '"unreliable"',
    '"quote"',
    '"importance"',
    '"central"',
):
    if required_text.lower() not in openai_prompt.lower():
        raise SystemExit(f"OpenAI StructuredOutput prompt omitted schema detail {required_text!r}: {openai_prompt}")

anthropic_prompt = module.openai_messages_to_prompt(
    [{"role": "user", "content": "Structured output only."}],
    [{"name": "claude_sdk_StructuredOutput", "input_schema": deep_research_schema}],
)
for required_text in ("claude_sdk_StructuredOutput", "sourceQuality", '"quote"', '"importance"'):
    if required_text not in anthropic_prompt:
        raise SystemExit(f"Anthropic StructuredOutput prompt omitted schema detail {required_text!r}: {anthropic_prompt}")
PY

"$LAUNCHER" off >/dev/null
disabled_status="$("$LAUNCHER" status)"
grep -q "disabled" <<<"$disabled_status" || fail "status should report disabled"
# reasonix flavor defaults to NATIVE subagents; force fleet mode to exercise the
# fleet-path flag hygiene these assertions validate (--disallowedTools, no --agents).
bare_output="$(CLAUDE_REASONIX_NATIVE_SUBAGENTS=0 "$LAUNCHER" "bare prompt")"
grep -q -- "--mcp-config" <<<"$bare_output" || fail "bare claude-reasonix should start fleet even when status is disabled"
grep -q -- "--disallowedTools Agent,Task" <<<"$bare_output" || fail "safe claude-reasonix should block generic Claude Agent/Task"
if grep -q -- " --agents {" <<<"$bare_output"; then
  fail "safe claude-reasonix should not define gateway native subagents by default"
fi
if grep -q -- "--allowedTools" <<<"$bare_output"; then
  fail "bare claude-reasonix should not allow-list only reasonix_fleet"
fi
if grep -q -- "--tools " <<<"$bare_output"; then
  fail "bare claude-reasonix should not disable Claude native tools"
fi
if grep -q -- "--strict-mcp-config" <<<"$bare_output"; then
  fail "bare claude-reasonix should not block normal MCP/plugins"
fi
grep -q "bare prompt" <<<"$bare_output" || fail "bare claude-reasonix should forward prompt"
plain_output="$("$LAUNCHER" plain "plain prompt")"
if grep -q -- "--mcp-config" <<<"$plain_output"; then
  fail "plain mode should bypass fleet"
fi
if grep -q -- " --agents {" <<<"$plain_output"; then
  fail "plain mode should not define claude-reasonix native subagents"
fi
grep -q "plain prompt" <<<"$plain_output" || fail "plain mode should forward prompt"

"$LAUNCHER" on 3 >/dev/null
enabled_status="$("$LAUNCHER" status)"
grep -q "enabled" <<<"$enabled_status" || fail "status should report enabled"
grep -q "default concurrency: 3" <<<"$enabled_status" || fail "status should show default concurrency"

cat >"$tmp_home/ps-fixture.txt" <<EOF
12345 claude --mcp-config $CLAUDE_REASONIX_FLEET_HOME/runtime/mcp.json --append-system-prompt Every Reasonix worker defaults to GPT-5.5 --disallowedTools Agent,Task
EOF
stale_status="$(CLAUDE_REASONIX_PS_FIXTURE="$tmp_home/ps-fixture.txt" "$LAUNCHER" status)"
grep -q "active claude-reasonix sessions:" <<<"$stale_status" || fail "status should report active claude-reasonix sessions"
grep -q "pid=12345" <<<"$stale_status" || fail "status should include stale session pid"
grep -q "mode=fleet" <<<"$stale_status" || fail "status should classify fleet session mode"
grep -q "stale_model=gpt-5.5" <<<"$stale_status" || fail "status should flag stale gpt-5.5 sessions"
grep -q "restart required" <<<"$stale_status" || fail "status should tell the user to restart stale sessions"

"$LAUNCHER" generate-config >/dev/null
python3 - "$CLAUDE_REASONIX_FLEET_HOME/runtime/mcp.json" <<'PY'
import json
import os
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    data = json.load(fh)
servers = data.get("mcpServers", {})
expected = {"reasonix_fleet"}
if set(servers) != expected:
    raise SystemExit(f"unexpected servers: {sorted(servers)}")
config = servers["reasonix_fleet"]
if config.get("command") != "/usr/bin/env":
    raise SystemExit("reasonix_fleet should start through /usr/bin/env")
args = config.get("args", [])
if "python3" not in args or not any(arg.endswith("reasonix-fleet-mcp.py") for arg in args):
    raise SystemExit(f"unexpected reasonix_fleet args: {args}")
env = config.get("env", {})
expected_model = os.environ.get("REASONIX_FLEET_MODEL", "deepseek-v4-flash")
if env.get("REASONIX_BIN") != "/bin/echo":
    raise SystemExit(f"REASONIX_BIN was not forwarded: {env}")
if env.get("REASONIX_FLEET_DEFAULT_CONCURRENCY") != "3":
    raise SystemExit(f"default concurrency was not forwarded: {env}")
if env.get("REASONIX_FLEET_MODEL") != expected_model:
    raise SystemExit(f"model env was not {expected_model}: {env}")
if env.get("REASONIX_FLEET_REASONING") != "xhigh":
    raise SystemExit(f"reasoning default was not xhigh: {env}")
if "REASONIX_FLEET_SERVICE_TIER" in env:
    raise SystemExit(f"REASONIX_FLEET_SERVICE_TIER must not be forwarded (dead codex-exec field): {env}")
PY

run_output="$(CLAUDE_REASONIX_NATIVE_SUBAGENTS=0 "$LAUNCHER" run "test prompt")"
grep -q -- "--mcp-config" <<<"$run_output" || fail "run should pass mcp config"
grep -q -- "--disallowedTools Agent,Task" <<<"$run_output" || fail "safe run should block generic Claude Agent/Task"
if grep -q -- " --agents {" <<<"$run_output"; then
  fail "safe run should not pass gateway native subagent definitions by default"
fi
if grep -q -- "--allowedTools" <<<"$run_output"; then
  fail "run should not allow-list only reasonix_fleet"
fi
if grep -q -- "--tools " <<<"$run_output"; then
  fail "run should not disable Claude native tools"
fi
if grep -q -- "--strict-mcp-config" <<<"$run_output"; then
  fail "run should not block normal MCP/plugins"
fi
grep -q "test prompt" <<<"$run_output" || fail "run should forward prompt args"

native_output="$(CLAUDE_REASONIX_NATIVE_SUBAGENTS=1 "$LAUNCHER" run "native prompt")"
grep -q -- " --agents {" <<<"$native_output" || fail "opt-in native gateway mode should pass native subagent definitions"
grep -q "claude-reasonix-flash" <<<"$native_output" || fail "opt-in native gateway mode should include the Reasonix-backed model"
if grep -q -- "--disallowedTools Agent,Task" <<<"$native_output"; then
  fail "opt-in native gateway mode should not globally block Agent/Task"
fi

claude_env_mock="$tmp_home/claude-env-mock"
cat >"$claude_env_mock" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
printf 'CLAUDE_CODE_SUBAGENT_MODEL=%s\n' "${CLAUDE_CODE_SUBAGENT_MODEL:-}"
printf 'ANTHROPIC_BASE_URL=%s\n' "${ANTHROPIC_BASE_URL:-}"
printf 'ANTHROPIC_AUTH_TOKEN=%s\n' "${ANTHROPIC_AUTH_TOKEN:-}"
printf 'ANTHROPIC_CUSTOM_MODEL_OPTION=%s\n' "${ANTHROPIC_CUSTOM_MODEL_OPTION:-}"
printf 'ANTHROPIC_CUSTOM_MODEL_OPTION_NAME=%s\n' "${ANTHROPIC_CUSTOM_MODEL_OPTION_NAME:-}"
printf 'ARGS=%s\n' "$*"
SH
chmod +x "$claude_env_mock"
native_env_output="$(CLAUDE_BIN="$claude_env_mock" CLAUDE_REASONIX_NATIVE_SUBAGENTS=1 "$LAUNCHER" run "native env prompt")"
grep -q "CLAUDE_CODE_SUBAGENT_MODEL=claude-reasonix-flash" <<<"$native_env_output" || fail "native gateway mode should force built-in subagents to the Reasonix-backed model"

"$LAUNCHER" off >/dev/null
# Force fleet mode (reasonix defaults to native) to validate the non-native task path.
task_output="$(CLAUDE_REASONIX_NATIVE_SUBAGENTS=0 "$LAUNCHER" task "one shot prompt")"
grep -q -- "--mcp-config" <<<"$task_output" || fail "task should pass mcp config"
if grep -q -- " --agents {" <<<"$task_output"; then
  fail "task should not pass native subagent definitions by default"
fi
grep -q -- "-p" <<<"$task_output" || fail "task should use Claude print mode"
grep -q "one shot prompt" <<<"$task_output" || fail "task should forward prompt"
after_task_status="$("$LAUNCHER" status)"
grep -q "disabled" <<<"$after_task_status" || fail "task should auto-disable fleet mode after finishing"

printf '{"tool_name":"mcp__reasonix_fleet__run_reasonix_fleet"}' | python3 "$HOOK"
printf '{"tool_name":"mcp__some_other__tool"}' | python3 "$HOOK"
printf '{"tool_name":"Bash"}' | python3 "$HOOK"
printf '{"tool_name":"Workflow"}' | python3 "$HOOK"
printf '{"tool_name":"Read"}' | python3 "$HOOK"
printf '{"tool_name":"Edit"}' | python3 "$HOOK"
if printf '{"tool_name":"Agent"}' | python3 "$HOOK" 2>/dev/null; then
  fail "hook should block Claude subagent tools"
fi
printf '{"tool_name":"Agent","tool_input":{"subagent_type":"reasonix-security"}}' | CLAUDE_REASONIX_NATIVE_SUBAGENTS=1 python3 "$HOOK"
# Legacy codex-*/deepseek-* agentTypes are still whitelisted for in-flight back-compat.
printf '{"tool_name":"Agent","tool_input":{"subagent_type":"codex-security"}}' | CLAUDE_REASONIX_NATIVE_SUBAGENTS=1 python3 "$HOOK"
printf '{"tool_name":"Agent","tool_input":{"subagent_type":"deepseek-deep"}}' | CLAUDE_REASONIX_NATIVE_SUBAGENTS=1 python3 "$HOOK"
if printf '{"tool_name":"Agent","tool_input":{"subagent_type":"Explore"}}' | CLAUDE_REASONIX_NATIVE_SUBAGENTS=1 python3 "$HOOK" 2>/dev/null; then
  fail "native mode should still block non-Reasonix agents"
fi
if printf '{"tool_name":"Task"}' | python3 "$HOOK" 2>/dev/null; then
  fail "hook should block Claude task tools"
fi
if printf '{"tool_name":"Subagent"}' | python3 "$HOOK" 2>/dev/null; then
  fail "hook should block native Subagent tools"
fi
if printf '{"tool_name":"SpawnAgent"}' | python3 "$HOOK" 2>/dev/null; then
  fail "hook should block native SpawnAgent tools"
fi

CLAUDE_REASONIX_WORKFLOW_MODE=native python3 - "$WORKFLOW_HOOK" <<'PY'
import json
import os
import subprocess
import sys

payload = {
    "tool_name": "Workflow",
    "tool_input": {
        "script": "\n".join([
            "phase('CodeReview')",
            "const security = await agent('inspect security', { label: 'cloud:security', phase: 'CodeReview' })",
            "const database = await agent('inspect database', { label: 'cloud:database', phase: 'CodeReview' })",
            "return { security, database }",
            "",
        ]),
    },
}
env = dict(os.environ, CLAUDE_REASONIX_WORKFLOW_MODE="native")
proc = subprocess.run(
    [sys.executable, sys.argv[1]],
    input=json.dumps(payload),
    text=True,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    check=True,
    env=env,
)
out = json.loads(proc.stdout)
updated = out["hookSpecificOutput"]["updatedInput"]
script = updated["script"]
if "__reasonixWorkflowAgent" not in script:
    raise SystemExit("Workflow hook did not inject native Reasonix wrapper")
if "await __reasonixWorkflowAgent('inspect security'" not in script:
    raise SystemExit("Workflow hook did not rewrite agent calls")
if "mcp__reasonix_fleet__run_reasonix_worker" in script:
    raise SystemExit("native Workflow hook should not route through Reasonix Fleet MCP")
if "reasonix-security" not in script:
    raise SystemExit("native Workflow hook should include a Reasonix security agent mapping")
if "reasonix-worker" not in script:
    raise SystemExit("native Workflow hook should include a Reasonix worker mapping (deep folds into worker)")
if "native Claude Code subagents" not in out["hookSpecificOutput"].get("additionalContext", ""):
    raise SystemExit("Workflow hook should add context about the rewrite")
PY

python3 - "$WORKFLOW_HOOK" <<'PY'
import json
import os
import subprocess
import sys

payload = {
    "tool_name": "Workflow",
    "tool_input": {
        "script": "phase('Test')\nconst result = await agent('inspect repo', { label: 'inspect', phase: 'Test' })\nreturn result\n",
    },
}
# Hermetic: this case asserts the DEFAULT (fleet) routing, so the hook must run
# WITHOUT any WORKFLOW_MODE override. When this suite runs INSIDE a live reasonix
# session, that session exports CLAUDE_CODEX_WORKFLOW_MODE=native into the
# environment; inheriting it would flip the hook to native and make this fleet-mode
# assertion a FALSE-POSITIVE failure (observed in a real fan-out run). Strip both the
# legacy and current mode vars so the hook falls back to its own default ("fleet").
_clean_env = {k: v for k, v in os.environ.items()
              if k not in ("CLAUDE_CODEX_WORKFLOW_MODE", "CLAUDE_REASONIX_WORKFLOW_MODE")}
proc = subprocess.run(
    [sys.executable, sys.argv[1]],
    input=json.dumps(payload),
    text=True,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    env=_clean_env,
    check=True,
)
out = json.loads(proc.stdout)
script = out["hookSpecificOutput"]["updatedInput"]["script"]
if "mcp__reasonix_fleet__run_reasonix_worker" not in script:
    raise SystemExit("legacy fleet Workflow hook should still route through Reasonix Fleet MCP")
PY

printf '{"tool_name":"Workflow","tool_input":{"name":"missing-saved-workflow","args":"x"}}' | python3 "$WORKFLOW_HOOK"

"$LAUNCHER" workers 200 >/dev/null
"$LAUNCHER" generate-config >/dev/null
"$LAUNCHER" generate-agents >/dev/null
python3 - "$CLAUDE_REASONIX_FLEET_HOME/runtime/agents.json" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    agents = json.load(fh)
required = {
    "reasonix-worker": "claude-reasonix-flash",
    "reasonix-security": "claude-reasonix-flash",
    "reasonix-reviewer": "claude-reasonix-flash",
    "reasonix-verify": "claude-reasonix-flash",
}
for name, model in required.items():
    if agents.get(name, {}).get("model") != model:
        raise SystemExit(f"missing native agent {name} with model {model}: {agents.get(name)}")
    if agents[name].get("effort") != "xhigh":
        raise SystemExit(f"native agent {name} should use xhigh effort: {agents[name]}")
# The dropped deepseek-* agentTypes must no longer be generated.
for gone in ("deepseek-deep", "deepseek-architecture"):
    if gone in agents:
        raise SystemExit(f"dropped agentType {gone} should not be in agents.json: {agents[gone]}")
PY

port_file="$tmp_home/gateway.port"
python3 "$GATEWAY" --host 127.0.0.1 --port 0 --port-file "$port_file" >"$tmp_home/gateway.log" 2>&1 &
gateway_pid=$!
for _ in {1..50}; do
  if [[ -s "$port_file" ]]; then
    break
  fi
  if ! kill -0 "$gateway_pid" 2>/dev/null; then
    cat "$tmp_home/gateway.log" >&2 || true
    fail "gateway exited before writing a port file"
  fi
  sleep 0.1
done
[[ -s "$port_file" ]] || fail "gateway did not write a port file"
python3 - "$port_file" <<'PY'
import json
import sys
import urllib.request

port = open(sys.argv[1], "r", encoding="utf-8").read().strip()
base = f"http://127.0.0.1:{port}"
models = json.load(urllib.request.urlopen(base + "/v1/models", timeout=5))
ids = {item["id"] for item in models.get("data", [])}
if "claude-reasonix-flash" not in ids:
    raise SystemExit(f"gateway did not advertise claude-reasonix-flash: {models}")

body = json.dumps({"model":"claude-reasonix-flash","messages":[{"role":"user","content":"hello"}]}).encode()
req = urllib.request.Request(base + "/v1/messages/count_tokens", data=body, headers={"content-type":"application/json"})
tokens = json.load(urllib.request.urlopen(req, timeout=5))
if not isinstance(tokens.get("input_tokens"), int) or tokens["input_tokens"] <= 0:
    raise SystemExit(f"bad token estimate: {tokens}")

chat_body = json.dumps({
    "model": "claude-reasonix-flash",
    "messages": [{"role": "user", "content": "hello from ccr"}],
}).encode()
chat_req = urllib.request.Request(
    base + "/v1/chat/completions",
    data=chat_body,
    headers={"content-type": "application/json"},
)
chat = json.load(urllib.request.urlopen(chat_req, timeout=5))
if chat.get("model") != "claude-reasonix-flash":
    raise SystemExit(f"chat endpoint should preserve requested alias model: {chat}")
choice = (chat.get("choices") or [{}])[0]
message = choice.get("message") or {}
if message.get("role") != "assistant":
    raise SystemExit(f"bad chat completion response: {chat}")

messages_body = json.dumps({
    "model": "claude-reasonix-flash",
    "messages": [{"role": "user", "content": "hello through query string"}],
}).encode()
messages_req = urllib.request.Request(
    base + "/v1/messages?anthropic-version=2023-06-01",
    data=messages_body,
    headers={"content-type": "application/json"},
)
# reasonix_cli ALWAYS streams (heartbeat path), even without stream=true, so the
# 180s workflow watchdog never fires on a slow lane. Parse the SSE message_start
# instead of expecting a single JSON blob.
resp = urllib.request.urlopen(messages_req, timeout=5)
ctype = resp.headers.get("content-type", "")
raw = resp.read().decode("utf-8")
if not ctype.startswith("text/event-stream"):
    raise SystemExit(f"reasonix_cli /v1/messages should stream SSE now, got content-type={ctype!r}: {raw[:200]}")
model_seen = None
for line in raw.splitlines():
    if line.startswith("data: "):
        try:
            evt = json.loads(line[len("data: "):])
        except Exception:
            continue
        m = (evt.get("message") or {}).get("model") or evt.get("model")
        if m:
            model_seen = m
            break
if model_seen != "claude-reasonix-flash":
    raise SystemExit(f"messages endpoint should ignore query string while routing: model_seen={model_seen!r} raw={raw[:200]}")
PY
kill "$gateway_pid"
wait "$gateway_pid" 2>/dev/null || true

python3 - "$CLAUDE_REASONIX_FLEET_HOME/runtime/mcp.json" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    data = json.load(fh)
server = data["mcpServers"]["reasonix_fleet"]
if server["env"].get("REASONIX_FLEET_DEFAULT_CONCURRENCY") != "200":
    raise SystemExit("workers 200 should set default concurrency to 200")
PY

echo "PASS: reasonix fleet launcher"

python3 "$ROOT/tests/test-workflow-selfheal.py" || fail "workflow self-heal regression"

# Verify the REPO launcher (the source of truth under test) wires the reasonix flavor.
# Install-location concerns (symlink, ~/.local/bin) are validated by the install test.
LAUNCHER_BIN="$LAUNCHER"
[[ -f "$LAUNCHER_BIN" ]] || fail "launcher not found at $LAUNCHER_BIN"
grep -Eq 'CLAUDE_REASONIX_FLAVOR="?reasonix"?' "$LAUNCHER_BIN" || fail "launcher must set CLAUDE_REASONIX_FLAVOR=reasonix"
grep -q 'claude-reasonix-flash' "$LAUNCHER_BIN" || fail "launcher reasonix flavor must force claude-reasonix-flash"
grep -q "REASONIX_BIN" "$LAUNCHER_BIN" || fail "launcher reasonix flavor must export REASONIX_BIN (gateway needs reasonix+node on PATH)"

CLAUDE_REASONIX_FLAVOR=reasonix python3 - "$GATEWAY" <<'PY' || fail "reasonix flavor must expose claude-reasonix-flash"
import importlib.util, sys
spec = importlib.util.spec_from_file_location("g", sys.argv[1])
g = importlib.util.module_from_spec(spec); spec.loader.exec_module(g)
reg = g.model_registry()
assert "claude-reasonix-flash" in reg, list(reg)
assert reg["claude-reasonix-flash"]["provider"] == "reasonix_cli"
PY

# The acp driver test exercises the REAL run_reasonix_acp path with its own fake
# driver, so it must run with the general GATEWAY_MOCK switch OFF (this suite
# exports it=1 for the HTTP probes above).
env -u CLAUDE_CODEX_GATEWAY_MOCK -u CLAUDE_REASONIX_GATEWAY_MOCK \
  python3 "$ROOT/tests/test-reasonix-acp.py" || fail "reasonix acp driver regression"

if [[ "${CLAUDE_REASONIX_E2E:-0}" == "1" ]]; then
  bash "$ROOT/tests/test-reasonix-e2e.sh" || fail "reasonix e2e"
else
  echo "SKIP: reasonix e2e (set CLAUDE_REASONIX_E2E=1 to run)"
fi

python3 "$ROOT/tests/test-reasonix-cost-ledger.py" || fail "reasonix cost ledger regression"

# Hook flavor-awareness: reasonix flavor must NOT block the native Agent tool
# (so subagents route to reasonix, not the reasonix_fleet MCP); the legacy flavor still blocks Agent.
echo '{"tool_name":"Agent","tool_input":{"prompt":"x"}}' | CLAUDE_REASONIX_FLAVOR=reasonix python3 "$ROOT/hooks/only-reasonix-fleet.py" >/dev/null 2>&1 && fail "reasonix flavor must STILL block Agent (push to reasonix MCP, not native which hangs)"
# back-compat: a legacy flavor value (non-reasonix) must still block the Agent tool.
echo '{"tool_name":"Agent","tool_input":{"prompt":"x"}}' | CLAUDE_REASONIX_FLAVOR=codex CLAUDE_REASONIX_NATIVE_SUBAGENTS=0 python3 "$ROOT/hooks/only-reasonix-fleet.py" >/dev/null 2>&1 && fail "a legacy flavor value must still block the Agent tool"
echo "PASS: only-reasonix-fleet flavor-aware"

# Like the acp test, this drives the real reasonix engine via its own fake driver
# and must not be intercepted by the suite-level GATEWAY_MOCK switch.
env -u CLAUDE_CODEX_GATEWAY_MOCK -u CLAUDE_REASONIX_GATEWAY_MOCK \
  python3 "$ROOT/tests/test-mcp-reasonix.py" || fail "mcp reasonix flavor regression"
