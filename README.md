# Claude Reasonix Fleet

Keep Claude Code as your main agent, but run subagent-style work — Workflow/UltraCode
fan-out lanes, agent teams, batch tasks — on **DeepSeek v4-flash via the Reasonix CLI**
instead of burning Claude tokens. Default mode is safe: it does not change Claude
Code's selected main model or set a process-wide LLM gateway.

The fleet is a small launcher + a local Anthropic-compatible gateway + Claude Code hooks
that route each `agent()` lane to `claude-reasonix-flash`, a model alias backed by an
**in-process DeepSeek engine** — the owner's fork (built using ideas from reasonix),
bundled with the fleet and called as a Node library, no separate CLI to install.

## The engine

The DeepSeek engine is a self-contained fork (`deepseek-reasonix-engine`), shipped as a
prebuilt bundle under `vendor/reasonix-engine/` and committed in this repo — it *is* the
shipped engine. Each lane runs through a one-shot Node shim (`engine/run-lane.mjs`) that
imports the bundle and drives ONE DeepSeek turn, printing the lane's `{text, usage, cost}`
back to the gateway. There is **no upstream reasonix install** and **no in-place patch** —
the fork carries the ephemeral-session / cache behavior natively (see "Cache & ephemeral
sessions" below).

## Requirements

The installer checks these and tells you what's missing; it never installs them for you:

- **Python 3.8+** (`python3`)
- **Claude Code CLI** (`claude`) — https://claude.com/claude-code
- **node** 18+ — the engine runs in-process via a Node shim
- **A DeepSeek credential** — either `DEEPSEEK_API_KEY` in your env, or a
  `~/.reasonix/config.json` (the engine falls back to it automatically)

## Install

```bash
git clone https://github.com/Tatlatat/ultimate-deepseek-ultracode.git
cd ultimate-deepseek-ultracode
./install.sh
```

`install.sh` is idempotent — re-run it any time. It:

1. checks the requirements above,
2. **prompts for your DeepSeek API key** if you don't have one yet (get one at
   <https://platform.deepseek.com/api_keys>) and saves it to `~/.reasonix/config.json` —
   or set `DEEPSEEK_API_KEY` in your env beforehand to skip the prompt,
3. copies the fleet **and the bundled fork engine** into `~/.claude/reasonix-fleet`,
4. installs the launcher to `~/.local/bin/claude-reasonix` (warns if that dir is not on PATH),
5. smoke-checks the install with the launcher's own `doctor` (node + bundled engine + auth).

If `~/.local/bin` is not on your PATH, add `export PATH="$HOME/.local/bin:$PATH"` to your
shell rc and restart your shell.

## Quick start

```bash
claude-reasonix "summarize this repo"   # one prompt, fleet mode
claude-reasonix                          # interactive, fleet mode
claude-reasonix on                       # enable fleet mode, then run claude normally
```

Type `ultracode` in a prompt (or run a Workflow) and the fan-out lanes route to
DeepSeek-flash automatically.

## Commands

```bash
claude-reasonix on [N]       # enable fleet mode (optional default concurrency N)
claude-reasonix off          # disable fleet mode
claude-reasonix status       # show mode and worker count
claude-reasonix workers N    # set default concurrent Reasonix tasks
claude-reasonix task "..."   # run one Claude task through the fleet, then auto-disable
claude-reasonix run          # start Claude in Reasonix Fleet mode (default)
claude-reasonix plain        # raw Claude, no fleet
claude-reasonix doctor       # validate files and local commands
```

## How it routes

In default safe mode the launcher generates `runtime/mcp.json` with one MCP server,
`reasonix_fleet`, and a `PreToolUse` hook rewrites each Workflow `agent()` lane to
dispatch through that MCP — so fan-out runs on DeepSeek while Claude keeps its normal
tools, skills, plugins, auth, and selected model (e.g. `claude-opus-4-8`). Generic
Claude subagents are blocked by hook policy and replaced by Reasonix Fleet workers.

## Cache & ephemeral sessions

claude-reasonix fans out many concurrent lanes. If lanes share a persisted session they
load each other's history — inflating tokens and wrecking the prompt cache (measured:
fan-out cache stuck at 60–94%). The fork engine runs every lane with an **ephemeral
session** natively: each lane is a one-shot in-process turn with `session: undefined`, so
there is zero on-disk session state and no history bleed between lanes. Combined with the
gateway's prefix prime-gate (which keeps the shared prefix warm in DeepSeek's server-side
cache), steady-state fan-out cache reaches the high-90s and shared-prefix review hits the
99%+ target.

This is built into the bundled engine — there is no in-place patch to apply and nothing
that a tool upgrade can revert.

## Defaults

Per-task MCP settings (read by the Fleet MCP), overridable via env:

```bash
REASONIX_FLEET_MODEL=deepseek-v4-flash
REASONIX_FLEET_REASONING=xhigh
REASONIX_FLEET_SERVICE_TIER=fast
REASONIX_FLEET_WEB_SEARCH=live
REASONIX_FLEET_SANDBOX=workspace-write
REASONIX_FLEET_APPROVAL=never
CLAUDE_REASONIX_FLEET_DEFAULT_WORKERS=16
```

Every `CLAUDE_REASONIX_*` variable has a `CLAUDE_CODEX_*` backward-compat fallback, so a
shell that exports the old names still works.

Worker lanes authenticate with `DEEPSEEK_API_KEY` if set, otherwise the bundled engine
falls back to the DeepSeek credential in `~/.reasonix/config.json` — so on a logged-in
machine no separate key export is needed.

## Configuration

The flags you are likely to ever set:

```bash
# Auth
DEEPSEEK_API_KEY=sk-...              # DeepSeek API key; falls back to ~/.reasonix/config.json

# Fleet on/off and concurrency (also settable via `claude-reasonix on/off/workers`)
CLAUDE_REASONIX_FLEET_DEFAULT_WORKERS=16   # concurrent lane slots (default: 16)

# Hard-task harness — retries failing lanes; off by default
CLAUDE_REASONIX_GATEWAY_LANE_HARNESS=1     # enable weak-executor retry harness (default: 0)

# Promoted levers — these are default-ON (the launcher sets them to 1)
CLAUDE_REASONIX_GATEWAY_READ_SUMMARY=1       # cap read-lane output to a compact summary
CLAUDE_REASONIX_GATEWAY_READER_BROADEN=1     # route analyze/review/audit verbs to reader bucket
CLAUDE_REASONIX_GATEWAY_READ_RETRY_HOLLOW=1  # retry an empty summary-capped read lane
CLAUDE_REASONIX_GATEWAY_LANE_FAIL_MARKER=1   # inject [LANE_FAILED] marker on lane failure
CLAUDE_REASONIX_GATEWAY_OVERSCOPE_REJECT=1   # reject bulk "read everything" over-scoped lanes
```

The MCP settings in the Defaults section above (`REASONIX_FLEET_MODEL`, `REASONIX_FLEET_REASONING`,
`REASONIX_FLEET_DEFAULT_WORKERS`, etc.) are the other knobs for normal day-to-day tuning.

## Uninstall

```bash
./uninstall.sh                  # remove the launcher and fleet code (keep logs/ledgers)
./uninstall.sh --purge          # also delete runtime logs/ledgers/state
```

claude and node are left untouched (the installer never installed them). There is no
in-place engine patch to revert — the engine is bundled, not patched into another tool.

## Advanced configuration

≈70 further internal/experimental levers exist (cache-tuning, prime-gate, prefetch,
output-discipline, etc.) — all default-OFF and not needed for normal use. See
[docs/CONFIGURATION.md](docs/CONFIGURATION.md) for the full reference.

## Layout

```
bin/claude-reasonix          the launcher
reasonix-native-gateway.py   thin shim (kept for the stable import path)
reasonix_gateway/            the gateway package: env, text, harness, cost,
                             levers, engine_seam, server modules
reasonix-fleet-mcp.py        the reasonix_fleet MCP server (batch + worker tools)
hooks/                       Workflow rewrite + subagent-policy hooks
bridge-settings.json         Claude settings template (__INSTALL_HOME__ rendered at run)
system-prompt-reasonix.md    the reasonix-flavor system prompt
engine/run-lane.mjs          one-shot Node shim that drives the in-process engine
vendor/reasonix-engine/      the bundled fork engine (self-contained dist + grammars + tokenizer)
install.sh / uninstall.sh    install / uninstall
tests/                       the test suite (run: bash tests/test-reasonix-fleet.sh)
runtime/realworld-bench.py   end-to-end quality + cache benchmark
```
