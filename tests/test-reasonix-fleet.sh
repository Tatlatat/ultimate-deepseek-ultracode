#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAUNCHER="$ROOT/bin/claude-reasonix"
HOOK="$ROOT/hooks/only-reasonix-fleet.py"
WORKFLOW_HOOK="$ROOT/hooks/reasonix-workflow.py"
MCP_SERVER="$ROOT/reasonix-fleet-mcp.py"
GATEWAY="$ROOT/reasonix-native-gateway.py"
CCR_PROXY="$ROOT/ccr-claude-proxy.py"

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
assert_file "$CCR_PROXY"

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
    "mcp__codex_fleet__run_codex_worker",
    "mcp__codex_fleet__run_codex_fleet",
    "mcp__codex_fleet__fleet_status",
}
if not required.issubset(set(permissions)):
    raise SystemExit(f"bridge settings must allow Codex Fleet MCP tools: {permissions}")
PY

tmp_home="$(mktemp -d)"
trap 'rm -rf "$tmp_home"' EXIT

# Point the launcher at the REPO as its install home so it loads the renamed
# gateway/mcp/hooks/settings under test (not a stale ~/.claude install).
export CLAUDE_REASONIX_FLEET_INSTALL_HOME="$ROOT"
export CLAUDE_CODEX_FLEET_HOME="$tmp_home/fleet"
export CLAUDE_BIN="/bin/echo"
export CODEX_BIN="/bin/echo"
export CCR_BIN="/bin/echo"
export ANTHROPIC_API_KEY="test-anthropic-key"
export CLAUDE_CODEX_GATEWAY_MOCK=1
export CLAUDE_CODEX_QWEN_SKIP_START=1
export CLAUDE_CODEX_KEEP_ROUTER_RUNTIME=1

latest_router_config() {
  find "$CLAUDE_CODEX_FLEET_HOME/runtime/router-sessions" \
    -path '*/.claude-code-router/config.json' \
    -type f -print 2>/dev/null | sort | tail -n 1
}

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
os.environ["CLAUDE_CODEX_CODEX_BACKEND"] = "codex-cli"
os.environ.pop("CLAUDE_CODEX_GATEWAY_MOCK", None)
# Force the wait loop to tick quickly so a heartbeat is emitted before the result.
os.environ["CLAUDE_CODEX_GATEWAY_STREAM_KEEPALIVE_SECONDS"] = "1"
spec = importlib.util.spec_from_file_location("codex_native_gateway_stream", gateway_path)
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

# Regression: the gateway codex timeout default must be the watchdog-safe 600s (not 165s),
# so legitimate long web-search lanes are not killed mid-work.
python3 - "$GATEWAY" <<'PY'
import importlib.util
import os
import sys

gateway_path = sys.argv[1]
os.environ.pop("CLAUDE_CODEX_GATEWAY_CODEX_TIMEOUT", None)
os.environ.pop("CODEX_FLEET_TIMEOUT_SECONDS", None)
spec = importlib.util.spec_from_file_location("codex_native_gateway_timeout_default", gateway_path)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)
default = module.env_first("CLAUDE_CODEX_GATEWAY_CODEX_TIMEOUT", "CODEX_FLEET_TIMEOUT_SECONDS", default="600")
if float(default) < 600:
    raise SystemExit(f"codex timeout default must be >= 600s, got {default}")
PY

python3 - "$GATEWAY" <<'PY'
import importlib.util
import sys

gateway_path = sys.argv[1]
spec = importlib.util.spec_from_file_location("codex_native_gateway_prompt_schema", gateway_path)
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
grep -q -- "--mcp-config" <<<"$bare_output" || fail "bare claude-codex should start fleet even when status is disabled"
grep -q -- "--disallowedTools Agent,Task" <<<"$bare_output" || fail "safe claude-codex should block generic Claude Agent/Task"
if grep -q -- " --agents {" <<<"$bare_output"; then
  fail "safe claude-codex should not define gateway native subagents by default"
fi
if grep -q -- "--allowedTools" <<<"$bare_output"; then
  fail "bare claude-codex should not allow-list only codex_fleet"
fi
if grep -q -- "--tools " <<<"$bare_output"; then
  fail "bare claude-codex should not disable Claude native tools"
fi
if grep -q -- "--strict-mcp-config" <<<"$bare_output"; then
  fail "bare claude-codex should not block normal MCP/plugins"
fi
grep -q "bare prompt" <<<"$bare_output" || fail "bare claude-codex should forward prompt"
plain_output="$("$LAUNCHER" plain "plain prompt")"
if grep -q -- "--mcp-config" <<<"$plain_output"; then
  fail "plain mode should bypass fleet"
fi
if grep -q -- " --agents {" <<<"$plain_output"; then
  fail "plain mode should not define claude-codex native subagents"
fi
grep -q "plain prompt" <<<"$plain_output" || fail "plain mode should forward prompt"

"$LAUNCHER" on 3 >/dev/null
enabled_status="$("$LAUNCHER" status)"
grep -q "enabled" <<<"$enabled_status" || fail "status should report enabled"
grep -q "default concurrency: 3" <<<"$enabled_status" || fail "status should show default concurrency"

cat >"$tmp_home/ps-fixture.txt" <<EOF
12345 claude --mcp-config $CLAUDE_CODEX_FLEET_HOME/runtime/mcp.json --append-system-prompt Every Codex worker defaults to GPT-5.5 --disallowedTools Agent,Task
EOF
stale_status="$(CLAUDE_CODEX_PS_FIXTURE="$tmp_home/ps-fixture.txt" "$LAUNCHER" status)"
grep -q "active claude-codex sessions:" <<<"$stale_status" || fail "status should report active claude-codex sessions"
grep -q "pid=12345" <<<"$stale_status" || fail "status should include stale session pid"
grep -q "mode=fleet" <<<"$stale_status" || fail "status should classify fleet session mode"
grep -q "stale_model=gpt-5.5" <<<"$stale_status" || fail "status should flag stale gpt-5.5 sessions"
grep -q "restart required" <<<"$stale_status" || fail "status should tell the user to restart stale sessions"

"$LAUNCHER" generate-config >/dev/null
python3 - "$CLAUDE_CODEX_FLEET_HOME/runtime/mcp.json" <<'PY'
import json
import os
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    data = json.load(fh)
servers = data.get("mcpServers", {})
expected = {"codex_fleet"}
if set(servers) != expected:
    raise SystemExit(f"unexpected servers: {sorted(servers)}")
config = servers["codex_fleet"]
if config.get("command") != "/usr/bin/env":
    raise SystemExit("codex_fleet should start through /usr/bin/env")
args = config.get("args", [])
if "python3" not in args or not any(arg.endswith("reasonix-fleet-mcp.py") for arg in args):
    raise SystemExit(f"unexpected codex_fleet args: {args}")
env = config.get("env", {})
expected_model = os.environ.get("CODEX_FLEET_MODEL", "gpt-5.4")
if env.get("CODEX_BIN") != "/bin/echo":
    raise SystemExit(f"CODEX_BIN was not forwarded: {env}")
if env.get("CODEX_FLEET_DEFAULT_CONCURRENCY") != "3":
    raise SystemExit(f"default concurrency was not forwarded: {env}")
if env.get("CODEX_FLEET_MODEL") != expected_model:
    raise SystemExit(f"model env was not {expected_model}: {env}")
if env.get("CODEX_FLEET_REASONING") != "xhigh":
    raise SystemExit(f"reasoning default was not xhigh: {env}")
if env.get("CODEX_FLEET_SERVICE_TIER") != "fast":
    raise SystemExit(f"service tier default was not fast: {env}")
PY

run_output="$(CLAUDE_REASONIX_NATIVE_SUBAGENTS=0 "$LAUNCHER" run "test prompt")"
grep -q -- "--mcp-config" <<<"$run_output" || fail "run should pass mcp config"
grep -q -- "--disallowedTools Agent,Task" <<<"$run_output" || fail "safe run should block generic Claude Agent/Task"
if grep -q -- " --agents {" <<<"$run_output"; then
  fail "safe run should not pass gateway native subagent definitions by default"
fi
if grep -q -- "--allowedTools" <<<"$run_output"; then
  fail "run should not allow-list only codex_fleet"
fi
if grep -q -- "--tools " <<<"$run_output"; then
  fail "run should not disable Claude native tools"
fi
if grep -q -- "--strict-mcp-config" <<<"$run_output"; then
  fail "run should not block normal MCP/plugins"
fi
grep -q "test prompt" <<<"$run_output" || fail "run should forward prompt args"

native_output="$(CLAUDE_CODEX_NATIVE_SUBAGENTS=1 "$LAUNCHER" run "native prompt")"
grep -q -- " --agents {" <<<"$native_output" || fail "opt-in native gateway mode should pass native subagent definitions"
grep -q "claude-reasonix-flash" <<<"$native_output" || fail "opt-in native gateway mode should include the Reasonix-backed model"
if grep -q -- "--disallowedTools Agent,Task" <<<"$native_output"; then
  fail "opt-in native gateway mode should not globally block Agent/Task"
fi

router_output="$("$LAUNCHER" router "router prompt")"
grep -q -- " --agents {" <<<"$router_output" || fail "router mode should pass native subagent definitions"
grep -q "claude-reasonix-flash" <<<"$router_output" || fail "router mode should include the Reasonix-backed model"
grep -q "<CCR-SUBAGENT-MODEL>codex-gateway,claude-reasonix-flash</CCR-SUBAGENT-MODEL>" <<<"$router_output" || fail "router mode should tag worker agents for CCR via codex-gateway"
grep -q "router prompt" <<<"$router_output" || fail "router mode should forward prompt args"
if grep -q -- "--disallowedTools Agent,Task" <<<"$router_output"; then
  fail "router mode should not globally block Agent/Task"
fi

router_login_output="$("$LAUNCHER" router-login "router login prompt")"
grep -q -- " --agents {" <<<"$router_login_output" || fail "router-login mode should pass native subagent definitions"
grep -q "<CCR-SUBAGENT-MODEL>codex-gateway,claude-reasonix-flash</CCR-SUBAGENT-MODEL>" <<<"$router_login_output" || fail "router-login mode should tag worker agents for CCR via codex-gateway"
grep -q "router login prompt" <<<"$router_login_output" || fail "router-login mode should forward prompt args"
if grep -q -- "--disallowedTools Agent,Task" <<<"$router_login_output"; then
  fail "router-login mode should not globally block Agent/Task"
fi
router_login_config="$(latest_router_config)"
[[ -n "$router_login_config" ]] || fail "router-login should keep an isolated router config for tests"
python3 - "$router_login_config" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    config = json.load(fh)
if config.get("APIKEY") != "":
    raise SystemExit(f"router-login should start CCR without a static API key: {config.get('APIKEY')!r}")
PY

router_qwen_output="$("$LAUNCHER" router-qwen "router qwen prompt")"
grep -q -- " --agents {" <<<"$router_qwen_output" || fail "router-qwen mode should pass native subagent definitions"
grep -q "<CCR-SUBAGENT-MODEL>qwen36-local,qwen36-mlx</CCR-SUBAGENT-MODEL>" <<<"$router_qwen_output" || fail "router-qwen mode should tag Qwen agents for CCR"
grep -q "qwen36-mlx" <<<"$router_qwen_output" || fail "router-qwen mode should include the local Qwen model"
grep -q -- "--model qwen36-mlx" <<<"$router_qwen_output" || fail "router-qwen mode should select the local Qwen model"
grep -q "router qwen prompt" <<<"$router_qwen_output" || fail "router-qwen mode should forward prompt args"
if grep -q -- "--disallowedTools Agent,Task" <<<"$router_qwen_output"; then
  fail "router-qwen mode should not globally block Agent/Task"
fi
router_qwen_config="$(latest_router_config)"
[[ -n "$router_qwen_config" ]] || fail "router-qwen should keep an isolated router config for tests"
python3 - "$router_qwen_config" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    config = json.load(fh)
router = config.get("Router", {})
if router.get("default") != "qwen36-local,qwen36-mlx":
    raise SystemExit(f"router-qwen should make local Qwen the main route: {router}")
providers = {provider.get("name"): provider for provider in config.get("Providers", [])}
ccr_providers = {provider.get("name"): provider for provider in config.get("providers", [])}
if "qwen36-local" not in ccr_providers:
    raise SystemExit(f"router-qwen should emit lowercase providers for installed CCR: {ccr_providers}")
if ccr_providers["qwen36-local"].get("models") != ["qwen36-mlx"]:
    raise SystemExit(f"router-qwen lowercase providers should include Qwen: {ccr_providers['qwen36-local']}")
if ccr_providers["qwen36-local"].get("transformer", {}).get("use") != ["Anthropic"]:
    raise SystemExit(f"router-qwen Qwen provider should use CCR's registerable Anthropic transformer form: {ccr_providers['qwen36-local']}")
anthropic_models = providers.get("anthropic", {}).get("models", [])
if "qwen36-mlx" in anthropic_models:
    raise SystemExit(f"router-qwen should not advertise local Qwen as an Anthropic model: {anthropic_models}")
qwen_models = providers.get("qwen36-local", {}).get("models", [])
if qwen_models != ["qwen36-mlx"]:
    raise SystemExit(f"router-qwen should advertise local Qwen only on qwen36-local: {qwen_models}")
PY

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
native_env_output="$(CLAUDE_BIN="$claude_env_mock" CLAUDE_CODEX_NATIVE_SUBAGENTS=1 "$LAUNCHER" run "native env prompt")"
grep -q "CLAUDE_CODE_SUBAGENT_MODEL=claude-reasonix-flash" <<<"$native_env_output" || fail "native gateway mode should force built-in subagents to the Reasonix-backed model"

router_env_output="$(CLAUDE_BIN="$claude_env_mock" "$LAUNCHER" router "router env prompt")"
grep -q "CLAUDE_CODE_SUBAGENT_MODEL=claude-reasonix-flash" <<<"$router_env_output" || fail "router mode should force built-in subagents to the Reasonix-backed model"
grep -q "ANTHROPIC_CUSTOM_MODEL_OPTION=claude-reasonix-flash" <<<"$router_env_output" || fail "router mode should expose the Reasonix-backed custom model option"

router_env_inherit_output="$(CLAUDE_BIN="$claude_env_mock" CLAUDE_CODEX_SUBAGENT_MODEL=inherit "$LAUNCHER" router "router inherit prompt")"
grep -q "CLAUDE_CODE_SUBAGENT_MODEL=inherit" <<<"$router_env_inherit_output" || fail "router mode should honor explicit CLAUDE_CODEX_SUBAGENT_MODEL overrides"

router_qwen_env_output="$(CLAUDE_BIN="$claude_env_mock" "$LAUNCHER" router-qwen "router qwen env prompt")"
grep -q "CLAUDE_CODE_SUBAGENT_MODEL=claude-reasonix-flash" <<<"$router_qwen_env_output" || fail "router-qwen should still force subagents to the Reasonix-backed model by default"
grep -q "ANTHROPIC_BASE_URL=http://127.0.0.1:" <<<"$router_qwen_env_output" || fail "router-qwen should point Claude at the scoped CCR proxy"
grep -q "ANTHROPIC_AUTH_TOKEN=claude-codex-router" <<<"$router_qwen_env_output" || fail "router-qwen should authenticate Claude to the scoped CCR proxy"
grep -q "ANTHROPIC_CUSTOM_MODEL_OPTION=claude-reasonix-flash" <<<"$router_qwen_env_output" || fail "router-qwen should expose the reasonix model id as Claude Code's custom model option"
grep -q "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME=qwen36-mlx" <<<"$router_qwen_env_output" || fail "router-qwen should name Claude Code's custom model option as Qwen"
grep -q -- "--model qwen36-mlx" <<<"$router_qwen_env_output" || fail "router-qwen env smoke should still select the local Qwen model"

"$LAUNCHER" generate-ccr-config >/dev/null
python3 - "$CLAUDE_CODEX_FLEET_HOME/runtime/ccr-home/.claude-code-router/config.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.is_file():
    raise SystemExit(f"missing CCR config: {path}")
with path.open("r", encoding="utf-8") as fh:
    config = json.load(fh)
router = config.get("Router", {})
if router.get("default") != "anthropic,claude-opus-4-8":
    raise SystemExit(f"router default should preserve Opus: {router}")
providers = {provider.get("name"): provider for provider in config.get("Providers", [])}
for name in ("anthropic", "codex-gateway", "deepseek-gateway", "qwen36-local"):
    if name not in providers:
        raise SystemExit(f"missing CCR provider {name}: {providers}")
ccr_providers = {provider.get("name"): provider for provider in config.get("providers", [])}
for name in ("anthropic", "codex-gateway", "deepseek-gateway", "qwen36-local"):
    if name not in ccr_providers:
        raise SystemExit(f"missing lowercase CCR provider {name}: {ccr_providers}")
if "claude-opus-4-8" not in providers["anthropic"].get("models", []):
    raise SystemExit(f"anthropic provider should advertise claude-opus-4-8: {providers['anthropic']}")
if ccr_providers["anthropic"].get("transformer", {}).get("use") != ["Anthropic"]:
    raise SystemExit(f"anthropic provider should use CCR's registerable Anthropic transformer form: {ccr_providers['anthropic']}")
if providers["codex-gateway"].get("models") != ["claude-reasonix-flash"]:
    raise SystemExit(f"bad codex-gateway CCR provider: {providers['codex-gateway']}")
if providers["deepseek-gateway"].get("models") != ["claude-reasonix-flash"]:
    raise SystemExit(f"bad deepseek-gateway CCR provider: {providers['deepseek-gateway']}")
if providers["qwen36-local"].get("models") != ["qwen36-mlx"]:
    raise SystemExit(f"bad Qwen CCR provider: {providers['qwen36-local']}")
custom_router = Path(config.get("CUSTOM_ROUTER_PATH", ""))
if not custom_router.is_file():
    raise SystemExit(f"missing custom router: {custom_router}")
source = custom_router.read_text(encoding="utf-8")
if "CCR-SUBAGENT-MODEL" not in source or "req.body.model" not in source:
    raise SystemExit("custom router should preserve subagent tags and explicit Claude model routing")
if "qwenModels.has(model)" not in source or "qwen36-local," not in source:
    raise SystemExit("custom router should route local Qwen explicitly")
PY

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

printf '{"tool_name":"mcp__codex_fleet__run_codex_fleet"}' | python3 "$HOOK"
printf '{"tool_name":"mcp__codex_2__codex"}' | python3 "$HOOK"
printf '{"tool_name":"Bash"}' | python3 "$HOOK"
printf '{"tool_name":"Workflow"}' | python3 "$HOOK"
printf '{"tool_name":"Read"}' | python3 "$HOOK"
printf '{"tool_name":"Edit"}' | python3 "$HOOK"
if printf '{"tool_name":"Agent"}' | python3 "$HOOK" 2>/dev/null; then
  fail "hook should block Claude subagent tools"
fi
printf '{"tool_name":"Agent","tool_input":{"subagent_type":"reasonix-security"}}' | CLAUDE_CODEX_NATIVE_SUBAGENTS=1 python3 "$HOOK"
# Legacy codex-*/deepseek-* agentTypes are still whitelisted for in-flight back-compat.
printf '{"tool_name":"Agent","tool_input":{"subagent_type":"codex-security"}}' | CLAUDE_CODEX_NATIVE_SUBAGENTS=1 python3 "$HOOK"
printf '{"tool_name":"Agent","tool_input":{"subagent_type":"deepseek-deep"}}' | CLAUDE_CODEX_NATIVE_SUBAGENTS=1 python3 "$HOOK"
if printf '{"tool_name":"Agent","tool_input":{"subagent_type":"Explore"}}' | CLAUDE_CODEX_NATIVE_SUBAGENTS=1 python3 "$HOOK" 2>/dev/null; then
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

CLAUDE_CODEX_WORKFLOW_MODE=native python3 - "$WORKFLOW_HOOK" <<'PY'
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
env = dict(os.environ, CLAUDE_CODEX_WORKFLOW_MODE="native")
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
if "__codexWorkflowAgent" not in script:
    raise SystemExit("Workflow hook did not inject native Codex wrapper")
if "await __codexWorkflowAgent('inspect security'" not in script:
    raise SystemExit("Workflow hook did not rewrite agent calls")
if "mcp__codex_fleet__run_codex_worker" in script:
    raise SystemExit("native Workflow hook should not route through Codex Fleet MCP")
if "reasonix-security" not in script:
    raise SystemExit("native Workflow hook should include a Reasonix security agent mapping")
if "reasonix-worker" not in script:
    raise SystemExit("native Workflow hook should include a Reasonix worker mapping (deep folds into worker)")
if "native Claude Code subagents" not in out["hookSpecificOutput"].get("additionalContext", ""):
    raise SystemExit("Workflow hook should add context about the rewrite")
PY

CLAUDE_CODEX_WORKFLOW_MODE=router python3 - "$WORKFLOW_HOOK" <<'PY'
import json
import os
import subprocess
import sys

payload = {
    "tool_name": "Workflow",
    "tool_input": {
        "script": "phase('CodeReview')\nconst result = await agent('inspect repo', { label: 'cloud:security', phase: 'CodeReview' })\nreturn result\n",
    },
}
env = dict(os.environ, CLAUDE_CODEX_WORKFLOW_MODE="router")
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
script = out["hookSpecificOutput"]["updatedInput"]["script"]
if "__codexWorkflowAgent" not in script:
    raise SystemExit("router Workflow hook did not inject Codex wrapper")
if "reasonix-security" not in script:
    raise SystemExit("router Workflow hook should route security lanes to reasonix-security")
if "mcp__codex_fleet__run_codex_worker" in script:
    raise SystemExit("router Workflow hook should not route through Codex Fleet MCP")
context = out["hookSpecificOutput"].get("additionalContext", "")
if "Claude Code Router" not in context or "native Claude Code subagents" not in context:
    raise SystemExit(f"router Workflow hook should describe CCR native subagents: {context}")
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
proc = subprocess.run(
    [sys.executable, sys.argv[1]],
    input=json.dumps(payload),
    text=True,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    check=True,
)
out = json.loads(proc.stdout)
script = out["hookSpecificOutput"]["updatedInput"]["script"]
if "mcp__codex_fleet__run_codex_worker" not in script:
    raise SystemExit("legacy fleet Workflow hook should still route through Codex Fleet MCP")
PY

printf '{"tool_name":"Workflow","tool_input":{"name":"missing-saved-workflow","args":"x"}}' | python3 "$WORKFLOW_HOOK"

"$LAUNCHER" workers 200 >/dev/null
"$LAUNCHER" generate-config >/dev/null
"$LAUNCHER" generate-agents >/dev/null
python3 - "$CLAUDE_CODEX_FLEET_HOME/runtime/agents.json" <<'PY'
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

proxy_port_file="$tmp_home/ccr-proxy.port"
python3 "$CCR_PROXY" \
  --host 127.0.0.1 \
  --port 0 \
  --port-file "$proxy_port_file" \
  --target "http://127.0.0.1:9" \
  --api-key "test-router-key" \
  --models "claude-opus-4-8,claude-reasonix-flash" \
  >"$tmp_home/ccr-proxy.log" 2>&1 &
proxy_pid=$!
for _ in {1..50}; do
  if [[ -s "$proxy_port_file" ]]; then
    break
  fi
  if ! kill -0 "$proxy_pid" 2>/dev/null; then
    cat "$tmp_home/ccr-proxy.log" >&2 || true
    fail "CCR Claude proxy exited before writing a port file"
  fi
  sleep 0.1
done
[[ -s "$proxy_port_file" ]] || fail "CCR Claude proxy did not write a port file"
python3 - "$proxy_port_file" <<'PY'
import json
import sys
import urllib.request

port = open(sys.argv[1], "r", encoding="utf-8").read().strip()
base = f"http://127.0.0.1:{port}"
health = json.load(urllib.request.urlopen(base + "/health", timeout=5))
if health.get("ok") is not True:
    raise SystemExit(f"bad CCR proxy health: {health}")
models = json.load(urllib.request.urlopen(base + "/v1/models", timeout=5))
ids = {item["id"] for item in models.get("data", [])}
required = {"claude-opus-4-8", "claude-reasonix-flash"}
if not required.issubset(ids):
    raise SystemExit(f"CCR proxy should expose Opus and reasonix alias for Claude Code discovery: {models}")
PY
kill "$proxy_pid"
wait "$proxy_pid" 2>/dev/null || true

main_port_file="$tmp_home/main-target.port"
ccr_port_file="$tmp_home/ccr-target.port"
direct_port_file="$tmp_home/direct-target.port"
python3 - "$main_port_file" "$ccr_port_file" "$direct_port_file" <<'PY' &
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading

main_port_file, ccr_port_file, direct_port_file = sys.argv[1:]


class TargetHandler(BaseHTTPRequestHandler):
    route = ""

    def log_message(self, fmt, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("content-length") or "0")
        body = json.loads((self.rfile.read(length) if length else b"{}").decode() or "{}")
        data = json.dumps({
            "route": self.route,
            "model": body.get("model"),
            "authorization": self.headers.get("authorization"),
            "x_api_key": self.headers.get("x-api-key"),
        }).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(route, port_file):
    handler = type(f"{route.title()}Handler", (TargetHandler,), {"route": route})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    Path(port_file).write_text(str(server.server_address[1]), encoding="utf-8")
    server.serve_forever()


threading.Thread(target=serve, args=("main", main_port_file), daemon=True).start()
threading.Thread(target=serve, args=("ccr", ccr_port_file), daemon=True).start()
threading.Thread(target=serve, args=("direct", direct_port_file), daemon=True).start()
threading.Event().wait()
PY
targets_pid=$!
for _ in {1..50}; do
  [[ -s "$main_port_file" && -s "$ccr_port_file" && -s "$direct_port_file" ]] && break
  sleep 0.1
done
[[ -s "$main_port_file" && -s "$ccr_port_file" && -s "$direct_port_file" ]] || fail "fake proxy targets did not start"
passthrough_proxy_port_file="$tmp_home/ccr-proxy-passthrough.port"
python3 "$CCR_PROXY" \
  --host 127.0.0.1 \
  --port 0 \
  --port-file "$passthrough_proxy_port_file" \
  --target "http://127.0.0.1:$(cat "$ccr_port_file")" \
  --main-target "http://127.0.0.1:$(cat "$main_port_file")" \
  --direct-alias-target "http://127.0.0.1:$(cat "$direct_port_file")" \
  --passthrough-main \
  --api-key "ccr-test-key" \
  --models "claude-opus-4-8,claude-reasonix-flash" \
  --alias-models "claude-reasonix-flash" \
  --direct-alias-models "claude-reasonix-flash" \
  >"$tmp_home/ccr-proxy-passthrough.log" 2>&1 &
passthrough_proxy_pid=$!
for _ in {1..50}; do
  if [[ -s "$passthrough_proxy_port_file" ]]; then
    break
  fi
  if ! kill -0 "$passthrough_proxy_pid" 2>/dev/null; then
    cat "$tmp_home/ccr-proxy-passthrough.log" >&2 || true
    fail "CCR passthrough proxy exited before writing a port file"
  fi
  sleep 0.1
done
[[ -s "$passthrough_proxy_port_file" ]] || fail "CCR passthrough proxy did not write a port file"
python3 - "$passthrough_proxy_port_file" <<'PY'
import json
import sys
import urllib.request

port = open(sys.argv[1], "r", encoding="utf-8").read().strip()
base = f"http://127.0.0.1:{port}"

def post(model, auth, system=None):
    payload = {"model": model, "messages": [{"role": "user", "content": "hello"}]}
    if system is not None:
        payload["system"] = system
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        base + "/v1/messages",
        data=body,
        headers={"content-type": "application/json", "authorization": auth},
    )
    return json.load(urllib.request.urlopen(req, timeout=5))

main = post("claude-opus-4-8", "Bearer login-token")
if main.get("route") != "main" or main.get("authorization") != "Bearer login-token":
    raise SystemExit(f"main model should passthrough incoming login auth: {main}")

doc_only = post(
    "claude-opus-4-8",
    "Bearer login-token",
    [{"type": "text", "text": "Router docs mention `<CCR-SUBAGENT-MODEL>...` inline, but this is not a subagent tag."}],
)
if doc_only.get("route") != "main" or doc_only.get("authorization") != "Bearer login-token":
    raise SystemExit(f"inline documentation text should not route the main model to CCR: {doc_only}")

alias = post("claude-reasonix-flash", "Bearer login-token")
if alias.get("route") != "direct" or alias.get("authorization") != "Bearer ccr-test-key":
    raise SystemExit(f"direct alias model should route to the native gateway target with CCR auth: {alias}")

tagged = post(
    "claude-opus-4-8",
    "Bearer login-token",
    [{"type": "text", "text": "<CCR-SUBAGENT-MODEL>codex-gateway,claude-reasonix-flash</CCR-SUBAGENT-MODEL>\nworker"}],
)
if tagged.get("route") != "ccr" or tagged.get("authorization") != "Bearer ccr-test-key":
    raise SystemExit(f"tagged subagent request should route to CCR even when the model is main Claude: {tagged}")

workflow_subagent = post(
    "claude-opus-4-8",
    "Bearer login-token",
    [{
        "type": "text",
        "text": (
            "x-anthropic-billing-header: cc_version=2.1.183; cc_entrypoint=sdk-cli; cc_is_subagent=true;\n"
            "You are a subagent spawned by a workflow orchestration script."
        ),
    }],
)
if (
    workflow_subagent.get("route") != "direct"
    or workflow_subagent.get("authorization") != "Bearer ccr-test-key"
    or workflow_subagent.get("model") != "claude-reasonix-flash"
):
    raise SystemExit(
        "workflow subagent requests using the main model should be forced to the Reasonix-backed direct alias target: "
        f"{workflow_subagent}"
    )
PY
kill "$passthrough_proxy_pid" "$targets_pid"
wait "$passthrough_proxy_pid" 2>/dev/null || true
wait "$targets_pid" 2>/dev/null || true
python3 - "$CLAUDE_CODEX_FLEET_HOME/runtime/mcp.json" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    data = json.load(fh)
server = data["mcpServers"]["codex_fleet"]
if server["env"].get("CODEX_FLEET_DEFAULT_CONCURRENCY") != "200":
    raise SystemExit("workers 200 should set default concurrency to 200")
PY

echo "PASS: codex fleet launcher"

python3 "$ROOT/tests/test-workflow-selfheal.py" || fail "workflow self-heal regression"

# Verify the launcher itself wires the reasonix flavor
LAUNCHER_BIN="$HOME/.local/bin/claude-codex"
[[ -f "$LAUNCHER_BIN" ]] || fail "launcher not found at $LAUNCHER_BIN"
grep -Eq 'CLAUDE_CODEX_FLAVOR="?reasonix"?' "$LAUNCHER_BIN" || fail "launcher must set CLAUDE_CODEX_FLAVOR=reasonix"
grep -q 'claude-reasonix-flash' "$LAUNCHER_BIN" || fail "launcher reasonix flavor must force claude-reasonix-flash"
grep -q "REASONIX_BIN" "$LAUNCHER_BIN" || fail "launcher reasonix flavor must export REASONIX_BIN (gateway needs reasonix+node on PATH)"
grep -q "CLAUDE_CODEX_CCR_CODEX_ROUTE.*claude-reasonix-flash" "$LAUNCHER_BIN" || fail "launcher reasonix flavor must route worker agents to claude-reasonix-flash, not codex"
grep -q "CLAUDE_CODEX_CCR_DEEPSEEK_MODEL.*claude-reasonix-flash" "$LAUNCHER_BIN" || fail "launcher reasonix flavor must point deepseek-* agent model at reasonix-flash (else they die Not-logged-in)"
[[ -L "$HOME/.local/bin/claude-reasonix" ]] || fail "claude-reasonix must be a symlink"

CLAUDE_CODEX_FLAVOR=reasonix python3 - "$GATEWAY" <<'PY' || fail "reasonix flavor must expose claude-reasonix-flash"
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

if [[ "${CLAUDE_CODEX_REASONIX_E2E:-0}" == "1" ]]; then
  bash "$ROOT/tests/test-reasonix-e2e.sh" || fail "reasonix e2e"
else
  echo "SKIP: reasonix e2e (set CLAUDE_CODEX_REASONIX_E2E=1 to run)"
fi

python3 "$ROOT/tests/test-reasonix-cost-ledger.py" || fail "reasonix cost ledger regression"

# Hook flavor-awareness: reasonix flavor must NOT block the native Agent tool
# (so subagents route to reasonix, not the codex_fleet MCP); codex flavor still blocks.
echo '{"tool_name":"Agent","tool_input":{"prompt":"x"}}' | CLAUDE_CODEX_FLAVOR=reasonix python3 "$ROOT/hooks/only-reasonix-fleet.py" >/dev/null 2>&1 && fail "reasonix flavor must STILL block Agent (push to reasonix MCP, not native which hangs)"
echo '{"tool_name":"Agent","tool_input":{"prompt":"x"}}' | CLAUDE_CODEX_FLAVOR=codex CLAUDE_CODEX_NATIVE_SUBAGENTS=0 python3 "$ROOT/hooks/only-reasonix-fleet.py" >/dev/null 2>&1 && fail "codex flavor must still block the Agent tool"
echo "PASS: only-codex-fleet flavor-aware"

# Like the acp test, this drives the real reasonix engine via its own fake driver
# and must not be intercepted by the suite-level GATEWAY_MOCK switch.
env -u CLAUDE_CODEX_GATEWAY_MOCK -u CLAUDE_REASONIX_GATEWAY_MOCK \
  python3 "$ROOT/tests/test-mcp-reasonix.py" || fail "mcp reasonix flavor regression"
