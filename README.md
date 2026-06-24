# Claude Reasonix Fleet

Keep Claude Code as your main agent, but run subagent-style work — Workflow/UltraCode
fan-out lanes, agent teams, batch tasks — on **DeepSeek v4-flash via the Reasonix CLI**
instead of burning Claude tokens. Default mode is safe: it does not change Claude
Code's selected main model or set a process-wide LLM gateway.

The fleet is a small launcher + a local Anthropic-compatible gateway + Claude Code hooks
that route each `agent()` lane to `claude-reasonix-flash`, a model alias backed by
`reasonix acp` (DeepSeek through your existing Reasonix login — no OpenAI or DeepSeek
HTTP key needed).

## Requirements

The installer checks these and tells you what's missing; it never installs them for you:

- **Python 3.8+** (`python3`)
- **Claude Code CLI** (`claude`) — https://claude.com/claude-code
- **Reasonix CLI** (`reasonix`) on PATH, or pass `REASONIX_BIN=/path/to/reasonix`
- **node** (ships beside reasonix; the gateway needs it on PATH)

## Install

```bash
git clone https://github.com/<you>/claude-reasonix-fleet.git
cd claude-reasonix-fleet
./install.sh
```

`install.sh` is idempotent — re-run it any time (e.g. after upgrading reasonix). It:

1. checks the requirements above,
2. copies the fleet into `~/.claude/reasonix-fleet`,
3. installs the launcher to `~/.local/bin/claude-reasonix` (warns if that dir is not on PATH),
4. applies the Reasonix ACP ephemeral-session patch (see below),
5. smoke-checks the install with the launcher's own `doctor`.

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
claude-reasonix router       # explicit Claude Code Router native-subagent mode
claude-reasonix router-qwen  # router mode with local Qwen3.6 as the main model
claude-reasonix plain        # raw Claude, no fleet
claude-reasonix doctor       # validate files and local commands
```

## How it routes

In default safe mode the launcher generates `runtime/mcp.json` with one MCP server,
`reasonix_fleet`, and a `PreToolUse` hook rewrites each Workflow `agent()` lane to
dispatch through that MCP — so fan-out runs on DeepSeek while Claude keeps its normal
tools, skills, plugins, auth, and selected model (e.g. `claude-opus-4-8`). Generic
Claude subagents are blocked by hook policy and replaced by Reasonix Fleet workers.

In **router** / **native** modes the same hook instead sets each lane's `agentType` to
one of the native `reasonix-worker` / `reasonix-security` / `reasonix-reviewer` /
`reasonix-verify` agents (all backed by `claude-reasonix-flash`), giving the parallel
phase fan-out. Router mode also sets `CLAUDE_CODE_SUBAGENT_MODEL=claude-reasonix-flash`
so built-in agent teams (e.g. `/deep-research`) inherit the Reasonix alias. Router mode
needs an Anthropic API key/token to preserve the main-model route; safe mode and raw
`claude` do not.

## The Reasonix ACP patch

claude-reasonix fans out many concurrent `reasonix acp` lanes. Stock reasonix names each
acp session by a minute-granular timestamp, so lanes that start in the same minute share
a session and load each other's history — inflating tokens and wrecking the prompt cache
(measured: fan-out cache stuck at 60–94%). The patch gates the session name on
`REASONIX_ACP_EPHEMERAL_SESSION=1` (which the launcher exports) so each lane uses an
independent session; steady-state fan-out cache then reaches the high-90s.

`install.sh` applies it via `patches/apply_ephemeral.py`. It edits reasonix's compiled
`dist/cli/acp-*.js`, so **a reasonix upgrade reverts it** — just re-run `./install.sh`.
The script is idempotent and can be run directly:

```bash
python3 patches/apply_ephemeral.py            # apply (or no-op if already patched)
python3 patches/apply_ephemeral.py --revert   # undo
```

See `patches/ephemeral-session.md` for the full rationale and the exact edit.

## Defaults

Per-task MCP settings (read by the Fleet MCP), overridable via env:

```bash
REASONIX_FLEET_MODEL=gpt-5.4
REASONIX_FLEET_REASONING=xhigh
REASONIX_FLEET_SERVICE_TIER=fast
REASONIX_FLEET_WEB_SEARCH=live
REASONIX_FLEET_SANDBOX=workspace-write
REASONIX_FLEET_APPROVAL=never
CLAUDE_REASONIX_FLEET_DEFAULT_WORKERS=16
CLAUDE_REASONIX_CCR_MODEL=claude-reasonix-flash       # router worker model
CLAUDE_REASONIX_ROUTER_MAIN_MODEL=claude-opus-4-8     # router main model
```

Every `CLAUDE_REASONIX_*` variable has a `CLAUDE_CODEX_*` backward-compat fallback, so a
shell that exports the old names still works.

No external API key is needed for worker lanes — the gateway spawns `reasonix acp` using
the Reasonix login already present in your terminal session.

## Uninstall

```bash
./uninstall.sh                  # remove the launcher and fleet code (keep logs/ledgers)
./uninstall.sh --purge          # also delete runtime logs/ledgers/state
./uninstall.sh --revert-patch   # also undo the reasonix ACP patch
```

reasonix, claude, and node are left untouched (the installer never installed them).

## Layout

```
bin/claude-reasonix          the launcher
reasonix-native-gateway.py   local Anthropic-compatible gateway (reasonix_cli provider)
reasonix-fleet-mcp.py        the reasonix_fleet MCP server (batch + worker tools)
ccr-claude-proxy.py          Claude Code Router compatibility proxy (router mode)
hooks/                       Workflow rewrite + subagent-policy hooks
bridge-settings.json         Claude settings template (__INSTALL_HOME__ rendered at run)
system-prompt-reasonix.md    the reasonix-flavor system prompt
patches/apply_ephemeral.py   the reasonix ACP ephemeral-session patcher
install.sh / uninstall.sh    install / uninstall
tests/                       the test suite (run: bash tests/test-reasonix-fleet.sh)
runtime/realworld-bench.py   end-to-end quality + cache benchmark
```
