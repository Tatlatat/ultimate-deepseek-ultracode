# Package + Rename + Publish `claude-reasonix` Design

**Goal:** Rename every `codex` reference to `reasonix` without breaking the system, then package the repo so anyone who clones it can run one command (`./install.sh`) and use it immediately — with clear guidance for the parts that cannot be automated (DeepSeek login).

**Architecture:** A three-layer rename (brand → agentType → cosmetics, each verified before the next), a real `install.sh` that auto-installs what it can and *checks + instructs* for what it can't, and a portable repo layout with no machine-specific hardcoded paths.

**Tech Stack:** bash launcher, Python gateway/hooks (stdlib only), reasonix CLI (npm) over DeepSeek, Claude Code as host. Optional: `ccr` (claude-code-router) for router mode.

## Global Constraints

- New name is `reasonix` (the project already uses it for the DeepSeek side; this unifies the two flavors under one name). `codex` → `reasonix`, `CLAUDE_CODEX_` → `CLAUDE_REASONIX_`, repo `claude-codex-fleet` → `claude-reasonix-fleet`.
- The system has **1180** `codex` occurrences, **71** distinct `CLAUDE_CODEX_*` env vars, **2** `codex-*.py` files, **4** `codex-*` agentTypes (worker/security/reviewer/verify), **7** files with hardcoded `/Users/tatlatat` paths.
- agentType is a string that MUST match byte-for-byte across THREE places simultaneously: launcher `--agents` definitions, `hooks/codex-workflow.py` emit (`return 'codex-worker'` etc.), and `hooks/only-codex-fleet.py` whitelist (`startswith(("codex-", "deepseek-"))`). Changing one without the others BREAKS lane routing (the code itself documents this: "Emitting reasonix-* names ... was what broke reasonix lanes").
- The reasonix NATIVE flavor (the one the user runs, `native_subagents_enabled`) does NOT require `ccr` — it uses `start_native_gateway` + `ANTHROPIC_BASE_URL=gateway` directly. `ccr` is OPTIONAL (router mode only).
- The system depends on a one-line PATCH to the reasonix npm dist (`REASONIX_ACP_EPHEMERAL_SESSION` in `acp-*.js`), which lives OUTSIDE the repo and reverts on reasonix upgrade.
- Backward-compat: each renamed env var is read with a fallback to its old `CLAUDE_CODEX_*` name. Rationale here is NOT external users (this is a first publish) but the OWNER's own running sessions/scripts and the in-flight migration — a renamed var should never silently lose a value someone already set under the old name. New name preferred, old name still honored; the fallback can be dropped in a later release.
- Every rename layer ends with: `python3 -c "import ast; ast.parse(...)"` on changed Python, the full 24-test suite, and a real bench (`realworld-bench.py`) — proceed to the next layer ONLY if green.
- Do NOT commit runtime artifacts (`runtime/*.jsonl`, `*.port`, `__pycache__`, `*.bak`) or any credential.

---

## Component 1: Layered Rename (`codex` → `reasonix`)

The rename is split by risk so a break is caught early and localized.

### Layer A — Brand names (low risk)
- **Files:** `codex-native-gateway.py` → `reasonix-native-gateway.py`; `codex-fleet-mcp.py` → `reasonix-fleet-mcp.py`; `hooks/codex-workflow.py` → `hooks/reasonix-workflow.py`; `hooks/only-codex-fleet.py` → `hooks/only-reasonix-fleet.py`. (`ccr-claude-proxy.py`, `system-prompt-reasonix.md` keep their names.)
- **Dir:** `~/.claude/codex-fleet` → `~/.claude/reasonix-fleet` (and the repo's `INSTALL_HOME` default).
- **Launcher:** `~/.local/bin/claude-codex` → ship as `bin/claude-reasonix`; the `claude-reasonix` symlink already exists.
- **Env vars (71):** `CLAUDE_CODEX_*` → `CLAUDE_REASONIX_*`, changed at EVERY read and write site, with a one-release fallback helper: read new name, else old name, else default.
- **Settings/MCP references:** `bridge-settings.json` hook command path; `runtime/mcp.json`; any `--mcp-config` / hook command that names a renamed file.
- **Verify:** ast-parse all changed Python; full suite; bench. The launcher's internal references to the renamed files/dir must all update together (a missed reference = file-not-found at spawn).

### Layer B — agentType + drop dead `deepseek-*` (HIGHEST risk)
- Rename the 4 agentTypes `codex-worker/codex-security/codex-reviewer/codex-verify` → `reasonix-worker/...`, changed SIMULTANEOUSLY in all three binding sites:
  1. launcher `--agents` definitions (both flavor blocks),
  2. `reasonix-workflow.py` emit (`return 'reasonix-worker'`, the `explicit.startsWith('reasonix-')` guard),
  3. `only-reasonix-fleet.py` whitelist (`startswith(("reasonix-", "deepseek-"))` → update prefix).
- **Drop `deepseek-deep` / `deepseek-architecture`** (they route to the same `claude-reasonix-flash` model as the worker — same runtime behavior, just different labels; the user confirmed they are unrelated cruft): remove the two `return 'deepseek-architecture'` / `return 'deepseek-deep'` emit branches in the hook (those lanes fall through to `reasonix-worker`), AND remove the two `--agents` definitions in the launcher. Whitelist: drop the `deepseek-` prefix once nothing emits it.
- **Verify:** ast-parse; full suite; **then a real fan-out workflow** (the bench's review scenario + a lane whose hint contains "architecture"/"database") and confirm via the ledger that those lanes route to `reasonix-worker` and are NOT blocked by the policy hook (0 blocked, lanes land in `reasonix-cost.jsonl`).

### Layer C — Cosmetics (zero risk)
- Comments, docstrings, test names, README prose, `claude-codex` strings in messages → `reasonix`. No behavior change.
- **Verify:** full suite (catches any test name still referenced).

---

## Component 2: `install.sh` (one command, honest)

Runs in order; AUTO-does what it can, CHECKS + INSTRUCTS for what it can't, exits non-zero with a clear message on any blocker.

| Step | Action | On failure |
|---|---|---|
| 1 | `command -v claude` — Claude Code present | print install link, exit 1 |
| 2 | `command -v reasonix` — reasonix CLI present | print `npm i -g reasonix` (or the project's install note), exit 1 |
| 3 | Probe reasonix is logged in to DeepSeek (a tiny `reasonix` invocation) | print `reasonix login` instructions, exit 1 |
| 4 | Symlink `bin/claude-reasonix` → `~/.local/bin/claude-reasonix` (mkdir -p, warn if `~/.local/bin` not on PATH) | warn, continue |
| 5 | Install `fleet/` → `~/.claude/reasonix-fleet` (copy; preserve existing `runtime/`) | exit 1 |
| 6 | Apply the dist patch (idempotent): locate reasonix dist via `dirname "$(command -v reasonix)"/../lib/node_modules/reasonix/dist`, grep for `REASONIX_ACP_EPHEMERAL_SESSION`; if absent, back up `acp-*.js` → `.preuniq.bak` and apply | if dist path not found, print the manual patch note, continue (system still works at lower cache) |
| 7 | Smoke test: spawn the gateway on a random port, POST one `/v1/messages` lane, assert a non-empty reply within a timeout | print the gateway log tail + exit 1 |

End: `✅ Ready — run 'claude-reasonix'` OR `⚠️ Missing X — do Y then re-run ./install.sh`.

- **Idempotent:** re-running install.sh is safe (patch checks before applying; symlink uses `ln -sf`).
- **`uninstall.sh`:** restore each `.preuniq.bak`, remove the symlink, leave `~/.claude/reasonix-fleet` (or `--purge` to remove).

---

## Component 3: Repo layout + portability

```
claude-reasonix-fleet/
├── install.sh           uninstall.sh
├── README.md            (quickstart, prerequisites, troubleshooting, "ccr optional")
├── bin/claude-reasonix  (the launcher, paths de-hardcoded)
├── fleet/
│   ├── reasonix-native-gateway.py   reasonix-fleet-mcp.py   ccr-claude-proxy.py
│   ├── hooks/reasonix-workflow.py    only-reasonix-fleet.py   workflow_selfheal.py
│   ├── system-prompt-reasonix.md     bridge-settings.json
│   └── tests/  (24 tests)
├── patches/ephemeral-session.md  (what the dist patch is + the apply script install.sh runs)
└── .gitignore  (runtime/*.jsonl, *.port, __pycache__/, *.bak, ccr-home/)
```

**Portability — de-hardcode the 7 files containing `/Users/tatlatat`:**
- `INSTALL_HOME` defaults to `${CLAUDE_REASONIX_INSTALL_HOME:-$HOME/.claude/reasonix-fleet}` (already env-overridable; confirm default uses `$HOME`).
- reasonix bin: resolve via `command -v reasonix` (never a fnm-multishell absolute path); the launcher already prepends the reasonix bin dir to PATH — keep that, but derive it from `command -v`.
- Any test/bench with a hardcoded reasonix path: read `REASONIX_BIN` env or `command -v reasonix`.
- `~/.local/bin` for the symlink uses `$HOME`.

**README** must state: prerequisites (Claude Code, reasonix CLI + DeepSeek login, node), the one-command install, that the dist patch reverts on reasonix upgrade (re-run install), that `ccr` is optional (router mode only — `npm i -g @musistudio/claude-code-router`), and a troubleshooting section mapping each install.sh failure to its fix.

---

## Testing

- After each rename layer: ast-parse changed Python + full 24-test suite + `realworld-bench.py` (must stay ALL-GATES-PASS / known-good cache).
- Layer B additionally: a live fan-out routing check (lanes land in the ledger under `reasonix-worker`, 0 policy-blocked).
- `install.sh`: tested in a clean `$HOME`/`$INSTALL_HOME` (e.g. a temp dir) to catch any residual hardcoded path — a fresh-machine simulation, since the real test is "another user clones and runs it."
- A new `tests/test-no-codex-leftovers.py`: asserts no `codex` / `CLAUDE_CODEX_` remains in shipped source except in explicit backward-compat fallbacks and historical doc/memory references.

## Error handling

- Env-var backward-compat: every renamed var read via a helper that tries `CLAUDE_REASONIX_X` then `CLAUDE_CODEX_X` then default — so a mid-migration environment never silently loses a setting.
- dist-patch absent (fresh machine / upgraded reasonix): the system still runs, only at lower cache; install.sh and the gateway both degrade gracefully (the ephemeral env is set on the child regardless; the patch just makes it effective).
- install.sh is fail-loud: any unmet prerequisite stops with an actionable message, never a half-installed state.
