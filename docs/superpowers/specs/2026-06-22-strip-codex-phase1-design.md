# Strip Codex — Phase 1 (remove code) — Design

**Goal:** Remove all codex-engine and direct-DeepSeek-HTTP code from the codex-fleet infrastructure so the gateway runs a single engine (`reasonix_cli`), without changing reasonix behavior. Names (`codex-fleet`, `CLAUDE_CODEX_*` env, `codex_fleet` MCP, file names) are kept as-is — renaming is a separate Phase 2.

**Why:** The codex flavor drove a personal ChatGPT subscription programmatically through this gateway, which got the OpenAI account deactivated for "Cyber Abuse" (see memory `codex-flavor-banned-toS`). Codex is operationally disabled already (auth removed, launcher guard, gateways killed). This phase deletes the now-dead codex code so only the supported reasonix (DeepSeek-API-via-Reasonix-CLI) path remains.

**Architecture:** Surgical removal guided by one rule, verified against the reasonix code, distinguishing two kinds of "openai/codex"-named code:
- **DELETE** — code that *invokes a non-reasonix engine*: `run_codex_cli`, `codex_cli_semaphore`, `extract_codex_final_text`, `retryable_codex_cli_failure`, `mock_openai_chat_response`, the codex_cli and deepseek-HTTP branches inside the two big `call_*` functions, and the `claude-codex-pro` + `claude-deepseek-pro` registry entries.
- **KEEP** — shape-conversion code the reasonix path reuses despite its "openai" name: `anthropic_messages_to_openai`, `openai_messages_to_prompt`, `openai_response_to_anthropic`, `provider_chat_payload`, the structured-output helpers, plus the reasonix engine itself (`run_reasonix_acp`, `append_reasonix_cost`, `summarize_reasonix_cost`).

This boundary was established by reading the reasonix dispatch branches: `call_openai_compatible`'s reasonix_cli branch calls `anthropic_messages_to_openai`, `openai_messages_to_prompt`, `openai_response_to_anthropic`, `anthropic_end_turn_response`; `call_openai_chat_completion`'s reasonix_cli branch calls `openai_messages_to_prompt` + `run_reasonix_acp` + `append_reasonix_cost`. Those functions are shared, not codex-only.

**Tech Stack:** Python 3 (gateway, MCP, hooks), bash (launcher, tests). No new dependencies.

## Global Constraints

- Phase 1 ONLY removes code and prunes branches. It does NOT rename any file, env var (`CLAUDE_CODEX_*`), MCP tool (`run_codex_worker`/`run_codex_fleet`), or the repo — that is Phase 2.
- The reasonix request path must not change behavior. After every gateway step, `python3 tests/test-reasonix-acp.py` must still print `PASS`.
- Shape-conversion functions keep their current names in Phase 1 (renaming them touches call sites and belongs to Phase 2).
- The gateway must advertise ONLY `claude-reasonix-flash` after this phase; a request for any other model returns a clean `400 unknown model`, never a crash.
- The launcher (`~/.local/bin/claude-codex`) is NOT git-tracked; back it up to `claude-codex.pre-gd1` before editing.
- Each task is one commit with its own verification, so any task can be reverted independently.

---

## Safety net (verification primitives reused by every task)

- `tests/test-reasonix-acp.py` — unit test with a FAKE reasonix binary (runs offline, no real reasonix CLI). Imports `codex-native-gateway.py` and exercises `run_reasonix_acp`. Currently PASSES. This is the per-step gate for gateway changes.
- `tests/test-mcp-reasonix.py`, `tests/test-reasonix-cost-ledger.py` — reasonix MCP + cost ledger unit tests (offline).
- Live smoke (after launcher/MCP changes): start `claude-reasonix router-login`, send one `/v1/chat/completions` request with model `claude-reasonix-flash`, expect HTTP 200 with non-empty content (the subagent-lane path).

---

## Component 1 — Gateway (`codex-native-gateway.py`)

The central, highest-risk file. Remove in this order; run `tests/test-reasonix-acp.py` after each step (must stay PASS).

**G1 — `model_registry()`:** delete the `claude-codex-pro` and `claude-deepseek-pro` entries and the `codex_provider`/`codex_backend` locals; keep only `claude-reasonix-flash`. After G1, any non-reasonix model is "unknown" — this is the kill switch.

**G2 — delete codex-engine functions** (no caller remains after G1's codex-branch removal): `run_codex_cli`, `retryable_codex_cli_failure`, `extract_codex_final_text`, `mock_openai_chat_response`, `last_openai_user_text`, `provider_openai_chat_payload`, `openai_has_successful_structured_output`, `openai_tool_call_response`, `openai_stop_response`. (Verify each truly has no remaining caller in the reasonix path before deleting — grep the symbol; if the only callers were codex branches, delete.)

> **KEEP `codex_cli_semaphore`** — despite its name it is SHARED: `run_reasonix_acp` (L1163) calls it to bound reasonix acp concurrency. Verified by grep (callers: `run_codex_cli` L970 AND `run_reasonix_acp` L1163). Deleting it would break reasonix. Its rename to a neutral name is Phase 2.

**G3 — prune branches in the two large `call_*` functions:**
- `call_openai_compatible`: delete the `if config.get("provider") == "codex_cli":` branch and the api_key/deepseek-HTTP path; keep the `reasonix_cli` branch and the shared head of the function.
- `call_openai_chat_completion`: delete the `if config.get("provider") == "codex_cli":` branch and the HTTP openai/deepseek path; keep the `reasonix_cli` branch.
- This is the riskiest step — both branches call shared conversion helpers; the safety-net test catches a wrong deletion immediately.

**G4 — clean dispatch + error messages + unused imports:**
- `do_POST`: `if provider in ("codex_cli", "reasonix_cli"):` → `if provider == "reasonix_cli":` (both occurrences).
- Replace the "needs an API key. Set OPENAI_API_KEY for claude-codex-pro or DEEPSEEK_API_KEY for claude-deepseek-pro" messages with a reasonix-only message.
- Remove now-unused imports/locals (e.g. `subprocess` if only codex used it — confirm reasonix acp still needs it before removing).

**Gateway done-gate:** `test-reasonix-acp.py` + `test-mcp-reasonix.py` + `test-reasonix-cost-ledger.py` all PASS; `/v1/models` advertises only `claude-reasonix-flash`.

---

## Component 2 — Codex-only files (delete whole files)

Delete (reasonix has its own equivalents or doesn't need these):
- `system-prompt.md` (codex flavor system prompt; `system-prompt-reasonix.md` becomes the only prompt).
- `tests/e2e-tmux-claude-codex.sh`, `tests/test-e2e-evidence.sh`, `tests/verify-e2e-evidence.py`, `tests/test-ccr-proxy-timeout.py`, `tests/test-ccr-proxy-streaming.py` (codex-only, reasonix=0).

Verify nothing else references a deleted file (grep the filenames across the repo + launcher before deleting).

---

## Component 3 — Woven-together files (prune codex branches, keep names)

- **Launcher `~/.local/bin/claude-codex`** (not tracked — back up first): remove the `codex` flavor's registry/route generation (codex-pro/deepseek-pro), the `CODEX_BIN`/`CODEX_BACKEND`/`OPENAI_*`/`DEEPSEEK_*` env, and the codex provider config; keep the reasonix flavor block and the shared infra (gateway start, ccr proxy, MCP launch). The existing codex-flavor `exit 1` guard can be simplified since codex no longer exists. Smoke: `claude-reasonix router-login` still starts.
- **`codex-fleet-mcp.py`** (already flavor-aware): in `run_one_task`, remove the `codex exec` fallback path; always dispatch through `run_reasonix_acp`. KEEP tool names `run_codex_worker`/`run_codex_fleet` (Phase 2 renames); only the engine inside changes.
- **`hooks/only-codex-fleet.py`**: keep the Agent-blocking logic (reasonix still needs it); remove codex-specific references. Keep filename.
- **`hooks/codex-workflow.py`**: remove codex agentType branches in the lane-rewriter; keep reasonix.
- **`hooks/workflow_selfheal.py`**: remove the codex/deepseek-key probes; keep the reasonix probe.
- **Tests that mix**: `tests/test-codex-fleet.sh` (mostly codex — prune codex cases, keep reasonix cases) and `tests/test-gateway-nonstream-heartbeat.py` (prune codex cases). Keep all `test-reasonix-*` and `test-mcp-reasonix.py` untouched.

After each woven file: run its related test; after the launcher: the live smoke.

---

## Component 4 — Docs & README

- `README.md`: rewrite the codex sections to describe the reasonix-only system; update command examples from `claude-codex` to `claude-reasonix`; keep the reasonix content.
- Keep `docs/specs/2026-06-20-claude-reasonix-design.md` and `docs/plans/2026-06-20-claude-reasonix.md` (already reasonix).
- After Phase 1 completes, push the updated repo to the public GitHub mirror so the reference reflects the reasonix-only system.

---

## Error handling (fail-safe preserved)

- Unknown model (codex-pro/deepseek-pro after removal) → existing `if model not in registry` path returns `400 unknown model`, no crash.
- Stale codex env vars in a shell → harmless; nothing reads them anymore.
- MCP task → always the reasonix engine; no codex branch left to fall into.

---

## Testing

- After each gateway step G1–G4: `python3 tests/test-reasonix-acp.py` → `PASS`.
- After gateway complete: `test-mcp-reasonix.py` + `test-reasonix-cost-ledger.py` → PASS; `/v1/models` lists only `claude-reasonix-flash`.
- After launcher/MCP/hooks: live smoke — `claude-reasonix router-login` starts, one `/v1/chat/completions` request for `claude-reasonix-flash` returns 200 + non-empty content.
- Final completeness check: `grep -rn "run_codex_cli\|codex_cli_semaphore\|claude-codex-pro\|claude-deepseek-pro\|codex exec\|provider.*deepseek" <code files>` returns only historical comments / kept env names — zero engine code.

**Success metric:** gateway has exactly one engine (`reasonix_cli`); the reasonix e2e/unit tests pass unchanged; no codex-engine or deepseek-HTTP code path remains; the public mirror is updated.

---

## Rollback

- Each task is one commit in `~/.claude/codex-fleet` (git-tracked, also pushed to GitHub at the pre-refactor state). Break → `git revert <task>` or `git reset` to the prior commit.
- Launcher (untracked) → restore from `claude-codex.pre-gd1`.

## Non-Goals (Phase 2, not here)

- Renaming files / `CLAUDE_CODEX_*` env / `codex_fleet` MCP tools / the repo to a reasonix name.
- Renaming the shared shape-conversion functions (`*_openai_*`) to neutral names.
- Rewriting the gateway from scratch.
- Touching the reasonix request path's behavior.
