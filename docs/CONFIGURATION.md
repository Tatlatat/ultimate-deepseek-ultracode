# Configuration — claude-reasonix

Every `CLAUDE_REASONIX_*` variable has a `CLAUDE_CODEX_*` backward-compat alias (checked
second, after the primary name). The launcher (`bin/claude-reasonix`) exports both names so
either works. Where a flag has neither a `CLAUDE_REASONIX_*` nor a `CLAUDE_CODEX_*` form the
alias column is shown as `—`.

**Default-ON flags:** The five *promoted levers* (see §Promoted levers) have a raw source
default of `0` but the launcher unconditionally sets them to `1` via `:=` (respects user
overrides). In practice they are **always on** unless you explicitly export the flag to `0`
before launching.

You do **not** need any of these for normal use — see the README's Configuration section for
the handful that matter in day-to-day operation.

---

## User-facing (the ones you might actually set)

| Flag | Default | Effect |
|---|---|---|
| `CLAUDE_REASONIX_ANTHROPIC_API_KEY` | (falls through to `ANTHROPIC_API_KEY`) | Anthropic API key for the router / orchestrator; preferred over bare `ANTHROPIC_API_KEY` |
| `CLAUDE_REASONIX_FLAVOR` | `"reasonix"` | Selects active flavor (`reasonix` is the only live flavor; `codex` is banned) |
| `CLAUDE_REASONIX_MODEL` | `"deepseek-v4-flash"` | Model name written into the system-prompt MCP tool list shown to Claude Code |
| `CLAUDE_REASONIX_REASONIX_MODEL` | `"deepseek-v4-flash"` | DeepSeek model id forwarded to the reasonix CLI for each lane |
| `CLAUDE_REASONIX_REASONIX_EFFORT` | `"high"` | Effort level passed to the reasonix CLI (`low`/`medium`/`high`) |
| `CLAUDE_REASONIX_REASONIX_BUDGET` | `"0.05"` | Per-lane budget cap (USD) passed to the reasonix CLI |
| `CLAUDE_REASONIX_FLEET_DEFAULT_WORKERS` | `16` | Number of concurrent worker slots advertised to Claude Code |
| `CLAUDE_REASONIX_NATIVE_SUBAGENTS` | `0` (launcher sets `1` for reasonix flavor) | Enable native subagent fan-out mode (parallel phase fan-out instead of serial MCP) |
| `CLAUDE_REASONIX_GATEWAY_TIMEOUT` | `"600"` | Per-lane timeout in seconds (also accepts `REASONIX_FLEET_TIMEOUT_SECONDS`) |
| `CLAUDE_REASONIX_GATEWAY_CONCURRENCY` | `16` | Max simultaneous lanes the gateway will run |
| `CLAUDE_REASONIX_GATEWAY_ANTHROPIC_BASE_URL` | `"https://api.anthropic.com"` | Anthropic API base URL (override for custom proxies) |
| `CLAUDE_REASONIX_GATEWAY_ANTHROPIC_AUTH_TOKEN` | (from env `ANTHROPIC_AUTH_TOKEN`) | Bearer token for Anthropic API; checked before `ANTHROPIC_AUTH_TOKEN` |
| `CLAUDE_REASONIX_GATEWAY_ANTHROPIC_API_KEY` | (from env `ANTHROPIC_API_KEY`) | API key for Anthropic; checked before bare `ANTHROPIC_API_KEY` |
| `CLAUDE_REASONIX_GATEWAY_HOST` | `"127.0.0.1"` | Address the gateway HTTP server binds to |
| `CLAUDE_REASONIX_GATEWAY_PORT` | `0` (random OS-assigned) | Port the gateway HTTP server listens on |
| `CLAUDE_REASONIX_GATEWAY_LANE_HARNESS` | `0` | Enable the weak-executor retry harness (C3) — retries failing lanes up to `LANE_MAX_ATTEMPTS` times within `LANE_BUDGET_USD` |

---

## Promoted levers (default-ON via launcher)

These five levers are measured and stable. The launcher sets them to `1` by default; override
to `0` to disable.

| Flag | Launcher default | Effect |
|---|---|---|
| `CLAUDE_REASONIX_GATEWAY_READ_SUMMARY` | **1** | Lever A — caps read-lane output to a compact summary (max `READ_SUMMARY_MAX_TOKENS` tokens); dramatically reduces output tokens for file-read lanes |
| `CLAUDE_REASONIX_GATEWAY_READER_BROADEN` | **1** | Routes read-heavy verbs (analyze/review/audit/examine/…) to the reader bucket so the summary cap applies |
| `CLAUDE_REASONIX_GATEWAY_READ_RETRY_HOLLOW` | **1** | When a summary-capped read lane returns empty, automatically retries with a higher cap; active only when `READ_SUMMARY` is on |
| `CLAUDE_REASONIX_GATEWAY_LANE_FAIL_MARKER` | **1** | Injects a `[LANE_FAILED]` marker into the orchestrator context when a lane fails so Claude Code can react; replaces silent failure |
| `CLAUDE_REASONIX_GATEWAY_OVERSCOPE_REJECT` | **1** | Lever G — rejects bulk "read everything" lane requests that exceed `OVERSCOPE_MAX_FILES` files, forcing decomposition |

---

## Cache / prefix

| Flag | Default | Effect |
|---|---|---|
| `CLAUDE_REASONIX_GATEWAY_KEEPALIVE` | `"1"` | Enable SSE keepalive heartbeat to prevent 180 s gateway timeout on long lanes |
| `CLAUDE_REASONIX_GATEWAY_KEEPALIVE_HEAD` | `8192` | Number of bytes at the start of a response used for the keepalive preamble |
| `CLAUDE_REASONIX_GATEWAY_KEEPALIVE_INTERVAL_SECONDS` | `120.0` | Interval between SSE keepalive comments (seconds) |
| `CLAUDE_REASONIX_GATEWAY_KEEPALIVE_WINDOW_SECONDS` | `600.0` | Total window during which keepalives are sent (seconds) |
| `CLAUDE_REASONIX_GATEWAY_STREAM_KEEPALIVE_SECONDS` | `10.0` | Interval for the stream-level keepalive emitted on streaming lanes (seconds) |
| `CLAUDE_REASONIX_GATEWAY_PRIME_GATE` | `"1"` | Enable the prime-gate: dispatch one real primer lane first and await it before fanning out, warming the shared prefix cache |
| `CLAUDE_REASONIX_GATEWAY_PRIME_WAIT_SECONDS` | `20.0` | How long to wait for the primer lane to complete before releasing the rest of the burst |
| `CLAUDE_REASONIX_GATEWAY_PRIME_GRACE_SECONDS` | `4.0` | Grace window after the primer before the burst is considered cold (seconds) |
| `CLAUDE_REASONIX_GATEWAY_PRIME_SERIAL` | `3` | Number of lanes to serialize within each prefix family to avoid prime-gate races |
| `CLAUDE_REASONIX_GATEWAY_PRIME_SERIAL_SETTLE_SECONDS` | `4.0` | Settle delay between serialized prime lanes (seconds) |
| `CLAUDE_REASONIX_GATEWAY_PRIME_KEY_HEAD` | `4096` | Bytes from the start of the prompt used to compute the prefix-family key (SHA-1); also accepts legacy alias `GATEWAY_PRIME_HEAD_BYTES` |
| `CLAUDE_REASONIX_GATEWAY_PRIME_HEAD_BYTES` | (alias for `PRIME_KEY_HEAD`) | Legacy alias — prefer `CLAUDE_REASONIX_GATEWAY_PRIME_KEY_HEAD` |
| `CLAUDE_REASONIX_GATEWAY_PRIME_DICT_CAP` | `2048` | Max number of prefix-family entries kept in the prime-gate dictionaries before FIFO eviction |
| `CLAUDE_REASONIX_GATEWAY_PREFIX_TRACE` | `""` (off) | Enable prefix-cache diagnostic logging (logs prefix hash, cache hit/miss per lane) |
| `CLAUDE_REASONIX_GATEWAY_READ_SUMMARY_CACHE` | `"0"` | Enable in-process read-summary cache (avoids re-summarising identical files within a session) |
| `CLAUDE_REASONIX_GATEWAY_READ_SUMMARY_CACHE_CAP` | `512` | Max entries in the in-process read-summary cache |
| `CLAUDE_REASONIX_GATEWAY_READ_SUMMARY_CACHE_TTL_S` | `300.0` | TTL for read-summary cache entries (seconds) |
| `CLAUDE_REASONIX_GATEWAY_READ_SUMMARY_CACHE_MAX_BYTES` | `131072` | Max byte size of a single entry stored in the read-summary cache |
| `CLAUDE_REASONIX_GATEWAY_READ_SUMMARY_CACHE_PATH` | `""` (none) | Persistent file path for the read-summary cache; empty = in-memory only |
| `CLAUDE_REASONIX_GATEWAY_RETRY_EMPTY` | `"burst"` | Strategy for retrying empty lane results: `"burst"` retries only cold mid-burst lanes; `"always"` retries every empty |

---

## Harness

| Flag | Default | Effect |
|---|---|---|
| `CLAUDE_REASONIX_GATEWAY_LANE_HARNESS` | `0` | Enable weak-executor harness (retries the whole lane on failure up to `LANE_MAX_ATTEMPTS` times) |
| `CLAUDE_REASONIX_GATEWAY_LANE_BUDGET_USD` | `0.05` | Per-lane USD budget cap enforced by the harness |
| `CLAUDE_REASONIX_GATEWAY_LANE_MAX_ATTEMPTS` | `4` | Max retry attempts per lane when the harness is on |
| `CLAUDE_REASONIX_GATEWAY_LANE_RESET_ON_SUCCESS` | `"1"` | After a successful lane, reset the attempt counter (allows the next lane to start fresh); set `0` for legacy monotonic never-reset behavior |
| `CLAUDE_REASONIX_GATEWAY_MAX_LANE_RETRIES` | `3` | Max low-level retries for transient network/spawning failures (separate from harness attempts) |
| `CLAUDE_REASONIX_GATEWAY_MAX_ATTEMPTS` | `3` | Max overall attempts for a single gateway request cycle |
| `CLAUDE_REASONIX_GATEWAY_MAX_ITER_PER_TURN` | `50` | Max reasonix agentic iterations allowed per turn |
| `CLAUDE_REASONIX_GATEWAY_HOLLOW_GUARD` | `"1"` | Detect and reject hollow (zero-content) responses before returning them to the orchestrator |
| `CLAUDE_REASONIX_GATEWAY_CONTEXT_GUARD` | `"1"` | Enable context-window guard; rejects lanes whose prompt would exceed the model's context limit |
| `CLAUDE_REASONIX_GATEWAY_READ_SUMMARY_MAX_TOKENS` | `512` | Hard output token cap applied to read lanes when `READ_SUMMARY` is on |
| `CLAUDE_REASONIX_GATEWAY_READ_RETRY_HOLLOW` | `"1"` | (see Promoted levers) Retry a `READ_SUMMARY`-capped lane that returns empty content |
| `CLAUDE_REASONIX_GATEWAY_READ_RETRY_CAP_MULT` | `2` | Token-cap multiplier applied on each hollow-retry escalation |
| `CLAUDE_REASONIX_GATEWAY_READ_RETRY_MAX_ESCALATIONS` | `3` | Max number of cap-escalation retries for a hollow read lane |

---

## Experimental levers

| Flag | Default | Effect |
|---|---|---|
| `CLAUDE_REASONIX_PREINDEX` | `0` (falsy) | Enable pre-indexing of the project file tree before lane dispatch |
| `CLAUDE_REASONIX_PREINDEX_TIMEOUT` | `120.0` | Timeout for the pre-indexing step (seconds) |
| `CLAUDE_REASONIX_PREFETCH_CONTEXT` | `"off"` | Enable context prefetch mode: `"off"`, `"eager"`, or `"lazy"` |
| `CLAUDE_REASONIX_GATEWAY_MAPREDUCE_SYNTHESIS` | `"1"` | Enable map-reduce synthesis routing for large-prompt synthesis lanes (aggregates/merges results across many sources) |
| `CLAUDE_REASONIX_GATEWAY_MAPREDUCE_MIN_PROMPT` | `20000` | Minimum prompt token count required to trigger map-reduce synthesis routing |
| `CLAUDE_REASONIX_GATEWAY_OUTPUT_DISCIPLINE` | `"0"` | Enable output-discipline lever: enforces per-bucket max-token caps on lane outputs |
| `CLAUDE_REASONIX_GATEWAY_OUTPUT_DISCIPLINE_DIRECTIVE` | `"1"` | When output-discipline is on, inject a system directive into the prompt requesting concise output |
| `CLAUDE_REASONIX_GATEWAY_OUTPUT_DISCIPLINE_MAX_TOKENS_READ` | `512` | Max output tokens for read-bucket lanes when output-discipline is on |
| `CLAUDE_REASONIX_GATEWAY_OUTPUT_DISCIPLINE_MAX_TOKENS_EDIT` | `5900` | Max output tokens for edit-bucket lanes when output-discipline is on |
| `CLAUDE_REASONIX_GATEWAY_OUTPUT_DISCIPLINE_MAX_TOKENS_DEFAULT` | `2048` | Max output tokens for all other lanes when output-discipline is on |
| `CLAUDE_REASONIX_GATEWAY_OVERSCOPE_MAX_FILES` | `10` | File-count threshold above which a lane is considered over-scoped and rejected (when `OVERSCOPE_REJECT` is on) |
| `CLAUDE_REASONIX_NATIVE_AGENT_TOOLS` | `"Read,Grep,Glob,Bash,Edit,Write,MultiEdit"` | Comma-separated list of tools allowed for native subagents in native fan-out mode |
| `CLAUDE_REASONIX_NATIVE_REASONIX_MODEL` | `"claude-reasonix-flash"` | Model id used for native subagents in native fan-out mode |
| `CLAUDE_REASONIX_GATEWAY_BACKEND` | (always `reasonix`) | Internal backend selector; included for completeness — `codex` is permanently banned |
| `CLAUDE_REASONIX_WORKFLOW_MODE` | `"fleet"` | Workflow dispatch mode: `"fleet"` (serial MCP) or `"native"` / `"gateway"` (parallel fan-out); the launcher sets `"native"` for the reasonix flavor |
| `CLAUDE_REASONIX_WORKFLOW_REWRITE` | `"1"` | Enable Workflow tool rewrite hook; set `0` to bypass the gateway rewrite of Workflow calls |
| `CLAUDE_REASONIX_WORKFLOW_PREFIX_GUIDE` | `"1"` | Inject a prefix-cache guide into Workflow `additionalContext` to improve cache hit rate across fan-out lanes (A/B measured +5.4 pts) |

---

## Internal / diagnostic

| Flag | Default | Effect |
|---|---|---|
| `CLAUDE_REASONIX_FLEET_HOME` | `$INSTALL_HOME` (`~/.claude/reasonix-fleet`) | Root directory for fleet state, runtime files, and debug logs |
| `CLAUDE_REASONIX_FLEET_INSTALL_HOME` | `~/.claude/reasonix-fleet` | Installation root; used to locate vendored reasonix engine dist |
| `CLAUDE_REASONIX_NODE_BIN` | `"node"` | Path to the Node.js binary used to spawn the reasonix engine (also accepts bare `NODE_BIN`) |
| `CLAUDE_REASONIX_GATEWAY_CWD` | `os.getcwd()` at startup | Working directory the gateway and reasonix lanes are spawned in |
| `CLAUDE_REASONIX_GATEWAY_DEBUG` | `""` (off) | Enable gateway debug logging (verbose request/response traces) |
| `CLAUDE_REASONIX_GATEWAY_TRACE` | `""` (off) | Enable low-level gateway trace logging (every SSE chunk logged) |
| `CLAUDE_REASONIX_REASONIX_COST_LEDGER` | `$FLEET_HOME/runtime/reasonix-cost.jsonl` | Path to the JSONL cost ledger where per-lane token/cost entries are appended |
| `CLAUDE_REASONIX_REASONIX_DISPLAY_NAME` | `"claude-reasonix-flash"` | Display name reported in the MCP tool capability list for the reasonix lane provider |
| `CLAUDE_REASONIX_SUBAGENT_MODEL` | `"claude-reasonix-flash"` | Model id forwarded as `CLAUDE_CODE_SUBAGENT_MODEL` when spawning native subagents (bin launcher only) |
| `CLAUDE_REASONIX_GATEWAY_STRUCTURED_DEBUG` | `""` (off) | Enable structured-output debug logging (logs StructuredOutput tool call details) |
| `CLAUDE_REASONIX_GATEWAY_QUIET` | `"1"` | Suppress non-essential gateway startup messages (set `0` for verbose startup) |
| `CLAUDE_REASONIX_GATEWAY_MOCK` | `""` (off) | Enable mock mode: the gateway returns a synthetic response without spawning reasonix |
| `CLAUDE_REASONIX_GATEWAY_MOCK_REASONIX_TEXT` | (unset) | Text the mock gateway returns as the reasonix response body (requires `GATEWAY_MOCK=1`) |
| `CLAUDE_REASONIX_LANE_SYSTEM` | `""` | Override system prompt text injected into lane requests (used for testing) |
| `CLAUDE_REASONIX_PS_FIXTURE` | (unset) | Path to a fixture file whose contents replace the live MCP system prompt (for testing) |
