# Changelog

All notable changes to the Claude Reasonix Fleet are recorded here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] — 2026-06-26

First public release. The fleet keeps Claude Code as your main agent while routing
subagent-style fan-out (Workflow / UltraCode lanes, agent teams, batch tasks) to
**DeepSeek v4-flash via a bundled in-process engine** instead of burning Claude tokens.

### Added

- **First-run DeepSeek API key prompt.** New OSS users have no credential, so
  `install.sh` now prompts for a DeepSeek API key on first run (when it has a TTY)
  and persists it to `~/.reasonix/config.json` — `chmod 600`, merged into any
  existing config. `DEEPSEEK_API_KEY` in the env still skips the prompt; a
  non-interactive install dies loudly with a clear instruction instead of hanging.
- **Continuous-integration suite.** `tests/run-all.sh` runs the full
  `test-*.{py,mjs,sh}` suite (52 tests) offline in mock mode; a GitHub Actions
  workflow runs it on every push to `main` and every pull request. The suite is
  clean-room green — it passes on a fresh runner with no DeepSeek credential and no
  `~/.reasonix/config.json`. Tests needing a live DeepSeek call are gated behind
  `REASONIX_LIVE_TESTS=1` and skip cleanly otherwise.
- **Configuration reference.** `docs/CONFIGURATION.md` documents every environment
  flag, grouped by audience (user-facing, promoted levers, cache/prefix, harness,
  experimental, internal).
- **Clean-clone install/uninstall test** proving a stranger can install and fully
  uninstall into an isolated temp home without touching real user state.

### Changed

- **Gateway refactored from a 3214-line monolith into an 8-module package**
  (`reasonix_gateway/`: env, text, harness, cost, levers, engine_seam, server,
  `__init__`) behind an 11-line re-export shim. The prompt-building engine seam is
  **byte-for-byte identical** to before — a golden test guards it, because a single
  changed byte would collapse the shared-prefix cache.
- **Cache guidance corrected.** The cross-workflow cache is **per-lane, not
  per-workflow**: a lane that answers from its in-prompt bytes caches ~99.6%; a lane
  that calls tools appends unique output and drops to 65–98%. Later workflows do
  **not** get monotonically cheaper. The lever is *one lane = one file, with the file
  in the prompt's shared prefix* — fine decomposition makes both the DeepSeek lane
  and the Opus controller cheaper — **not** a tool cap (capping starves a lane).

### Engine

- The DeepSeek engine is the owner's self-contained fork
  (`deepseek-reasonix-engine`), shipped as a prebuilt bundle under
  `vendor/reasonix-engine/` and driven in-process by a one-shot Node shim
  (`engine/run-lane.mjs`). There is no upstream-reasonix install and no in-place
  patch; the fork carries the ephemeral-session / cache behavior natively.
- **Engine-stability work** (outline-threshold, overscope-reject, the
  `LANE_UNVERIFIED` marker) and a **weak-executor harness** that drives the lane
  through a cheaper executor pass, measured ~5.9× cheaper on the validation workload.

[1.0.0]: https://github.com/Tatlatat/ultimate-deepseek-ultracode/releases/tag/v1.0.0
