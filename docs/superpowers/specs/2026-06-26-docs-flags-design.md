# Sub-project C — Docs + Flags — Design

**Date:** 2026-06-26
**Status:** Design (approved in brainstorming, pending spec review)
**Goal:** Make the project usable by a stranger — a clean README surfacing only the ~8-10 flags a user needs, a full `docs/CONFIGURATION.md` documenting all flags, and a clean-clone install/uninstall test. Sub-project C of the public-OSS ship (A refactor + B CI done; C docs+flags; D push).

## Measured facts
- README.md is 142 lines, already decent (Install, Quick start, Commands, How it routes, Cache, Defaults, Uninstall, Layout). Two gaps: (1) the Layout section still says only `reasonix-native-gateway.py` — it doesn't mention the new `reasonix_gateway/` package (8 modules) from the A refactor; (2) ~78 `CLAUDE_REASONIX_*` flags exist (60 GATEWAY + 3 WORKFLOW + 15 FLEET/other) with no organization — a stranger can't tell which matter.
- 5 levers are promoted (default-ON, exported by the launcher): READ_SUMMARY, READER_BROADEN, READ_RETRY_HOLLOW, LANE_FAIL_MARKER, OVERSCOPE_REJECT.
- install.sh already runs a `doctor` self-check; uninstall.sh exists.

## Flag policy (decided)
README surfaces only the ~8-10 flags a user actually needs; the ~70 internal/experimental flags go into `docs/CONFIGURATION.md` (grouped, with defaults) — NOT dumped in the README. Every flag keeps its code (no removal); they're just default-OFF + documented in the advanced doc.

## Components

### C1 — README: update + surface only user-facing flags
**Where:** `README.md`.
**What:**
- Fix the **Layout** section to reflect the post-refactor structure: `reasonix-native-gateway.py` is now an 11-line shim; the real gateway lives in the `reasonix_gateway/` package (env, text, harness, cost, levers, engine_seam, server modules). Mention it briefly.
- Add/clean a **Configuration** section listing ONLY the ~8-10 flags a user needs, each with a one-line description + default: the MCP settings already shown (model/reasoning/workers), `DEEPSEEK_API_KEY` (auth), the fleet on/off, `CLAUDE_REASONIX_GATEWAY_LANE_HARNESS` (turn on the hard-task harness), and the 5 promoted levers (one line each: what they do, default-on). Do NOT list the ~70 internal flags here.
- Add a short **Advanced configuration** pointer: "≈70 further internal/experimental levers exist (cache-tuning, prime-gate, prefetch, etc.), all default-OFF and not needed for normal use — see `docs/CONFIGURATION.md`."
- Verify every flag/command the README mentions actually exists (grep the source) — no invented flags.

### C2 — `docs/CONFIGURATION.md` — the full flag reference
**Where:** new `docs/CONFIGURATION.md`.
**What:** document EVERY `CLAUDE_REASONIX_*` flag, grouped by area:
- **User-facing** (the ~8-10 from C1, repeated here with more detail).
- **Promoted levers** (the 5 default-ON, what each measures/does).
- **Cache / prefix** (keepalive, prime-gate, prime-serial, prefix-trace, read-cache).
- **Harness** (LANE_HARNESS, LANE_BUDGET_USD, LANE_MAX_ATTEMPTS, LANE_FAIL_MARKER).
- **Experimental levers** (preindex, prefetch, reader-broaden, output-discipline, overscope, etc.).
- **Internal/diagnostic** (CWD, timeouts, dict caps, traces).
Each entry: flag name, default, one-line effect. The list is GENERATED from the source (grep the `env_first/env_int/env_float/env_truthy` call sites for the name + default) so it's accurate and complete — NOT hand-invented. Note the `CLAUDE_CODEX_*` backward-compat fallback once at the top (don't repeat per flag).
**Done when:** every flag found in the source appears in the doc with its real default; no flag is invented; README links to it.

### C3 — Clean-clone install/uninstall end-to-end test
**Where:** new `tests/test-clean-install.sh` (or extend an existing install test).
**What:** prove a stranger can clone + install + use + uninstall:
1. Copy the repo to a fresh temp dir (simulating a clone), to a temp INSTALL_HOME.
2. Run `install.sh` with that temp INSTALL_HOME → assert it succeeds + `doctor` passes + the `reasonix_gateway/` package landed in the install (the A-refactor gap that was already fixed — guard it stays fixed).
3. Run `uninstall.sh` → assert the install home + launcher are removed, nothing left behind.
4. Clean up the temp dirs.
This is offline (install/doctor/uninstall don't need DeepSeek for the structural check). It is NOT added to CI's run-all by default if it's slow/heavy or mutates a real install — instead it uses a TEMP install home so it's safe, and CAN be in run-all if it stays fast + isolated. Decide in the plan; default to isolated-temp + include in run-all only if it doesn't touch the user's real install.
**Done when:** the test installs into a temp home, doctor passes, the package is present, uninstall removes everything, and it never touches the user's real `~/.claude/reasonix-fleet`.

## How we'll know it worked
1. README surfaces ~8-10 user flags + a pointer to the full doc; the Layout reflects the package; every mentioned flag/command exists in the source.
2. `docs/CONFIGURATION.md` documents every `CLAUDE_REASONIX_*` flag with its real default (generated from source, complete, none invented).
3. The clean-clone test installs into a temp home, doctor passes, package present, uninstall clean — proving a stranger's clone-install-uninstall works, without touching the real install.

## Out of scope
- No flag REMOVAL (keep all code; just organize the docs).
- No version/tag/push (Sub-project D).
- No gateway/engine/lever behavior change.
- No new features.
