#!/usr/bin/env bash
set -euo pipefail

ROOT="/Users/tatlatat/.claude/codex-fleet"
LAUNCHER="/Users/tatlatat/.local/bin/claude-codex"
HOOK="$ROOT/hooks/only-codex-fleet.py"
WORKFLOW_HOOK="$ROOT/hooks/codex-workflow.py"
MCP_SERVER="$ROOT/codex-fleet-mcp.py"
GATEWAY="$ROOT/codex-native-gateway.py"
CCR_PROXY="$ROOT/ccr-claude-proxy.py"
E2E_EVIDENCE_TEST="$ROOT/tests/test-e2e-evidence.sh"
E2E_HARNESS="$ROOT/tests/e2e-tmux-claude-codex.sh"
EXPECTED_CODEX_MODEL="${CODEX_FLEET_MODEL:-gpt-5.4}"

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
assert_file "$ROOT/system-prompt.md"
assert_file "$HOOK"
assert_file "$WORKFLOW_HOOK"
assert_file "$MCP_SERVER"
assert_file "$GATEWAY"
assert_file "$CCR_PROXY"
assert_file "$E2E_EVIDENCE_TEST"
assert_file "$E2E_HARNESS"
"$E2E_EVIDENCE_TEST"

python3 - "$E2E_HARNESS" <<'PY'
from pathlib import Path
import sys

text = Path(sys.argv[1]).read_text(encoding="utf-8")
required_fragments = [
    "CODEX_E2E_MODEL",
    "CLAUDE_CODEX_CODEX_MODEL=$CODEX_E2E_MODEL",
    "CODEX_FLEET_MODEL=$CODEX_E2E_MODEL",
    "--expected-model \"$CODEX_E2E_MODEL\"",
    "-m \"$CODEX_E2E_MODEL\"",
    "CODEX_MODEL_OK",
]
for fragment in required_fragments:
    if fragment not in text:
        raise SystemExit(f"E2E harness must support model override; missing {fragment!r}")
for hard_coded in ("-m gpt-5.5", "CODEX_GPT55_OK"):
    if hard_coded in text:
        raise SystemExit(f"E2E harness still hard-codes the Codex model probe: {hard_coded}")
PY

grep -q "Non-subagent work stays in Claude Code" "$ROOT/system-prompt.md" || fail "prompt must keep non-subagent work in Claude Code"
grep -q "UltraCode/Dynamic Workflow policy" "$ROOT/system-prompt.md" || fail "prompt must define UltraCode/Dynamic Workflow policy"
grep -q "file worker, verify worker, review worker" "$ROOT/system-prompt.md" || fail "prompt must route UltraCode worker roles to Codex"
grep -q "Do not spawn Claude subagents" "$ROOT/system-prompt.md" || fail "prompt must ban Claude subagents"
grep -q "If the user asks whether your subagents are Claude or Codex" "$ROOT/system-prompt.md" || fail "prompt must answer subagent identity as Codex"
grep -q "Codex worker" "$ROOT/system-prompt.md" || fail "prompt must route subagents to Codex workers"
grep -q "run_codex_fleet" "$ROOT/system-prompt.md" || fail "prompt must route dynamic batches to run_codex_fleet"
grep -q "/fast on" "$ROOT/system-prompt.md" || fail "prompt must mention Codex fast mode"
grep -q "not automatically in UltraCode mode" "$ROOT/system-prompt.md" || fail "prompt must say UltraCode is not auto-enabled"
grep -q "when the user writes the word ultracode" "$ROOT/system-prompt.md" || fail "prompt must route ultracode keyword activation"
grep -q "preserves Claude Code's selected main model" "$ROOT/system-prompt.md" || fail "prompt must say safe mode preserves the main model"
grep -q "Workflow scripts are rewritten" "$ROOT/system-prompt.md" || fail "prompt must describe Workflow rewrite hook"
grep -q "experimental native gateway" "$ROOT/system-prompt.md" || fail "prompt must describe native gateway as experimental"
grep -q "claude-deepseek-pro" "$ROOT/system-prompt.md" || fail "prompt must mention the DeepSeek-backed native model"
grep -q "qwen36-mlx" "$ROOT/system-prompt.md" || fail "prompt must mention the local Qwen native model"
grep -q "Claude Code Router" "$ROOT/system-prompt.md" || fail "prompt must describe Claude Code Router mode"
grep -q "claude-codex router" "$ROOT/system-prompt.md" || fail "prompt must document explicit router activation"

python3 - "$ROOT/bridge-settings.json" "$WORKFLOW_HOOK" <<'PY'
import json
import sys

settings_path, workflow_hook = sys.argv[1:]
with open(settings_path, "r", encoding="utf-8") as fh:
    settings = json.load(fh)

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
    raise SystemExit("bridge settings must install the Codex Workflow rewrite hook")

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

fake_codex="$tmp_home/fake-codex"
cat >"$fake_codex" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
cat >/dev/null
printf 'OpenAI Codex v-test\n'
printf 'codex\n'
printf 'FAKE_CODEX_GATEWAY_OK\n'
printf 'hook: Stop\n'
printf 'tokens used\n1\n'
SH
chmod +x "$fake_codex"

python3 - "$GATEWAY" "$fake_codex" <<'PY'
import importlib.util
import json
import os
import sys

gateway_path, fake_codex = sys.argv[1:]
os.environ["CODEX_BIN"] = fake_codex
os.environ["CLAUDE_CODEX_CODEX_BACKEND"] = "codex-cli"
expected_model = os.environ.get("CODEX_FLEET_MODEL", "gpt-5.4")
os.environ["CLAUDE_CODEX_CODEX_MODEL"] = expected_model
os.environ.pop("CLAUDE_CODEX_GATEWAY_MOCK", None)
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("CLAUDE_CODEX_OPENAI_API_KEY", None)

spec = importlib.util.spec_from_file_location("codex_native_gateway", gateway_path)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)

config = module.model_registry()["claude-codex-pro"]
if config.get("provider") != "codex_cli":
    raise SystemExit(f"claude-codex-pro should default to codex_cli, got {config}")
if config.get("target_model") != expected_model:
    raise SystemExit(f"claude-codex-pro target model should be {expected_model}: {config}")
response = module.call_openai_chat_completion(
    {
        "model": "claude-codex-pro",
        "messages": [{"role": "user", "content": "gateway smoke"}],
    },
    "claude-codex-pro",
    config,
)
content = response["choices"][0]["message"]["content"]
if content != "FAKE_CODEX_GATEWAY_OK":
    raise SystemExit(f"gateway did not return fake Codex output: {content!r}")
PY

fake_codex_retry="$tmp_home/fake-codex-retry"
cat >"$fake_codex_retry" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
state="${FAKE_CODEX_RETRY_STATE:?missing retry state}"
count=0
if [[ -f "$state" ]]; then
  count="$(tr -d '[:space:]' <"$state")"
fi
count=$((count + 1))
printf '%s\n' "$count" >"$state"
cat >/dev/null
if (( count < 3 )); then
  printf 'ERROR: exceeded retry limit, last status: 429 Too Many Requests\n' >&2
  exit 1
fi
printf 'OpenAI Codex v-test\n'
printf 'codex\n'
printf 'RETRY_CODEX_OK\n'
printf 'hook: Stop\n'
SH
chmod +x "$fake_codex_retry"

python3 - "$GATEWAY" "$fake_codex_retry" "$tmp_home/retry-state" <<'PY'
import importlib.util
import os
import sys
from pathlib import Path

gateway_path, fake_codex, retry_state = sys.argv[1:]
os.environ["CODEX_BIN"] = fake_codex
os.environ["FAKE_CODEX_RETRY_STATE"] = retry_state
os.environ["CLAUDE_CODEX_CODEX_BACKEND"] = "codex-cli"
os.environ["CLAUDE_CODEX_GATEWAY_CODEX_MAX_ATTEMPTS"] = "3"
os.environ["CLAUDE_CODEX_GATEWAY_CODEX_RETRY_BASE_SECONDS"] = "0"
os.environ.pop("CLAUDE_CODEX_GATEWAY_MOCK", None)

spec = importlib.util.spec_from_file_location("codex_native_gateway_retry", gateway_path)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)

config = module.model_registry()["claude-codex-pro"]
text, _usage = module.run_codex_cli("retry smoke", config)
if text != "RETRY_CODEX_OK":
    raise SystemExit(f"gateway retry did not return successful Codex output: {text!r}")
attempts = int(Path(retry_state).read_text(encoding="utf-8").strip())
if attempts != 3:
    raise SystemExit(f"gateway retry should attempt exactly 3 times, got {attempts}")
PY

python3 - "$GATEWAY" <<'PY'
import importlib.util
import json
import os
import sys

gateway_path = sys.argv[1]
os.environ["CLAUDE_CODEX_CODEX_BACKEND"] = "codex-cli"
os.environ.pop("CLAUDE_CODEX_GATEWAY_MOCK", None)

spec = importlib.util.spec_from_file_location("codex_native_gateway_structured", gateway_path)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)

def fake_run_codex_cli(prompt, config):
    return ('{"marker":"STRUCTURED_CODEX_OK","ok":true}', {"input_tokens": 8, "output_tokens": 4})

module.run_codex_cli = fake_run_codex_cli
config = module.model_registry()["claude-codex-pro"]
variant_tool = module.requested_structured_output_tool(
    {"tools": [{"name": "claude_sdk_StructuredOutput"}], "tool_choice": {"type": "tool", "name": "claude_sdk_StructuredOutput"}}
)
if variant_tool != "claude_sdk_StructuredOutput":
    raise SystemExit(f"StructuredOutput detector should allow SDK-prefixed names: {variant_tool!r}")
response = module.call_openai_compatible(
    {
        "model": "claude-codex-pro",
        "max_tokens": 128,
        "tools": [
            {
                "name": "StructuredOutput",
                "description": "Return the requested structured object.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "marker": {"type": "string"},
                        "ok": {"type": "boolean"},
                    },
                    "required": ["marker", "ok"],
                },
            }
        ],
        "tool_choice": {"type": "tool", "name": "StructuredOutput"},
        "messages": [{"role": "user", "content": "return structured marker"}],
    },
    "claude-codex-pro",
    config,
)
content = response.get("content") or []
if not content or content[0].get("type") != "tool_use":
    raise SystemExit(f"expected StructuredOutput tool_use from Codex JSON: {response}")
if content[0].get("name") != "StructuredOutput":
    raise SystemExit(f"expected StructuredOutput tool name: {response}")
if content[0].get("input", {}).get("marker") != "STRUCTURED_CODEX_OK":
    raise SystemExit(f"bad StructuredOutput input: {response}")
if response.get("stop_reason") != "tool_use":
    raise SystemExit(f"expected tool_use stop reason: {response}")
chat_response = module.call_openai_chat_completion(
    {
        "model": "claude-codex-pro",
        "messages": [{"role": "user", "content": "return structured marker"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "StructuredOutput",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "marker": {"type": "string"},
                            "ok": {"type": "boolean"},
                        },
                        "required": ["marker", "ok"],
                    },
                },
            }
        ],
        "tool_choice": {"type": "function", "function": {"name": "StructuredOutput"}},
    },
    "claude-codex-pro",
    config,
)
message = (chat_response.get("choices") or [{}])[0].get("message") or {}
tool_calls = message.get("tool_calls") or []
if not tool_calls:
    raise SystemExit(f"expected OpenAI StructuredOutput tool call from Codex JSON: {chat_response}")
function = tool_calls[0].get("function") or {}
if function.get("name") != "StructuredOutput":
    raise SystemExit(f"expected StructuredOutput function call: {chat_response}")
arguments = json.loads(function.get("arguments") or "{}")
if arguments.get("marker") != "STRUCTURED_CODEX_OK":
    raise SystemExit(f"bad OpenAI StructuredOutput arguments: {chat_response}")
PY

python3 - "$GATEWAY" <<'PY'
import importlib.util
import os
import sys

gateway_path = sys.argv[1]
os.environ["CLAUDE_CODEX_CODEX_BACKEND"] = "codex-cli"
os.environ.pop("CLAUDE_CODEX_GATEWAY_MOCK", None)

spec = importlib.util.spec_from_file_location("codex_native_gateway_structured_success", gateway_path)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)

def fail_run_codex_cli(prompt, config):
    raise AssertionError("Codex CLI should not be called after StructuredOutput succeeded")

module.run_codex_cli = fail_run_codex_cli
config = module.model_registry()["claude-codex-pro"]
chat_response = module.call_openai_chat_completion(
    {
        "model": "claude-codex-pro",
        "messages": [
            {"role": "user", "content": "return structured marker"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_structured_ok",
                        "type": "function",
                        "function": {
                            "name": "StructuredOutput",
                            "arguments": "{\"marker\":\"STRUCTURED_CODEX_OK\",\"ok\":true}",
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_structured_ok",
                "content": "Structured output provided successfully",
            },
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "StructuredOutput",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "marker": {"type": "string"},
                            "ok": {"type": "boolean"},
                        },
                        "required": ["marker", "ok"],
                    },
                },
            }
        ],
    },
    "claude-codex-pro",
    config,
)
choice = (chat_response.get("choices") or [{}])[0]
message = choice.get("message") or {}
if choice.get("finish_reason") != "stop":
    raise SystemExit(f"successful StructuredOutput follow-up should stop: {chat_response}")
if message.get("tool_calls"):
    raise SystemExit(f"successful StructuredOutput follow-up should not repeat tool calls: {chat_response}")
if not str(message.get("content") or "").strip():
    raise SystemExit(f"successful StructuredOutput follow-up should return visible text: {chat_response}")

anthropic_response = module.call_openai_compatible(
    {
        "model": "claude-codex-pro",
        "tools": [{"name": "StructuredOutput", "input_schema": {"type": "object", "properties": {}}}],
        "messages": [
            {"role": "user", "content": "return structured marker"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_structured_ok",
                        "name": "StructuredOutput",
                        "input": {"marker": "STRUCTURED_CODEX_OK", "ok": True},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_structured_ok",
                        "content": "Structured output provided successfully",
                    }
                ],
            },
        ],
    },
    "claude-codex-pro",
    config,
)
if anthropic_response.get("stop_reason") != "end_turn":
    raise SystemExit(f"successful Anthropic StructuredOutput follow-up should end turn: {anthropic_response}")
if any(block.get("type") == "tool_use" for block in anthropic_response.get("content") or []):
    raise SystemExit(f"successful Anthropic StructuredOutput follow-up should not repeat tool_use: {anthropic_response}")
if not module.text_from_content(anthropic_response.get("content")).strip():
    raise SystemExit(f"successful Anthropic StructuredOutput follow-up should return visible text: {anthropic_response}")
PY

python3 - "$GATEWAY" <<'PY'
import importlib.util
import os
import sys

gateway_path = sys.argv[1]
os.environ["CLAUDE_CODEX_CODEX_BACKEND"] = "codex-cli"
os.environ.pop("CLAUDE_CODEX_GATEWAY_MOCK", None)
spec = importlib.util.spec_from_file_location("codex_native_gateway_structured_timeout", gateway_path)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)

def timeout_run_codex_cli(prompt, config):
    raise module.GatewayError(504, "codex_timeout", "codex exec timed out after 165s")

module.run_codex_cli = timeout_run_codex_cli
config = module.model_registry()["claude-codex-pro"]
anthropic_response = module.call_openai_compatible(
    {
        "model": "claude-codex-pro",
        "tools": [
            {
                "name": "StructuredOutput",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "results": {"type": "array", "items": {"type": "object"}},
                        "sourceQuality": {"type": "string"},
                    },
                    "required": ["results", "sourceQuality"],
                },
            }
        ],
        "messages": [{"role": "user", "content": "return structured output slowly"}],
    },
    "claude-codex-pro",
    config,
)
content = anthropic_response.get("content") or []
if anthropic_response.get("stop_reason") != "tool_use" or not content or content[0].get("name") != "StructuredOutput":
    raise SystemExit(f"timeout fallback should return StructuredOutput tool_use: {anthropic_response}")
if content[0].get("input", {}).get("results") != []:
    raise SystemExit(f"timeout fallback should provide empty results: {anthropic_response}")
if content[0].get("input", {}).get("sourceQuality") != "unreliable":
    raise SystemExit(f"timeout fallback should mark source quality unreliable: {anthropic_response}")
PY

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
    "model": "claude-codex-pro",
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

handler.send_sse_response_lazy(slow_producer, "claude-codex-pro")
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
for required_text in (
    "STRUCTURED OUTPUT REQUIREMENT",
    "exactly one JSON object",
    "sourceQuality",
    '"primary"',
    '"unreliable"',
    '"quote"',
    '"importance"',
    '"central"',
):
    if required_text not in openai_prompt:
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
bare_output="$("$LAUNCHER" "bare prompt")"
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
if "python3" not in args or not any(arg.endswith("codex-fleet-mcp.py") for arg in args):
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

run_output="$("$LAUNCHER" run "test prompt")"
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
grep -q "claude-codex-pro" <<<"$native_output" || fail "opt-in native gateway mode should include the Codex-backed model"
grep -q "claude-deepseek-pro" <<<"$native_output" || fail "opt-in native gateway mode should include the DeepSeek-backed model"
if grep -q -- "--disallowedTools Agent,Task" <<<"$native_output"; then
  fail "opt-in native gateway mode should not globally block Agent/Task"
fi

router_output="$("$LAUNCHER" router "router prompt")"
grep -q -- " --agents {" <<<"$router_output" || fail "router mode should pass native subagent definitions"
grep -q "claude-codex-pro" <<<"$router_output" || fail "router mode should include the Codex-backed model"
grep -q "claude-deepseek-pro" <<<"$router_output" || fail "router mode should include the DeepSeek-backed model"
grep -q "<CCR-SUBAGENT-MODEL>codex-gateway,claude-codex-pro</CCR-SUBAGENT-MODEL>" <<<"$router_output" || fail "router mode should tag Codex agents for CCR"
grep -q "<CCR-SUBAGENT-MODEL>deepseek-gateway,claude-deepseek-pro</CCR-SUBAGENT-MODEL>" <<<"$router_output" || fail "router mode should tag DeepSeek agents for CCR"
grep -q "router prompt" <<<"$router_output" || fail "router mode should forward prompt args"
if grep -q -- "--disallowedTools Agent,Task" <<<"$router_output"; then
  fail "router mode should not globally block Agent/Task"
fi

router_login_output="$("$LAUNCHER" router-login "router login prompt")"
grep -q -- " --agents {" <<<"$router_login_output" || fail "router-login mode should pass native subagent definitions"
grep -q "<CCR-SUBAGENT-MODEL>codex-gateway,claude-codex-pro</CCR-SUBAGENT-MODEL>" <<<"$router_login_output" || fail "router-login mode should tag Codex agents for CCR"
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
grep -q "CLAUDE_CODE_SUBAGENT_MODEL=claude-codex-pro" <<<"$native_env_output" || fail "native gateway mode should force built-in subagents to the Codex-backed model"

router_env_output="$(CLAUDE_BIN="$claude_env_mock" "$LAUNCHER" router "router env prompt")"
grep -q "CLAUDE_CODE_SUBAGENT_MODEL=claude-codex-pro" <<<"$router_env_output" || fail "router mode should force built-in subagents to the Codex-backed model"
grep -q "ANTHROPIC_CUSTOM_MODEL_OPTION=claude-codex-pro" <<<"$router_env_output" || fail "router mode should expose the Codex-backed custom model option"

router_env_inherit_output="$(CLAUDE_BIN="$claude_env_mock" CLAUDE_CODEX_SUBAGENT_MODEL=inherit "$LAUNCHER" router "router inherit prompt")"
grep -q "CLAUDE_CODE_SUBAGENT_MODEL=inherit" <<<"$router_env_inherit_output" || fail "router mode should honor explicit CLAUDE_CODEX_SUBAGENT_MODEL overrides"

router_qwen_env_output="$(CLAUDE_BIN="$claude_env_mock" "$LAUNCHER" router-qwen "router qwen env prompt")"
grep -q "CLAUDE_CODE_SUBAGENT_MODEL=claude-codex-pro" <<<"$router_qwen_env_output" || fail "router-qwen should still force subagents to the Codex-backed model by default"
grep -q "ANTHROPIC_BASE_URL=http://127.0.0.1:" <<<"$router_qwen_env_output" || fail "router-qwen should point Claude at the scoped CCR proxy"
grep -q "ANTHROPIC_AUTH_TOKEN=claude-codex-router" <<<"$router_qwen_env_output" || fail "router-qwen should authenticate Claude to the scoped CCR proxy"
grep -q "ANTHROPIC_CUSTOM_MODEL_OPTION=qwen36-mlx" <<<"$router_qwen_env_output" || fail "router-qwen should expose Qwen as Claude Code's custom model option"
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
if providers["codex-gateway"].get("models") != ["claude-codex-pro"]:
    raise SystemExit(f"bad Codex CCR provider: {providers['codex-gateway']}")
if providers["deepseek-gateway"].get("models") != ["claude-deepseek-pro"]:
    raise SystemExit(f"bad DeepSeek CCR provider: {providers['deepseek-gateway']}")
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
task_output="$("$LAUNCHER" task "one shot prompt")"
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
printf '{"tool_name":"Agent","tool_input":{"subagent_type":"codex-security"}}' | CLAUDE_CODEX_NATIVE_SUBAGENTS=1 python3 "$HOOK"
printf '{"tool_name":"Agent","tool_input":{"subagent_type":"deepseek-deep"}}' | CLAUDE_CODEX_NATIVE_SUBAGENTS=1 python3 "$HOOK"
if printf '{"tool_name":"Agent","tool_input":{"subagent_type":"Explore"}}' | CLAUDE_CODEX_NATIVE_SUBAGENTS=1 python3 "$HOOK" 2>/dev/null; then
  fail "native mode should still block non-Codex/non-DeepSeek agents"
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
if "codex-security" not in script:
    raise SystemExit("native Workflow hook should include a Codex security agent mapping")
if "deepseek-deep" not in script:
    raise SystemExit("native Workflow hook should include a DeepSeek deep agent mapping")
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
if "codex-security" not in script:
    raise SystemExit("router Workflow hook should route security lanes to codex-security")
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
    "codex-worker": "claude-codex-pro",
    "codex-security": "claude-codex-pro",
    "codex-reviewer": "claude-codex-pro",
    "codex-verify": "claude-codex-pro",
    "deepseek-deep": "claude-deepseek-pro",
    "deepseek-architecture": "claude-deepseek-pro",
}
for name, model in required.items():
    if agents.get(name, {}).get("model") != model:
        raise SystemExit(f"missing native agent {name} with model {model}: {agents.get(name)}")
    if agents[name].get("effort") != "xhigh":
        raise SystemExit(f"native agent {name} should use xhigh effort: {agents[name]}")
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
if not {"claude-codex-pro", "claude-deepseek-pro"}.issubset(ids):
    raise SystemExit(f"gateway did not advertise native models: {models}")

body = json.dumps({"model":"claude-codex-pro","messages":[{"role":"user","content":"hello"}]}).encode()
req = urllib.request.Request(base + "/v1/messages/count_tokens", data=body, headers={"content-type":"application/json"})
tokens = json.load(urllib.request.urlopen(req, timeout=5))
if not isinstance(tokens.get("input_tokens"), int) or tokens["input_tokens"] <= 0:
    raise SystemExit(f"bad token estimate: {tokens}")

chat_body = json.dumps({
    "model": "claude-codex-pro",
    "messages": [{"role": "user", "content": "hello from ccr"}],
}).encode()
chat_req = urllib.request.Request(
    base + "/v1/chat/completions",
    data=chat_body,
    headers={"content-type": "application/json"},
)
chat = json.load(urllib.request.urlopen(chat_req, timeout=5))
if chat.get("model") != "claude-codex-pro":
    raise SystemExit(f"chat endpoint should preserve requested alias model: {chat}")
choice = (chat.get("choices") or [{}])[0]
message = choice.get("message") or {}
if message.get("role") != "assistant" or "mock claude-codex-pro response" not in str(message.get("content")):
    raise SystemExit(f"bad chat completion response: {chat}")

messages_body = json.dumps({
    "model": "claude-codex-pro",
    "messages": [{"role": "user", "content": "hello through query string"}],
}).encode()
messages_req = urllib.request.Request(
    base + "/v1/messages?anthropic-version=2023-06-01",
    data=messages_body,
    headers={"content-type": "application/json"},
)
# codex_cli ALWAYS streams now (heartbeat path), even without stream=true, so the
# 180s workflow watchdog never fires on a slow lane. Parse the SSE message_start
# instead of expecting a single JSON blob.
resp = urllib.request.urlopen(messages_req, timeout=5)
ctype = resp.headers.get("content-type", "")
raw = resp.read().decode("utf-8")
if not ctype.startswith("text/event-stream"):
    raise SystemExit(f"codex_cli /v1/messages should stream SSE now, got content-type={ctype!r}: {raw[:200]}")
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
if model_seen != "claude-codex-pro":
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
  --models "claude-opus-4-8,claude-codex-pro,claude-deepseek-pro" \
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
required = {"claude-opus-4-8", "claude-codex-pro", "claude-deepseek-pro"}
if not required.issubset(ids):
    raise SystemExit(f"CCR proxy should expose Opus and native aliases for Claude Code discovery: {models}")
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
  --models "claude-opus-4-8,claude-codex-pro,claude-deepseek-pro" \
  --alias-models "claude-codex-pro,claude-deepseek-pro" \
  --direct-alias-models "claude-codex-pro,claude-deepseek-pro" \
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

alias = post("claude-codex-pro", "Bearer login-token")
if alias.get("route") != "direct" or alias.get("authorization") != "Bearer ccr-test-key":
    raise SystemExit(f"direct alias model should route to the native gateway target with CCR auth: {alias}")

tagged = post(
    "claude-opus-4-8",
    "Bearer login-token",
    [{"type": "text", "text": "<CCR-SUBAGENT-MODEL>codex-gateway,claude-codex-pro</CCR-SUBAGENT-MODEL>\nworker"}],
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
    or workflow_subagent.get("model") != "claude-codex-pro"
):
    raise SystemExit(
        "workflow subagent requests using the main model should be forced to the Codex-backed direct alias target: "
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

export CODEX_BIN="/bin/echo"
export CODEX_FLEET_DEFAULT_CONCURRENCY=2
python3 - "$MCP_SERVER" <<'PY'
import json
import os
import subprocess
import sys
import time

server = subprocess.Popen(
    [sys.executable, sys.argv[1]],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
)

def send(message):
    server.stdin.write(json.dumps(message) + "\n")
    server.stdin.flush()
    while True:
        line = server.stdout.readline()
        if not line:
            raise SystemExit("MCP server closed stdout")
        data = json.loads(line)
        if data.get("id") == message.get("id"):
            return data

init = send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}})
if "serverInfo" not in init.get("result", {}):
    raise SystemExit(f"bad initialize response: {init}")

tools = send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
names = {tool["name"] for tool in tools["result"]["tools"]}
expected = {"run_codex_worker", "run_codex_fleet", "fleet_status"}
if not expected.issubset(names):
    raise SystemExit(f"missing tools: {expected - names}")

batch = send({
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {
        "name": "run_codex_fleet",
        "arguments": {
            "concurrency": 2,
            "tasks": [
                {"title": "alpha", "prompt": "first dynamic task"},
                {"title": "beta", "prompt": "second dynamic task"}
            ]
        }
    }
})
text = batch["result"]["content"][0]["text"]
payload = json.loads(text)
if payload["total_tasks"] != 2:
    raise SystemExit(f"bad task count: {payload}")
if payload["concurrency"] != 2:
    raise SystemExit(f"bad concurrency: {payload}")
if [item["title"] for item in payload["results"]] != ["alpha", "beta"]:
    raise SystemExit(f"bad results: {payload}")
if not all("exec" in item["stdout"] for item in payload["results"]):
    raise SystemExit(f"fake codex command was not invoked: {payload}")
joined_stdout = "\n".join(item["stdout"] for item in payload["results"])
expected_model = os.environ.get("CODEX_FLEET_MODEL", "gpt-5.4")
required_fragments = [
    "exec",
    f"-m {expected_model}",
    "model_reasoning_effort=\"xhigh\"",
    "service_tier=\"fast\"",
    "features.fast_mode=true",
]
for fragment in required_fragments:
    if fragment not in joined_stdout:
        raise SystemExit(f"missing codex fast/xhigh fragment {fragment!r}: {joined_stdout}")

server.terminate()
try:
    server.wait(timeout=2)
except subprocess.TimeoutExpired:
    server.kill()
PY

echo "PASS: codex fleet launcher"

python3 "$ROOT/tests/test-ccr-proxy-timeout.py" || fail "ccr-proxy timeout regression"

python3 "$ROOT/tests/test-workflow-selfheal.py" || fail "workflow self-heal regression"

python3 "$ROOT/tests/test-gateway-nonstream-heartbeat.py" || fail "gateway non-stream heartbeat regression"

python3 "$ROOT/tests/test-ccr-proxy-streaming.py" || fail "ccr-proxy SSE streaming regression"

python3 "$ROOT/tests/test-reasonix-acp.py" || fail "reasonix acp driver regression"
