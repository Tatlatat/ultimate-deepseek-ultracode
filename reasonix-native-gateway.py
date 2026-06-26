#!/usr/bin/env python3
"""Small Anthropic Messages-compatible gateway for claude-reasonix native agents.

The gateway is intentionally local and session-scoped.  The claude-reasonix
launcher starts it, points only that Claude Code process at it, and then stops
it when Claude exits.
"""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import time as _time
import traceback
from typing import Any
import urllib.error
import urllib.request
from uuid import uuid4


JSON = dict[str, Any]
_REASONIX_CLI_SEMAPHORE_LOCK = threading.Lock()
_REASONIX_CLI_SEMAPHORE: tuple[int, threading.BoundedSemaphore] | None = None


def env_first(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


def env_int(*names: str, default: int) -> int:
    raw = env_first(*names, default=str(default))
    try:
        return int(raw)
    except ValueError:
        return default


def env_float(*names: str, default: float) -> float:
    raw = env_first(*names, default=str(default))
    try:
        return float(raw)
    except ValueError:
        return default


def env_truthy(*names: str, default: str = "") -> bool:
    return env_first(*names, default=default).strip().lower() in {"1", "true", "yes", "on"}


def _lane_fail_marker_on() -> bool:
    return env_truthy("CLAUDE_REASONIX_GATEWAY_LANE_FAIL_MARKER",
                      "CLAUDE_CODEX_GATEWAY_LANE_FAIL_MARKER", default="1")


def lane_unverified_reply(reason: str) -> str:
    """A3: when a lane times out/errors, return a machine-readable marker so a workflow
    distinguishes 'could not verify' from 'verified=false'. A verify lane that gets this
    must be treated UNVERIFIED and its finding KEPT, never silently rejected (the
    level-3.1 bug: a timed-out verify with an empty verdict was counted as 'rejected').
    Returns '' when the flag is off (caller restores the old bare-error behavior)."""
    if not _lane_fail_marker_on():
        return ""
    return (f"LANE_UNVERIFIED: this lane did not complete ({reason}). "
            "Treat as UNVERIFIED (could not check), NOT as a false/disproven finding — "
            "keep the item and re-run with a smaller scope.")


# --- C3: weak-executor harness helpers (CLAUDE_REASONIX_GATEWAY_LANE_HARNESS, default OFF) ---
# The shim (engine/run-lane.mjs) runs an acceptance-test retry loop and returns a
# terse `__HARNESS__:<status>:<attempts>:<lesson>` text.  The gateway parses it into
# a SHORT structured lane reply (<200 chars) so the Opus orchestrator reviews only
# failures (ESCALATE marker) and never re-reads raw files — fixing the 97% cache-read
# blowup.  When the flag is off every path is byte-identical to today.

def _lane_harness_on() -> bool:
    return env_truthy("CLAUDE_REASONIX_GATEWAY_LANE_HARNESS",
                      "CLAUDE_CODEX_GATEWAY_LANE_HARNESS", default="0")


def parse_harness_result(text: str) -> JSON | None:
    """Parse the shim's harness summary text `__HARNESS__:<status>:<attempts>:<lesson>`.
    Returns None for a normal (non-harness) reply so the gateway passes it through
    unchanged (byte-inert when the harness is off)."""
    if not isinstance(text, str) or not text.startswith("__HARNESS__:"):
        return None
    parts = text.split(":", 3)  # ['__HARNESS__', status, attempts, lesson]
    if len(parts) < 3:
        return None
    try:
        attempts = int(parts[2])
    except (TypeError, ValueError):
        attempts = 0
    return {"status": parts[1], "attempts": attempts, "lesson": parts[3] if len(parts) > 3 else ""}


def harness_lane_reply(parsed: JSON) -> str:
    """A SHORT structured lane reply for the orchestrator. A passed lane returns a terse
    OK; a stagnated/exhausted lane carries an ESCALATE marker + the lesson so Opus reviews
    ONLY the failures (never re-reading raw files — the 97% cache-read fix)."""
    st = parsed.get("status")
    att = parsed.get("attempts")
    if st == "pass":
        return f"LANE_OK pass: completed in {att} attempt(s), acceptance test green."
    return (f"LANE_ESCALATE: status={st} after {att} attempt(s). "
            f"Could not finish; orchestrator should take over this lane. Lesson: {parsed.get('lesson','')}")


def lane_acceptance_test(messages: Any) -> str:
    txt = lane_task_text(messages)
    for line in txt.splitlines():
        s = line.strip()
        if s.upper().startswith("ACCEPTANCE_TEST:"):
            return s.split(":", 1)[1].strip()
    return ""


# --- Lever D — pre-index (CLAUDE_REASONIX_PREINDEX, default OFF) ----------------
# Build a semantic index ONCE per codebase so read-exploration lanes can QUERY it
# via the EXISTING `semantic_search` tool instead of reading raw files. The index
# adds NO prefix bytes (it's a side store + a query tool that only registers when
# an index already exists), so the immutable prefix stays byte-stable.
#
# The GATEWAY is the SOLE build trigger: it calls build_preindex(cwd) once, up
# front. Per-lane shims NEVER build — they only check `indexCompatible()`
# read-only (via buildCodeToolset -> bootstrapSemanticSearchInCodeMode). This
# avoids the JSONL append race where two concurrent lanes both call buildIndex
# and corrupt index.jsonl.
#
# FAIL-OPEN is mandatory: when PREINDEX is off (default) this is a no-op; when on
# but no embedding provider/model is reachable (the current state — Ollama runs
# with 0 models), build_preindex logs and returns gracefully. It MUST NOT raise,
# MUST NOT block lanes, MUST NOT break the gateway.
_PREINDEX_LOCK = threading.Lock()
_PREINDEX_DONE: set[str] = set()


def preindex_enabled() -> bool:
    return env_truthy("CLAUDE_REASONIX_PREINDEX", default="0")


def _preindex_node_bin() -> str:
    return env_first("CLAUDE_REASONIX_NODE_BIN", "NODE_BIN", default="node")


def _preindex_engine_dist() -> str | None:
    explicit = env_first("REASONIX_ENGINE_DIST", default="")
    if explicit and os.path.exists(explicit):
        return explicit
    install_home = env_first(
        "CLAUDE_REASONIX_FLEET_INSTALL_HOME", "CLAUDE_CODEX_FLEET_INSTALL_HOME",
        default=os.path.dirname(os.path.abspath(__file__)),
    )
    vendored = os.path.join(install_home, "vendor", "reasonix-engine", "dist", "index.js")
    return vendored if os.path.exists(vendored) else None


# Inline ESM driver: import the vendored fork dist and call the re-exported
# buildIndex(root, opts). Embedding provider/model/baseUrl come from env so NO
# secret is interpolated into the script source. Any throw (no provider, probe
# failure, etc.) is printed and exits non-zero — the Python caller treats that as
# fail-open. Default provider "ollama" lets buildIndex's own probeEmbeddingProvider
# decide reachability and raise if a model is absent.
_PREINDEX_NODE_SCRIPT = r"""
import { createRequire } from "node:module";
// The vendored fork engine is a tsup `noExternal` bundle that interops with a
// few CJS-only deps via an esbuild `__require` shim resolving to the global
// `require`. An ESM `-e` host has no ambient require, so provide the canonical
// bridge before loading the bundle (same as engine/run-lane.mjs).
if (typeof globalThis.require !== "function") {
  globalThis.require = createRequire(import.meta.url);
}
const { buildIndex, indexCompatible } = await import(process.env.REASONIX_ENGINE_DIST);
const root = process.env.REASONIX_PREINDEX_ROOT;
const provider = (process.env.REASONIX_EMBED_PROVIDER || "ollama").trim();
const model = (process.env.REASONIX_EMBED_MODEL || "").trim();
const baseUrl = (process.env.REASONIX_EMBED_BASE_URL || "").trim();
const opts = {};
if (provider === "openai-compat") {
  opts.provider = "openai-compat";
  if (baseUrl) opts.baseUrl = baseUrl;
  if (model) opts.model = model;
  const key = (process.env.REASONIX_EMBED_API_KEY || "").trim();
  if (key) opts.apiKey = key;
} else {
  opts.provider = "ollama";
  if (baseUrl) opts.baseUrl = baseUrl;
  if (model) opts.model = model;
}
try {
  const res = await buildIndex(root, opts);
  // A "successful" build can still yield NO usable index when the embedding
  // model is absent: Ollama is reachable (so probeEmbeddingProvider doesn't
  // throw) but every chunk is skipped ("model not pulled"), so chunksAdded==0
  // and no compatible index meta is written. Tie success to the SAME read-only
  // check the per-lane path uses (indexCompatible) — if it's not compatible,
  // exit 4 so the gateway treats it as fail-open (no model), not a real index.
  const compatible = await indexCompatible(root, {
    provider: opts.provider,
    model: opts.model,
  });
  if (!compatible) {
    process.stderr.write(
      "preindex produced no usable index (no reachable embedding model; chunksAdded=" +
        (res && res.chunksAdded != null ? res.chunksAdded : "?") + ")\n",
    );
    process.exit(4);
  }
  process.stdout.write(JSON.stringify({ ok: true, ...res }) + "\n");
} catch (e) {
  process.stderr.write("preindex build failed: " + (e && e.message ? e.message : String(e)) + "\n");
  process.exit(3);
}
"""


def build_preindex(cwd: str | None = None) -> bool:
    """Lever D: build the semantic index ONCE for `cwd`. Returns True iff an index
    was built. FAIL-OPEN — never raises; logs and returns False on any problem
    (PREINDEX off, no node, no engine dist, no embedding model, timeout, error)."""
    if not preindex_enabled():
        return False
    root = os.path.abspath(cwd or env_first(
        "CLAUDE_REASONIX_GATEWAY_CWD", "CLAUDE_CODEX_GATEWAY_CODEX_CWD", default=os.getcwd()))
    with _PREINDEX_LOCK:
        if root in _PREINDEX_DONE:
            return False
        # Mark BEFORE building so a concurrent caller never double-triggers even if
        # the build raises — the gateway is the sole trigger and a failed build
        # fails open (we don't retry-storm; a later restart re-attempts).
        _PREINDEX_DONE.add(root)
    dist = _preindex_engine_dist()
    if not dist:
        gateway_trace("preindex_skip", reason="no_engine_dist", root=root)
        print("[preindex] skipped: vendored engine dist not found (fail-open)", file=sys.stderr, flush=True)
        return False
    node_bin = _preindex_node_bin()
    timeout = env_float("CLAUDE_REASONIX_PREINDEX_TIMEOUT", default=120.0)
    child_env = dict(os.environ)
    # The inline ESM driver reads the dist path + root from env (no interpolation).
    child_env["REASONIX_ENGINE_DIST"] = dist
    child_env["REASONIX_PREINDEX_ROOT"] = root
    try:
        proc = subprocess.run(
            [node_bin, "--input-type=module", "-e", _PREINDEX_NODE_SCRIPT],
            input="", capture_output=True, text=True, cwd=root, env=child_env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        gateway_trace("preindex_fail_open", reason="timeout", root=root, timeout=timeout)
        print(f"[preindex] fail-open: build timed out after {timeout:g}s (lanes proceed read-only)", file=sys.stderr, flush=True)
        return False
    except OSError as exc:
        gateway_trace("preindex_fail_open", reason="spawn_error", root=root, error=str(exc))
        print(f"[preindex] fail-open: could not start node ({exc}); lanes proceed read-only", file=sys.stderr, flush=True)
        return False
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        msg = detail[-1] if detail else f"node exited {proc.returncode}"
        gateway_trace("preindex_fail_open", reason="build_error", root=root, detail=msg)
        print(f"[preindex] fail-open: no reachable embedding provider/model ({msg}); lanes proceed read-only", file=sys.stderr, flush=True)
        return False
    gateway_trace("preindex_built", root=root, stdout=(proc.stdout or "").strip()[:300])
    print(f"[preindex] built semantic index for {root}", file=sys.stderr, flush=True)
    return True


def gateway_trace(event: str, **fields: Any) -> None:
    if os.getenv("CLAUDE_REASONIX_GATEWAY_TRACE", os.getenv("CLAUDE_CODEX_GATEWAY_TRACE", "")).lower() not in {"1", "true", "yes", "on"}:
        return
    record = {"time": time.time(), "event": event, **fields}
    print(json.dumps(record, ensure_ascii=False, sort_keys=True), file=sys.stderr, flush=True)


def reasonix_cli_semaphore() -> threading.BoundedSemaphore:
    global _REASONIX_CLI_SEMAPHORE
    limit = max(1, env_int("CLAUDE_REASONIX_GATEWAY_CONCURRENCY", "CLAUDE_CODEX_GATEWAY_CODEX_CONCURRENCY", default=16))
    with _REASONIX_CLI_SEMAPHORE_LOCK:
        if _REASONIX_CLI_SEMAPHORE is None or _REASONIX_CLI_SEMAPHORE[0] != limit:
            _REASONIX_CLI_SEMAPHORE = (limit, threading.BoundedSemaphore(limit))
        return _REASONIX_CLI_SEMAPHORE[1]


# --- Prefix-prime gate -------------------------------------------------------
# Concurrent fan-out lanes that share a long byte-identical prefix (the common
# review context) otherwise all hit DeepSeek BEFORE its server-side prompt cache
# has stored that prefix from the first lane — so every lane in the burst pays
# the full prefix as a cache MISS (measured: a 28KB-shared 12-lane burst cached
# only ~69%, while the LAST lane, after the prefix warmed, hit 99.7%). The gate
# lets ONE lane per distinct prefix run alone to warm the cache, then releases
# the rest to run concurrently against the now-warm prefix. Keyed by a hash of
# the prompt's leading bytes. Deterministic, controller-independent.
_PRIME_LOCK = threading.Lock()
_PRIME_GATES: dict[str, threading.Event] = {}


def _prime_dict_cap() -> int:
    # 0 disables eviction (keep all). Default 2048 — large enough that a single
    # burst's whole prefix-family set is never evicted mid-flight (real fan-outs are
    # tens of families), small enough to bound a long-lived session.
    return env_int("CLAUDE_REASONIX_GATEWAY_PRIME_DICT_CAP", "CLAUDE_CODEX_GATEWAY_PRIME_DICT_CAP", default=2048)


def _evict_oldest(*dicts: dict) -> None:
    """Bound the prime/lane bookkeeping dicts (they are keyed by prefix-family hash
    and otherwise grow unbounded across a long session — a real memory leak found by
    the bench review). Evict the OLDEST inserted keys (dict preserves insertion
    order) so the most-recent keys — the only ones a LIVE burst still looks up — are
    always kept. Must be called holding the relevant lock. An evicted Event still
    works for any waiter already holding a reference; only NEW lookups for that old
    (completed) key would miss, which is harmless. cap<=0 disables."""
    cap = _prime_dict_cap()
    if cap <= 0:
        return
    for d in dicts:
        while len(d) > cap:
            d.pop(next(iter(d)), None)


# --- Cross-workflow keep-alive ---------------------------------------------
# DeepSeek's shared-prefix cache is best-effort and evicted by LRU/idle: a warm
# codebase prefix that many same-context workflows reuse gets cold-dropped during a
# gap between workflows (measured: an accumulating 96.96->99.60% run cold-dropped to
# 74% on one workflow). A tiny background keep-alive re-touches each recently-seen
# shared prefix periodically, refreshing its LRU recency so it survives the gap
# between same-codebase workflows. Records the LEADING slice of each lane's prompt
# (the cacheable shared block) keyed by prefix-family. Off via
# CLAUDE_REASONIX_GATEWAY_KEEPALIVE=0.
_KEEPALIVE_LOCK = threading.Lock()
_KEEPALIVE_PREFIXES: dict[str, tuple[str, float]] = {}


def _keepalive_enabled() -> bool:
    return env_first("CLAUDE_REASONIX_GATEWAY_KEEPALIVE", "CLAUDE_CODEX_GATEWAY_KEEPALIVE", default="1").lower() not in {"0", "false", "no", "off"}


def record_keepalive_prefix(prompt: str) -> None:
    """Remember the leading shared-prefix slice of a lane's prompt so a background
    keep-alive can later re-warm it. No-op when disabled or the prompt is too short
    to carry a meaningful shared prefix."""
    if not _keepalive_enabled():
        return
    head_len = env_int("CLAUDE_REASONIX_GATEWAY_KEEPALIVE_HEAD", "CLAUDE_CODEX_GATEWAY_KEEPALIVE_HEAD", default=8192)
    if len(prompt) < min(head_len, 2000):
        return
    key = prefix_prime_key(prompt)
    head = prompt[:head_len]
    with _KEEPALIVE_LOCK:
        _KEEPALIVE_PREFIXES[key] = (head, _time.time())
        _evict_oldest(_KEEPALIVE_PREFIXES)


def keepalive_targets() -> list[tuple[str, str]]:
    """(key, head) pairs for families seen within the freshness window — the prefixes
    worth re-warming. Stale families (the user moved on) are skipped and pruned."""
    window = env_float("CLAUDE_REASONIX_GATEWAY_KEEPALIVE_WINDOW_SECONDS", "CLAUDE_CODEX_GATEWAY_KEEPALIVE_WINDOW_SECONDS", default=600.0)
    now = _time.time()
    out: list[tuple[str, str]] = []
    with _KEEPALIVE_LOCK:
        for key, (head, ts) in list(_KEEPALIVE_PREFIXES.items()):
            if now - ts <= window:
                out.append((key, head))
            else:
                _KEEPALIVE_PREFIXES.pop(key, None)
    return out


# --- Lever C: gateway shared read-summary cache ----------------------------
# DEFAULT OFF (measure-then-promote). A file read+summarized by ONE fan-out lane
# is cached HERE, in the long-lived gateway process (NOT the shim — each shim lane
# is an ephemeral subprocess that shares nothing). Later lanes on the SAME codebase
# that reference the same file get its cached summary INJECTED into their prompt at
# a FIXED boundary (after the shared system block, before the per-lane tail), turning
# a raw re-read MISS into a cached-summary HIT.
#
# HIGHEST-RISK INVARIANT: the injected block MUST be byte-deterministic — same files
# referenced => byte-identical injected bytes regardless of the per-lane tail — or it
# forks the shared prefix that drives the prefix cache. Enforced by the BLOCKING test
# tests/test-read-cache-bytestable.py. The block is SORTED by path, fixed-format, and
# normalize_prefix-clean. Keyed by (path, mtime/hash); persisted to
# runtime/read-summary-cache.json with mtime-freshness on load (Q10).
_READ_SUMMARY_CACHE_LOCK = threading.Lock()
# key: absolute file path  ->  {"fp": mtime/hash str, "summary": str, "ts": float}
_READ_SUMMARY_CACHE: dict[str, dict[str, Any]] = {}
_READ_CACHE_LOADED = False

# Fixed, byte-stable block sentinels. These NEVER carry per-lane data so the block
# is identical across lanes that reference the same cached files.
READ_CACHE_BLOCK_BEGIN = "<<<REASONIX_READ_SUMMARY_CACHE>>>"
READ_CACHE_BLOCK_END = "<<<END_REASONIX_READ_SUMMARY_CACHE>>>"

# A file path referenced in a prompt: absolute POSIX-ish path ending in a code/text
# extension, OR a repo-relative path with a slash. Conservative on purpose — a false
# positive only fails to find a cache entry (no injection), never forks the prefix.
_FILE_PATH_RE = re.compile(
    r"(?<![\w./-])(/?(?:[\w.-]+/)+[\w.-]+\.[A-Za-z0-9]{1,8})(?![\w/])"
)


def _read_cache_on() -> bool:
    """Lever C master switch. DEFAULT OFF (reasonix-first, CLAUDE_CODEX_ fallback)."""
    return os.getenv(
        "CLAUDE_REASONIX_GATEWAY_READ_SUMMARY_CACHE",
        os.getenv("CLAUDE_CODEX_GATEWAY_READ_SUMMARY_CACHE", "0"),
    ).lower() in {"1", "true", "yes", "on"}


def _read_cache_cap() -> int:
    return env_int("CLAUDE_REASONIX_GATEWAY_READ_SUMMARY_CACHE_CAP",
                   "CLAUDE_CODEX_GATEWAY_READ_SUMMARY_CACHE_CAP", default=512)


def _read_cache_ttl_s() -> float:
    return env_float("CLAUDE_REASONIX_GATEWAY_READ_SUMMARY_CACHE_TTL_S",
                     "CLAUDE_CODEX_GATEWAY_READ_SUMMARY_CACHE_TTL_S", default=300.0)


def _read_cache_max_bytes() -> int:
    return env_int("CLAUDE_REASONIX_GATEWAY_READ_SUMMARY_CACHE_MAX_BYTES",
                   "CLAUDE_CODEX_GATEWAY_READ_SUMMARY_CACHE_MAX_BYTES", default=131072)


def _read_cache_path() -> Path:
    explicit = env_first("CLAUDE_REASONIX_GATEWAY_READ_SUMMARY_CACHE_PATH",
                         "CLAUDE_CODEX_GATEWAY_READ_SUMMARY_CACHE_PATH", default="")
    if explicit:
        return Path(explicit)
    home = env_first("CLAUDE_REASONIX_FLEET_HOME", "CLAUDE_CODEX_FLEET_HOME",
                     default=os.path.dirname(os.path.abspath(__file__)))
    return Path(home) / "runtime" / "read-summary-cache.json"


def _file_fingerprint(path: str) -> str | None:
    """mtime-based fingerprint for freshness: if the file changed, its old summary
    is stale and must not be served. Returns None when the file is unreadable."""
    try:
        st = os.stat(path)
        return f"{int(st.st_mtime_ns)}:{st.st_size}"
    except OSError:
        return None


def extract_file_paths_from_prompt(prompt: str) -> list[str]:
    """Find file paths referenced in a prompt. Returns a SORTED, de-duplicated list
    so downstream injection is order-independent and byte-deterministic. Conservative:
    only matches dotted-extension paths with a directory component, so prose words are
    not mistaken for files (a miss is harmless — no injection)."""
    if not prompt:
        return []
    found: set[str] = set()
    for m in _FILE_PATH_RE.finditer(prompt):
        found.add(m.group(1))
    return sorted(found)


def _read_cache_store(path: str, fp: str, summary: str) -> None:
    """Insert/refresh one entry (caller may hold or not hold the lock — this takes it).
    Bounds the dict to _CAP via FIFO eviction of the oldest inserted key (mirror of
    _evict_oldest for the prime dicts), keeping the newest (live) keys."""
    summary = summary[: _read_cache_max_bytes()]
    with _READ_SUMMARY_CACHE_LOCK:
        # Re-insert moves the key to newest (dict preserves insertion order); pop first.
        _READ_SUMMARY_CACHE.pop(path, None)
        _READ_SUMMARY_CACHE[path] = {"fp": fp, "summary": summary, "ts": _time.time()}
        cap = _read_cache_cap()
        if cap > 0:
            while len(_READ_SUMMARY_CACHE) > cap:
                _READ_SUMMARY_CACHE.pop(next(iter(_READ_SUMMARY_CACHE)), None)


def _read_cache_lookup(path: str) -> str | None:
    """Return a FRESH cached summary for `path`, or None. Fresh = within TTL AND the
    file's current fingerprint matches the cached one (mtime-freshness). Stale entries
    are dropped."""
    ttl = _read_cache_ttl_s()
    now = _time.time()
    with _READ_SUMMARY_CACHE_LOCK:
        entry = _READ_SUMMARY_CACHE.get(path)
        if entry is None:
            return None
        if ttl > 0 and (now - float(entry.get("ts", 0))) > ttl:
            _READ_SUMMARY_CACHE.pop(path, None)
            return None
        cur_fp = _file_fingerprint(path)
        # If the file is gone/unreadable we can't verify freshness -> serve only when
        # we have no fingerprint to compare (keep deterministic: drop on mismatch).
        if cur_fp is not None and entry.get("fp") not in (None, "", cur_fp):
            _READ_SUMMARY_CACHE.pop(path, None)
            return None
        return str(entry.get("summary") or "")


def read_cache_injection_block(prompt: str) -> str:
    """Build the byte-deterministic cached-summary block to inject for the files this
    prompt references. Returns "" when the lever is OFF, or no referenced file has a
    fresh cache entry. The block is SORTED by path and fixed-format, so two lanes that
    reference the same cached files get BYTE-IDENTICAL bytes regardless of their tail.
    normalize_prefix-clean by construction (no volatile lines)."""
    if not _read_cache_on():
        return ""
    paths = extract_file_paths_from_prompt(prompt)
    if not paths:
        return ""
    lines: list[str] = []
    for p in paths:  # already sorted + de-duped
        summary = _read_cache_lookup(p)
        if not summary:
            continue
        # One line per file, fixed format. Collapse newlines so the block stays
        # single-line-per-file and byte-stable (summaries are compact JSON anyway).
        flat = " ".join(summary.split())
        lines.append(f"- {p}: {flat}")
    if not lines:
        return ""
    body = "\n".join(lines)
    block = (
        f"{READ_CACHE_BLOCK_BEGIN}\n"
        "Cached read-summaries for files this task references (reuse instead of "
        "re-reading; the file is unchanged since it was summarized):\n"
        f"{body}\n"
        f"{READ_CACHE_BLOCK_END}"
    )
    # Belt-and-suspenders: ensure no volatile billing line ever rides along.
    return normalize_prefix(block)


def populate_read_cache(prompt: str, summary: str) -> None:
    """After a lane returns, cache its summary keyed by the file(s) it read. No-op when
    the lever is OFF, the summary is empty, or the prompt referenced no file path.
    Best-effort and exception-safe (a cache failure must never break a lane)."""
    if not _read_cache_on():
        return
    if not summary or not summary.strip():
        return
    try:
        paths = extract_file_paths_from_prompt(prompt)
        if not paths:
            return
        for p in paths:
            fp = _file_fingerprint(p) or ""
            _read_cache_store(p, fp, summary)
        save_read_cache()
    except Exception:
        pass


def save_read_cache() -> None:
    """Persist the cache to runtime/read-summary-cache.json (Q10). Append-only-ish:
    we rewrite the whole small dict atomically. Exception-safe."""
    if not _read_cache_on():
        return
    try:
        path = _read_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with _READ_SUMMARY_CACHE_LOCK:
            data = {k: dict(v) for k, v in _READ_SUMMARY_CACHE.items()}
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        pass


def load_read_cache() -> None:
    """Load persisted summaries on startup, dropping entries whose file changed since
    they were cached (mtime-freshness on load, Q10) or whose TTL has expired."""
    global _READ_CACHE_LOADED
    if not _read_cache_on():
        return
    try:
        path = _read_cache_path()
        if not path.exists():
            _READ_CACHE_LOADED = True
            return
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            _READ_CACHE_LOADED = True
            return
        ttl = _read_cache_ttl_s()
        now = _time.time()
        with _READ_SUMMARY_CACHE_LOCK:
            for p, entry in raw.items():
                if not isinstance(entry, dict):
                    continue
                summary = str(entry.get("summary") or "")
                if not summary:
                    continue
                ts = float(entry.get("ts", 0) or 0)
                if ttl > 0 and (now - ts) > ttl:
                    continue  # stale by TTL
                cur_fp = _file_fingerprint(p)
                cached_fp = entry.get("fp")
                # mtime-freshness on load: drop if the file changed.
                if cur_fp is not None and cached_fp not in (None, "", cur_fp):
                    continue
                _READ_SUMMARY_CACHE[p] = {"fp": cached_fp, "summary": summary, "ts": ts}
            cap = _read_cache_cap()
            if cap > 0:
                while len(_READ_SUMMARY_CACHE) > cap:
                    _READ_SUMMARY_CACHE.pop(next(iter(_READ_SUMMARY_CACHE)), None)
        _READ_CACHE_LOADED = True
    except Exception:
        _READ_CACHE_LOADED = True


# --- Staggered prime serialization -----------------------------------------
# DeepSeek persists a prefix only AFTER a request finishes — so when N lanes of
# one prefix family hit concurrently, the first 2-3 race the persist and all miss
# (measured: 3 early lanes at 65-83% while later ones hit 97-99%). To warm the
# prefix deterministically, the first PRIME_SERIAL lanes of a family take a
# per-key lock and run ONE AT A TIME (each ~persists more of the shared prefix
# before the next); lanes past that window run in parallel against the now-warm
# prefix. Costs ~one-lane latency up front, zero extra tokens.
_PRIME_SERIAL_LOCK = threading.Lock()
_PRIME_SERIAL_COUNTS: dict[str, int] = {}
_PRIME_SERIAL_LOCKS: dict[str, threading.Lock] = {}


def reset_prime_state(key: str) -> None:
    """Test/diagnostic helper — clear the serial counter+lock for a key."""
    with _PRIME_SERIAL_LOCK:
        _PRIME_SERIAL_COUNTS.pop(key, None)
        _PRIME_SERIAL_LOCKS.pop(key, None)


def serial_lock_for(key: str) -> threading.Lock:
    with _PRIME_SERIAL_LOCK:
        lk = _PRIME_SERIAL_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _PRIME_SERIAL_LOCKS[key] = lk
        return lk


def acquire_serial_slot(key: str) -> bool:
    """True if this caller is within the first PRIME_SERIAL lanes of the family and
    must run serially (hold serial_lock_for(key) while running, release when done).
    False if past the window — run in parallel."""
    n = env_int("CLAUDE_REASONIX_GATEWAY_PRIME_SERIAL", "CLAUDE_CODEX_GATEWAY_PRIME_SERIAL", default=3)
    if n <= 0:
        return False
    with _PRIME_SERIAL_LOCK:
        c = _PRIME_SERIAL_COUNTS.get(key, 0)
        if c >= n:
            return False
        # On a NEW family key, bound the serial dicts first (evict oldest completed
        # families). _PRIME_SERIAL_COUNTS must stay monotonic WITHIN a live burst, so
        # only evict when adding a brand-new key, and never the key we're about to set.
        if c == 0 and len(_PRIME_SERIAL_COUNTS) >= _prime_dict_cap() > 0:
            _evict_oldest(_PRIME_SERIAL_COUNTS, _PRIME_SERIAL_LOCKS)
        _PRIME_SERIAL_COUNTS[key] = c + 1
        return True


# --- Per-lane loop breaker -------------------------------------------------
# A lane whose model never emits valid JSON gets re-driven turn-by-turn by Claude
# Code, each turn re-feeding history (input 27K->227K, measured). We count repeats
# of the same lane signature; past the threshold, the forced-StructuredOutput path
# returns a schema-valid fallback so the workflow completes instead of looping.
_LANE_LOCK = threading.Lock()
_LANE_COUNTS: dict[str, int] = {}


def register_lane_attempt(prompt: str) -> int:
    key = prefix_prime_key(prompt)
    with _LANE_LOCK:
        n = _LANE_COUNTS.get(key, 0) + 1
        _LANE_COUNTS[key] = n
        # Bound the lane-count dict, but never evict the key we just touched (it is a
        # live lane being retry-counted) — re-insert it so it's the newest.
        if len(_LANE_COUNTS) > _prime_dict_cap() > 0:
            _evict_oldest(_LANE_COUNTS)
            _LANE_COUNTS[key] = n  # ensure the live key survives as newest
        return n


def should_force_fallback(prompt: str) -> bool:
    limit = env_int("CLAUDE_REASONIX_GATEWAY_MAX_LANE_RETRIES", "CLAUDE_CODEX_GATEWAY_MAX_LANE_RETRIES", default=3)
    if limit <= 0:
        return False
    key = prefix_prime_key(prompt)
    with _LANE_LOCK:
        return _LANE_COUNTS.get(key, 0) >= limit


def clear_lane_count(prompt: str) -> None:
    """Reset a prefix-family's attempt count after a lane of that family SUCCEEDS
    (produced parseable output). The loop-breaker counts attempts per prefix-family,
    so without this a family that ever accumulated MAX_LANE_RETRIES attempts across
    the session would force the schema-fallback on every later same-family lane that
    narrates once — even fresh healthy lanes that would have succeeded on a retry. A
    success proves the family is not stuck looping, so clear it. (Found by the bench
    review lanes auditing the gateway.)"""
    if env_first("CLAUDE_REASONIX_GATEWAY_LANE_RESET_ON_SUCCESS", "CLAUDE_CODEX_GATEWAY_LANE_RESET_ON_SUCCESS",
                 default="1").lower() not in {"1", "true", "yes", "on"}:
        return  # kill-switch: keep legacy monotonic (never-reset) behavior
    key = prefix_prime_key(prompt)
    with _LANE_LOCK:
        _LANE_COUNTS.pop(key, None)


def prefix_prime_key(prompt: str) -> str:
    """Group lanes for the prime gate by hashing only the LEADING head of the
    prompt (the part lanes actually share), NOT the whole prompt. Measured: real
    fan-out lanes share ~5-8KB (system + shared intro/file head) then diverge into
    per-lane data — hashing 32KB split every lane into its own key, so the gate
    never grouped them. A short head (default 8KB) groups lanes that share that
    leading block, so one primer warms it for the rest. Tunable via
    CLAUDE_REASONIX_GATEWAY_PRIME_KEY_HEAD (falls back to CLAUDE_CODEX_GATEWAY_PRIME_KEY_HEAD,
    then the legacy CLAUDE_REASONIX_GATEWAY_PRIME_HEAD_BYTES alias if set)."""
    import hashlib
    head = env_int("CLAUDE_REASONIX_GATEWAY_PRIME_KEY_HEAD", "CLAUDE_CODEX_GATEWAY_PRIME_KEY_HEAD",
                   "CLAUDE_CODEX_GATEWAY_PRIME_HEAD_BYTES",  # legacy alias, CLAUDE_REASONIX_GATEWAY_PRIME_KEY_HEAD preferred
                   default=4096)
    return hashlib.sha1(prompt[:head].encode("utf-8", "ignore")).hexdigest()[:16]


def acquire_prime_role(prompt: str) -> tuple[bool, threading.Event | None]:
    """Return (is_primer, gate). The first caller for a given prefix is the
    primer (is_primer=True) and MUST call gate.set() when its call completes.
    Later callers get is_primer=False and should wait on the returned gate
    (bounded) before proceeding — by then the prefix is warm."""
    if env_first("CLAUDE_REASONIX_GATEWAY_PRIME_GATE", "CLAUDE_CODEX_GATEWAY_PRIME_GATE", default="1").lower() not in {"1", "true", "yes", "on"}:
        return False, None
    key = prefix_prime_key(prompt)
    with _PRIME_LOCK:
        gate = _PRIME_GATES.get(key)
        if gate is None:
            gate = threading.Event()
            _PRIME_GATES[key] = gate
            _evict_oldest(_PRIME_GATES)
            return True, gate
        return False, gate


def model_registry() -> dict[str, JSON]:
    return {
        "claude-reasonix-flash": {
            "display_name": os.getenv("CLAUDE_REASONIX_REASONIX_DISPLAY_NAME", os.getenv("CLAUDE_CODEX_REASONIX_DISPLAY_NAME", "claude-reasonix-flash")),
            "provider": "reasonix_cli",
            "target_model": env_first("CLAUDE_REASONIX_REASONIX_MODEL", "CLAUDE_CODEX_REASONIX_MODEL", default="deepseek-v4-flash"),
            "reasonix_bin": env_first("REASONIX_BIN", default="reasonix"),
        },
    }


def json_bytes(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def text_from_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                parts.append(str(block))
                continue
            block_type = block.get("type")
            if block_type == "text":
                parts.append(str(block.get("text", "")))
            elif block_type == "tool_result":
                parts.append(text_from_content(block.get("content")))
            elif block_type == "image":
                parts.append("[image omitted by local gateway]")
            else:
                parts.append(json.dumps(block, ensure_ascii=False))
        return "\n".join(part for part in parts if part)
    return str(content)


_BILLING_HEADER_RE = re.compile(r"^x-anthropic-billing-header:[^\n]*\n?", re.MULTILINE)


def normalize_prefix(text: str) -> str:
    """Strip per-request volatile lines from the START of the system prompt so the
    prefix is byte-stable across lanes/sessions and DeepSeek's prompt cache can
    reuse it. The `x-anthropic-billing-header: cc_version=...XXX; ...` line carries
    a rotating version segment (measured: 9d6/94e/ef4/bcd across sessions) at the
    very first bytes, which otherwise busts the cache for the whole leading block.
    It is pure telemetry (version/entrypoint/is_subagent) — reasonix never needs
    it — so removing it is safe and lossless for the worker task."""
    return _BILLING_HEADER_RE.sub("", text)


def anthropic_system_to_text(system: Any) -> str:
    return normalize_prefix(text_from_content(system))


def anthropic_messages_to_openai(payload: JSON) -> list[JSON]:
    messages: list[JSON] = []
    system_text = anthropic_system_to_text(payload.get("system"))
    if system_text:
        messages.append({"role": "system", "content": system_text})

    for item in payload.get("messages", []):
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")

        if role == "assistant":
            text_parts: list[str] = []
            tool_calls: list[JSON] = []
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        text_parts.append(str(block))
                        continue
                    if block.get("type") == "text":
                        text_parts.append(str(block.get("text", "")))
                    elif block.get("type") == "tool_use":
                        tool_calls.append(
                            {
                                "id": str(block.get("id") or f"call_{uuid4().hex[:24]}"),
                                "type": "function",
                                "function": {
                                    "name": str(block.get("name") or ""),
                                    "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                                },
                            }
                        )
            else:
                text_parts.append(text_from_content(content))
            message: JSON = {"role": "assistant", "content": "\n".join(p for p in text_parts if p) or None}
            if tool_calls:
                message["tool_calls"] = tool_calls
            messages.append(message)
            continue

        if role == "user" and isinstance(content, list):
            user_parts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    user_parts.append(str(block))
                    continue
                if block.get("type") == "tool_result":
                    if user_parts:
                        messages.append({"role": "user", "content": "\n".join(user_parts)})
                        user_parts = []
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": str(block.get("tool_use_id") or ""),
                            "content": text_from_content(block.get("content")),
                        }
                    )
                elif block.get("type") == "text":
                    user_parts.append(str(block.get("text", "")))
                elif block.get("type") == "image":
                    user_parts.append("[image omitted by local gateway]")
                else:
                    user_parts.append(json.dumps(block, ensure_ascii=False))
            if user_parts:
                messages.append({"role": "user", "content": "\n".join(user_parts)})
            continue

        if role in {"user", "system"}:
            messages.append({"role": role, "content": text_from_content(content)})

    return messages


def anthropic_tools_to_openai(tools: Any) -> list[JSON] | None:
    if not isinstance(tools, list) or not tools:
        return None
    converted: list[JSON] = []
    for tool in tools:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": str(tool["name"]),
                    "description": str(tool.get("description") or ""),
                    "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
                },
            }
        )
    return converted or None


def anthropic_tool_choice_to_openai(choice: Any) -> Any:
    if not isinstance(choice, dict):
        return None
    kind = choice.get("type")
    if kind == "none":
        return "none"
    if kind == "auto":
        return "auto"
    if kind == "any":
        return "required"
    if kind == "tool" and choice.get("name"):
        return {"type": "function", "function": {"name": str(choice["name"])}}
    return None


def openai_response_to_anthropic(data: JSON, requested_model: str) -> JSON:
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content: list[JSON] = []

    text = message.get("content")
    if isinstance(text, str) and text:
        content.append({"type": "text", "text": text})

    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        raw_args = function.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except Exception:
            args = {"raw_arguments": raw_args}
        content.append(
            {
                "type": "tool_use",
                "id": str(call.get("id") or f"toolu_{uuid4().hex[:24]}"),
                "name": str(function.get("name") or ""),
                "input": args if isinstance(args, dict) else {"value": args},
            }
        )

    finish_reason = choice.get("finish_reason")
    stop_reason = "tool_use" if any(block.get("type") == "tool_use" for block in content) else "end_turn"
    if finish_reason == "length":
        stop_reason = "max_tokens"
    elif finish_reason == "content_filter":
        stop_reason = "stop_sequence"

    usage = data.get("usage") or {}
    return {
        "id": str(data.get("id") or f"msg_{uuid4().hex}"),
        "type": "message",
        "role": "assistant",
        "model": requested_model,
        "content": content or [{"type": "text", "text": ""}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
        },
    }


def estimate_tokens(payload: Any) -> int:
    return max(1, len(json.dumps(payload, ensure_ascii=False)) // 4)


def provider_chat_payload(payload: JSON, config: JSON) -> JSON:
    request: JSON = {
        "model": config["target_model"],
        "messages": anthropic_messages_to_openai(payload),
    }
    max_tokens = payload.get("max_tokens")
    if isinstance(max_tokens, int) and max_tokens > 0:
        request[str(config.get("max_tokens_param") or "max_tokens")] = max_tokens

    tools = anthropic_tools_to_openai(payload.get("tools"))
    if tools:
        request["tools"] = tools
    tool_choice = anthropic_tool_choice_to_openai(payload.get("tool_choice"))
    if tool_choice is not None:
        request["tool_choice"] = tool_choice

    for field in ("temperature", "top_p", "stop"):
        if field in payload:
            request[field] = payload[field]

    return request


def call_openai_compatible(payload: JSON, requested_model: str, config: JSON) -> JSON:
    if os.getenv("CLAUDE_REASONIX_GATEWAY_MOCK", os.getenv("CLAUDE_CODEX_GATEWAY_MOCK", "")).lower() in {"1", "true", "yes", "on"}:
        return {
            "id": f"msg_{uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "model": requested_model,
            "content": [
                {
                    "type": "text",
                    "text": f"mock {requested_model} response for {text_from_content((payload.get('messages') or [{}])[-1].get('content'))}",
                }
            ],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": estimate_tokens(payload), "output_tokens": 12},
        }

    if config.get("provider") == "reasonix_cli":
        messages = anthropic_messages_to_openai(payload)
        prompt = openai_messages_to_prompt(messages, payload.get("tools"))
        register_lane_attempt(prompt)
        record_keepalive_prefix(prompt)
        if os.getenv("CLAUDE_REASONIX_GATEWAY_STRUCTURED_DEBUG", os.getenv("CLAUDE_CODEX_GATEWAY_STRUCTURED_DEBUG", "")).lower() in {"1", "true", "yes", "on"}:
            try:
                _dd = Path(env_first("CLAUDE_REASONIX_FLEET_HOME", "CLAUDE_CODEX_FLEET_HOME",
                    default=os.path.dirname(os.path.abspath(__file__)))) / "runtime"
                _dd.mkdir(parents=True, exist_ok=True)
                with open(_dd / "structured-debug.jsonl", "a", encoding="utf-8") as _df:
                    _df.write(json.dumps({
                        "ts": _time.time(), "path": "messages-entry",
                        "tool_names": tool_names_from_payload(payload),
                        "tool_choice": payload.get("tool_choice"),
                        "prompt_has_schema_instr": "STRUCTURED OUTPUT REQUIREMENT" in prompt,
                        "prompt_tail": prompt[-600:],
                    }, ensure_ascii=False) + "\n")
            except Exception:
                pass
        lane_type = classify_lane_type(payload.get("tools"), lane_task_text(messages))
        # Lever F HARD layer (default off): cap output by lane-type budget.
        # Lever A HARD layer (default off): for read lanes, read_summary_budget()
        # returns 512 when READ_SUMMARY is on.  Both levers agree on 512 for read
        # lanes; pick the tighter (smallest) non-None cap so either flag alone or
        # both together yield the same correct cap.
        _f_cap = output_discipline_budget(lane_type)
        _a_cap = read_summary_budget() if lane_type == "read" else None
        _caps = [c for c in (_f_cap, _a_cap) if c is not None]
        _max_out = min(_caps) if _caps else None
        # Lever G (default off): reject lanes whose file scope is too broad.
        _rej = overscope_rejection(lane_task_text(messages),
                                   env_first("CLAUDE_REASONIX_GATEWAY_CWD",
                                             "CLAUDE_CODEX_GATEWAY_CODEX_CWD",
                                             default=os.getcwd()))
        if _rej is not None:
            return anthropic_end_turn_response(requested_model, None, text=_rej)
        # Lever A truncation recovery: when A caps THIS read lane (_a_cap set), an empty
        # result means the model was truncated before answering — retry once at a higher
        # cap. Gated by CLAUDE_REASONIX_GATEWAY_READ_RETRY_HOLLOW (default on when A on).
        _retry_hollow = (_a_cap is not None) and env_truthy(
            "CLAUDE_REASONIX_GATEWAY_READ_RETRY_HOLLOW",
            "CLAUDE_CODEX_GATEWAY_READ_RETRY_HOLLOW", default="1")
        # C3: build harness dict gated by flag (default off -> _harness stays None
        # -> run_reasonix_acp gets harness=None -> request dict byte-identical).
        _harness = None
        if _lane_harness_on():
            _at = lane_acceptance_test(messages)
            if _at:
                _harness = {
                    "acceptanceTest": _at,
                    "budgetUsd": env_float("CLAUDE_REASONIX_GATEWAY_LANE_BUDGET_USD",
                                          "CLAUDE_CODEX_GATEWAY_LANE_BUDGET_USD", default=0.05),
                    "harnessMaxAttempts": env_int("CLAUDE_REASONIX_GATEWAY_LANE_MAX_ATTEMPTS",
                                                  "CLAUDE_CODEX_GATEWAY_LANE_MAX_ATTEMPTS", default=4),
                }
        text, usage = run_reasonix_acp(
            prompt, config, max_output_tokens=_max_out,
            retry_empty_force=_retry_hollow, harness=_harness)
        # C3: fold harness reply BEFORE populate_read_cache / ledger so the short
        # structured reply (not raw shim text) flows onward.
        _hp = parse_harness_result(text)
        if _hp is not None:
            text = harness_lane_reply(_hp)
        # Lever C (default off): cache this lane's summary keyed by the file(s) it
        # read so later lanes on the same codebase reuse it (miss->hit). No-op when
        # the flag is off. Best-effort; never breaks the lane.
        populate_read_cache(prompt, text)
        gateway_trace("reasonix_acp_response", model=requested_model,
                      cost=usage.get("reasonix_cost_usd"), cache=usage.get("reasonix_cache_pct"))
        ledger = env_first(
            "CLAUDE_REASONIX_REASONIX_COST_LEDGER", "CLAUDE_CODEX_REASONIX_COST_LEDGER",
            default=str(Path(env_first("CLAUDE_REASONIX_FLEET_HOME", "CLAUDE_CODEX_FLEET_HOME",
                                       default=os.path.dirname(os.path.abspath(__file__)))) / "runtime" / "reasonix-cost.jsonl"),
        )
        append_reasonix_cost(
            ledger, usage,
            cwd=env_first("CLAUDE_REASONIX_GATEWAY_CWD", "CLAUDE_CODEX_GATEWAY_CODEX_CWD", default=os.getcwd()),
            model=str(config.get("target_model") or ""),
            claude_equiv=usage.get("reasonix_claude_equiv_usd"),
            lane_type=lane_type,
        )
        # Dynamic-Workflow agent({schema}) lanes pass a StructuredOutput tool and
        # expect the subagent to RETURN A tool_use, not prose. reasonix/DeepSeek
        # emits the JSON as text (the prompt instruction tells it to), so the
        # workflow harness saw "completed without calling StructuredOutput" and
        # failed the lane. When such a tool was requested AND the model produced a
        # parseable JSON object, wrap it as a StructuredOutput tool_use so the
        # harness gets the tool-call it requires. Fall back to plain text only when
        # no structured tool was requested or the output isn't valid JSON.
        structured_tool = requested_structured_output_tool(payload)
        if os.getenv("CLAUDE_REASONIX_GATEWAY_STRUCTURED_DEBUG", os.getenv("CLAUDE_CODEX_GATEWAY_STRUCTURED_DEBUG", "")).lower() in {"1", "true", "yes", "on"}:
            try:
                _dbg_dir = Path(env_first("CLAUDE_REASONIX_FLEET_HOME", "CLAUDE_CODEX_FLEET_HOME",
                    default=os.path.dirname(os.path.abspath(__file__)))) / "runtime"
                _dbg_dir.mkdir(parents=True, exist_ok=True)
                _parsed = parse_json_object_from_text(text) if structured_tool else None
                with open(_dbg_dir / "structured-debug.jsonl", "a", encoding="utf-8") as _df:
                    _df.write(json.dumps({
                        "ts": _time.time(),
                        "tool_names": tool_names_from_payload(payload),
                        "structured_tool": structured_tool,
                        "tool_choice": payload.get("tool_choice"),
                        "text_len": len(text),
                        "text_head": text[:400],
                        "parsed_ok": _parsed is not None,
                    }, ensure_ascii=False) + "\n")
            except Exception:
                pass
        if structured_tool:
            tool_input = parse_json_object_from_text(text)
            if tool_input is not None:
                # Real parseable output: this family is NOT stuck looping, so reset its
                # attempt count (otherwise a past loop poisons fresh healthy lanes).
                clear_lane_count(prompt)
            if tool_input is None:
                # DeepSeek sometimes narrates ("results returned via StructuredOutput")
                # instead of emitting JSON. When the caller FORCED this tool via
                # tool_choice, OR when this lane has looped past the retry limit, the
                # lane MUST still get a StructuredOutput tool_use or the workflow
                # aborts/loops. Synthesize a schema-valid object so the lane completes.
                forced = _tool_choice_forces(payload, structured_tool)
                looping = should_force_fallback(prompt)
                if forced or looping:
                    if looping:
                        gateway_trace("lane_loop_break", model=requested_model,
                                      retries=env_int("CLAUDE_REASONIX_GATEWAY_MAX_LANE_RETRIES", "CLAUDE_CODEX_GATEWAY_MAX_LANE_RETRIES", default=3))
                    tool_input = structured_timeout_fallback(
                        payload.get("tools"), structured_tool,
                        "schema-valid fallback (model narrated or lane looped)",
                    )
            if tool_input is not None:
                return anthropic_tool_use_response(requested_model, structured_tool, tool_input, usage)
        return anthropic_end_turn_response(requested_model, usage, text=text)

    raise GatewayError(400, "unsupported_provider", f"unsupported provider: {config.get('provider')!r}; this gateway serves only claude-reasonix-flash")


def tool_schema_entries(tools: Any) -> list[JSON]:
    if not isinstance(tools, list):
        return []

    entries: list[JSON] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if isinstance(function, dict):
            name = str(function.get("name") or "")
            description = str(function.get("description") or tool.get("description") or "")
            parameters = function.get("parameters")
        else:
            name = str(tool.get("name") or "")
            description = str(tool.get("description") or "")
            parameters = tool.get("input_schema") or tool.get("parameters")

        if not name:
            continue
        if not isinstance(parameters, dict):
            parameters = {"type": "object", "properties": {}}
        entries.append({"name": name, "description": description, "schema": parameters})
    return entries


def schema_type(schema: JSON) -> str:
    raw_type = schema.get("type")
    if isinstance(raw_type, list):
        for item in raw_type:
            if item != "null":
                return str(item)
        return str(raw_type[0]) if raw_type else ""
    return str(raw_type or "")


def fallback_value_from_schema(schema: Any, field_name: str, reason: str) -> Any:
    if not isinstance(schema, dict):
        return reason

    if "const" in schema:
        return schema["const"]
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]

    kind = schema_type(schema)
    properties = schema.get("properties")
    if kind == "object" or isinstance(properties, dict):
        props = properties if isinstance(properties, dict) else {}
        required = schema.get("required")
        names = list(props.keys())
        if isinstance(required, list):
            for name in required:
                if isinstance(name, str) and name not in names:
                    names.append(name)
        return {name: fallback_value_from_schema(props.get(name, {"type": "string"}), name, reason) for name in names}

    if kind == "array":
        return []
    if kind == "boolean":
        return False
    if kind in {"integer", "number"}:
        return 0
    if field_name.lower() in {"sourcequality", "quality"}:
        return "unreliable"
    if "date" in field_name.lower():
        return "unknown"
    return reason


def structured_timeout_fallback(tools: Any, tool_name: str, reason: str) -> JSON:
    for entry in tool_schema_entries(tools):
        if entry.get("name") == tool_name:
            fallback = fallback_value_from_schema(entry.get("schema") or {}, "", reason)
            return fallback if isinstance(fallback, dict) else {"error": str(fallback)}
    return {"error": reason}


def structured_output_prompt_instruction(tools: Any) -> str:
    structured_entries = [
        entry for entry in tool_schema_entries(tools)
        if is_structured_output_tool_name(str(entry.get("name") or ""))
    ]
    if not structured_entries:
        return ""

    blocks: list[str] = [
        "STRUCTURED OUTPUT REQUIREMENT:",
        (
            "Respond in ONE shot. Your ENTIRE reply must be EXACTLY ONE JSON object matching the schema "
            "below and NOTHING else — no prose, no markdown fences, no tool-call narration, no commentary "
            "before or after, and do NOT attempt to run shell/Bash commands or call any tool (you cannot; "
            "the embedded commands are context only). Do NOT write sentences like 'returned via "
            "StructuredOutput'. Emit the raw JSON object directly as your whole answer; this gateway "
            "converts that JSON into the StructuredOutput tool call for the caller."
        ),
        (
            "Match the schema exactly: use the exact property names, include every required key, "
            "use only literal enum values, and do not wrap the result in extra keys unless the schema requires them. "
            "Base the content on the task and any data already present in the prompt; if you cannot determine "
            "a value, use a best-effort value or an empty array — never reply with prose."
        ),
    ]
    for entry in structured_entries:
        if entry.get("description"):
            blocks.append(f"Tool {entry['name']} description: {entry['description']}")
        blocks.append(f"Tool {entry['name']} JSON schema:")
        blocks.append(json.dumps(entry.get("schema") or {}, ensure_ascii=False, indent=2, sort_keys=True))
    return "\n".join(blocks)


def _schema_has_nested_array_of_objects(schema: Any) -> bool:
    """True if the JSON schema contains an array whose items are objects (a
    nested structure DeepSeek-flash struggles to emit in one shot)."""
    if not isinstance(schema, dict):
        return False
    props = schema.get("properties")
    if isinstance(props, dict):
        for v in props.values():
            if isinstance(v, dict) and v.get("type") == "array":
                items = v.get("items")
                if isinstance(items, dict) and items.get("type") == "object":
                    return True
            if _schema_has_nested_array_of_objects(v):
                return True
    items = schema.get("items")
    if isinstance(items, dict) and _schema_has_nested_array_of_objects(items):
        return True
    return False


# A lane is a genuine SYNTHESIZE/merge step (where map-reduce belongs) only when its
# prompt is about merging MANY already-collected items into one structured result.
# A READER lane (read these files and report) ALSO carries a nested schema + a long
# prompt, so size+schema alone misclassifies readers as heavy-synthesis and wrongly
# injects the map-reduce skill into them. Gate on explicit synthesize intent so the
# skill fires ONLY in the Synthesize phase.
_SYNTHESIS_INTENT_RE = re.compile(
    r"\b(synthe|merge|combine|aggregate|consolidate|reduce|rank|dedup|"
    r"into one|into a single|across (the |all )?(items|findings|claims|sources|results)|"
    r"the following (items|findings|claims|sources|results))",
    re.IGNORECASE,
)
# A reader lane is the opposite — it ingests source material rather than merging it.
# The broad \bread\b alternative at the end acts as a catch-all; classify_lane_type
# checks edit intent BEFORE read, so "edit and read" → edit, never misclassified here.
_READER_INTENT_RE = re.compile(
    r"\b(read (the|these|all|only|just|this|through|file)|read:|open the file|inspect the (file|repo|code)|"
    r"use webfetch|fetch (the|this) (page|url|source)|enumerate|list what'?s in|\bread\b)",
    re.IGNORECASE,
)


_EDIT_INTENT_RE = re.compile(
    r"\b(edit|write|create|modify|apply|patch|implement|add|delete|rename|refactor|replace|fix|optimize|update|change|remove|insert)\b",
    re.IGNORECASE,
)

# Broadened read verbs — only active when CLAUDE_REASONIX_GATEWAY_READER_BROADEN=1.
# Synthesis and edit are checked BEFORE this in classify_lane_type, so they always
# win ties ("review and merge" → synthesize, "review and refactor" → edit).
_READER_BROADEN_RE = re.compile(
    r"\b(analyze|analyse|review|examine|investigate|audit|inspect|study|trace|explain|summari[sz]e|find\b|walk through|describe what)\b",
    re.I)


def _reader_broaden_on() -> bool:
    return env_truthy("CLAUDE_REASONIX_GATEWAY_READER_BROADEN",
                      os.getenv("CLAUDE_CODEX_GATEWAY_READER_BROADEN", "0"))


def lane_task_text(messages: Any) -> str:
    """The lane's RAW task text — the user/system message content BEFORE the gateway
    appends any directive (structured-output instruction, F's discipline directive,
    A's summary instruction, the cache block). Classify on THIS, never on the fully
    assembled prompt: every injected directive carries edit/read keywords (e.g. the
    structured instruction says 'Do NOT write sentences like…'), which would flip a
    read lane to 'edit' and silently disable the per-type output cap. Measured: with
    the cap keyed off the assembled prompt, 0 read/review lanes were ever classified
    as read — all 150 became 'edit'."""
    if not isinstance(messages, list):
        return ""
    parts: list[str] = []
    for m in messages:
        if isinstance(m, dict) and m.get("role") in {"user", "system"}:
            parts.append(text_from_content(m.get("content")))
    return "\n\n".join(p for p in parts if p)


def classify_lane_type(tools: Any, prompt_text: str | None) -> str:
    """Classify a lane as 'synthesize', 'edit', 'read', or 'unknown'.

    Order matters: synthesize is checked first (heavy-synthesis OR synthesis-intent),
    then edit (checked BEFORE read so 'modify and summarize' → edit, never capped as
    read), then read, then unknown.

    The gateway-injected PREFIX_GUIDE cache-advice is stripped first (see
    _strip_injected_guide): its prose carries edit-verbs ("build ONE fixed shared
    block", "crams everything") that would otherwise flip a read lane to 'edit' and
    silently disable Lever A's read-summary cap. Classify the lane's TASK, not the
    advice. No-op (byte-identical) when no guide marker is present.
    """
    pt = _strip_injected_guide(prompt_text or "")
    if is_heavy_synthesis(tools, len(pt), pt):
        return "synthesize"
    if _SYNTHESIS_INTENT_RE.search(pt):
        return "synthesize"
    if _EDIT_INTENT_RE.search(pt):
        return "edit"
    if _READER_INTENT_RE.search(pt):
        return "read"
    if _reader_broaden_on() and _READER_BROADEN_RE.search(pt):
        return "read"
    return "unknown"


def is_synthesis_prompt(prompt_text: str) -> bool:
    """True when the prompt's intent is to MERGE many items (a synthesize step),
    not to READ source material. Reader-intent wins ties so we never misfire the
    map-reduce skill into a file/web reader lane."""
    if not prompt_text:
        return False
    if _READER_INTENT_RE.search(prompt_text) and not _SYNTHESIS_INTENT_RE.search(prompt_text):
        return False
    return bool(_SYNTHESIS_INTENT_RE.search(prompt_text))


def is_heavy_synthesis(tools: Any, prompt_len: int, prompt_text: str = "") -> bool:
    """A forced StructuredOutput whose schema is nested, whose prompt is large, AND
    whose intent is genuinely to SYNTHESIZE/merge many items is a 'heavy synthesis'
    lane that flash loops on — route it to the map-reduce skill. The synthesis-intent
    gate keeps the skill OUT of reader lanes (which also have nested schemas + long
    prompts). Disabled by CLAUDE_REASONIX_GATEWAY_MAPREDUCE_SYNTHESIS=0."""
    if os.getenv("CLAUDE_REASONIX_GATEWAY_MAPREDUCE_SYNTHESIS", os.getenv("CLAUDE_CODEX_GATEWAY_MAPREDUCE_SYNTHESIS", "1")).lower() not in {"1", "true", "yes", "on"}:
        return False
    min_len = env_int("CLAUDE_REASONIX_GATEWAY_MAPREDUCE_MIN_PROMPT", "CLAUDE_CODEX_GATEWAY_MAPREDUCE_MIN_PROMPT", default=20000)
    if prompt_len < min_len:
        return False
    # Map-reduce is a Synthesize-phase tool only. A reader lane must never get it.
    if not is_synthesis_prompt(prompt_text):
        return False
    for entry in tool_schema_entries(tools):
        if is_structured_output_tool_name(str(entry.get("name") or "")):
            if _schema_has_nested_array_of_objects(entry.get("schema")):
                return True
    return False


def mapreduce_directive() -> str:
    return (
        "\n\n========================================\n"
        "MANDATORY FIRST ACTION — DO THIS BEFORE ANYTHING ELSE:\n"
        "This synthesis is too large to answer in one turn (it overflows and breaks "
        "the JSON). You MUST delegate it. Your VERY FIRST tool call must be exactly:\n"
        "  run_skill({\"name\": \"map-reduce-synthesis\", \"arguments\": \"<paste the ENTIRE task and item block from above here>\"})\n"
        "Do NOT try to write the JSON yourself. Do NOT summarize the items yourself. "
        "Call run_skill now with the full task as `arguments`, wait for its JSON result, "
        "and return that JSON object verbatim as your answer. The skill 'map-reduce-synthesis' "
        "is in your pinned Skills index.\n"
        "========================================"
    )


def context_budget_directive() -> str:
    """A lane-invariant guard that keeps a worker lane LEAN without a hard read cap.
    Root cause of the 75-80% cache + slow lanes (measured): a lane read 833 files /
    ran 659 commands, ballooning its prompt to 532K tokens — every fresh file is
    uncached content, so cache craters and flash slows. A HARD cap is wrong: a lane
    that genuinely needs 50 files would be killed, and flash (acp, no subagentRunner)
    cannot self-split an oversized task. So this guard does NOT cap — it tells the
    lane to work in a targeted way AND to FLAG when the task is too big to do well in
    one lane, so the work surfaces for decomposition instead of being silently
    crammed. The real fix for oversized work is finer decomposition at the controller
    (see system-prompt-reasonix.md). Byte-identical across lanes (prefix-stable).
    Off via CLAUDE_REASONIX_GATEWAY_CONTEXT_GUARD=0."""
    if os.getenv("CLAUDE_REASONIX_GATEWAY_CONTEXT_GUARD", os.getenv("CLAUDE_CODEX_GATEWAY_CONTEXT_GUARD", "1")).lower() not in {"1", "true", "yes", "on"}:
        return ""
    return (
        "WORK LEAN (this directly controls cost and speed — but never skip work the "
        "task actually requires):\n"
        "- Use targeted search (grep/glob for the exact symbol) before reading whole "
        "files or whole directories. Read what the task needs — no more, no less.\n"
        "- Keep a running summary in your own words; do not re-read files you already "
        "read or hold raw file dumps you no longer need.\n"
        "- If this task is genuinely too large to do well in ONE lane (it would need "
        "to read very many files or explore broadly), say so explicitly in your "
        "answer and describe how it should be split into smaller lanes — do NOT try "
        "to cram all of it into this single lane. Right-sized lanes are faster, "
        "cheaper, and more accurate.\n"
    )


def _output_discipline_on() -> bool:
    """Lever F master switch. DEFAULT OFF (owner Q1: measure-then-promote)."""
    return os.getenv(
        "CLAUDE_REASONIX_GATEWAY_OUTPUT_DISCIPLINE",
        os.getenv("CLAUDE_CODEX_GATEWAY_OUTPUT_DISCIPLINE", "0"),
    ).lower() in {"1", "true", "yes", "on"}


def output_discipline_directive() -> str:
    """Lever F SOFT layer — the #1-ROI token lever attacks the 42.3% output
    bucket. Returns "" unless OUTPUT_DISCIPLINE is on (and the _DIRECTIVE
    sub-flag, default on, isn't disabled). When on, a terse-output block that
    bans narration and restating the task, and — for edits — demands a minimal
    diff / SEARCH-REPLACE with NO reprinted unchanged code and NO placeholder
    comments. Appended LAST in the prompt (correctness-beats-cache placement,
    same slot the structured/summary instruction uses) so it is the freshest
    instruction the model sees. The budget (output_discipline_budget) is the
    HARD layer; this is the soft nudge."""
    if not _output_discipline_on():
        return ""
    if os.getenv(
        "CLAUDE_REASONIX_GATEWAY_OUTPUT_DISCIPLINE_DIRECTIVE",
        os.getenv("CLAUDE_CODEX_GATEWAY_OUTPUT_DISCIPLINE_DIRECTIVE", "1"),
    ).lower() not in {"1", "true", "yes", "on"}:
        return ""
    # NOTE: this text must contain NO _EDIT_INTENT_RE keyword (edit/write/apply/
    # modify/…). The call-site classifier sees the full assembled prompt (task +
    # this directive), so an edit keyword here would flip EVERY lane to 'edit' and
    # silently disable F's per-type cap. Phrased to avoid those tokens; the
    # classifier therefore keys only off the lane's actual task text.
    return (
        "OUTPUT DISCIPLINE (obey exactly):\n"
        "- Be terse. No narration ('I will now…', 'Let me…', 'Sure, here is…'), "
        "no restating or summarizing the task, no chain-of-thought prose. Lead "
        "with the answer.\n"
        "- For code changes: emit a MINIMAL unified diff (a find/swap block) "
        "ONLY. Do NOT reprint unchanged code, do NOT leave placeholder comments "
        "like '// rest unchanged' or '# ... existing code ...'. Show only the "
        "lines that differ, with just enough context to locate them."
    )


def output_discipline_budget(lane_type: str) -> int | None:
    """Lever F HARD layer — the per-lane-type max_output_tokens cap. Returns
    None when OUTPUT_DISCIPLINE is off (the gateway then passes NO cap, so F is
    a true no-op by default). When on:
      read   -> 512  (a read lane returns a one-line summary; it should never
                      stream a wall of text)
      edit   -> EDIT budget (default 5900 — the top-20% output proxy the
                      controller measured; the ledger had too few REAL
                      edit-format lanes to derive a P95, so this is set
                      conservatively above the structural minimum and MUST be
                      re-tuned to ceil(measured-edit-P95 x 1.2) once the harness
                      emits real diff/SEARCH-REPLACE edit lanes — Step 1 probe
                      confirmed a too-low cap truncates an edit block mid-
                      structure, so this floor must stay generous)
      else   -> 2048 (review/synthesize/unknown — a normal worker answer)
    All three are env-overridable for per-deployment tuning."""
    if not _output_discipline_on():
        return None
    if lane_type == "read":
        return env_int(
            "CLAUDE_REASONIX_GATEWAY_OUTPUT_DISCIPLINE_MAX_TOKENS_READ",
            "CLAUDE_CODEX_GATEWAY_OUTPUT_DISCIPLINE_MAX_TOKENS_READ", default=512)
    if lane_type == "edit":
        return env_int(
            "CLAUDE_REASONIX_GATEWAY_OUTPUT_DISCIPLINE_MAX_TOKENS_EDIT",
            "CLAUDE_CODEX_GATEWAY_OUTPUT_DISCIPLINE_MAX_TOKENS_EDIT", default=5900)
    return env_int(
        "CLAUDE_REASONIX_GATEWAY_OUTPUT_DISCIPLINE_MAX_TOKENS_DEFAULT",
        "CLAUDE_CODEX_GATEWAY_OUTPUT_DISCIPLINE_MAX_TOKENS_DEFAULT", default=2048)


def _read_summary_on() -> bool:
    """Lever A master switch. DEFAULT OFF (measure-then-promote, same as F)."""
    return os.getenv(
        "CLAUDE_REASONIX_GATEWAY_READ_SUMMARY",
        os.getenv("CLAUDE_CODEX_GATEWAY_READ_SUMMARY",
                  os.getenv("READ_SUMMARY", "0")),
    ).lower() in {"1", "true", "yes", "on"}


def read_summary_budget() -> int | None:
    """Lever A HARD cap for read lanes. Returns None when READ_SUMMARY is off
    (no cap, true no-op). When on, returns the READ_SUMMARY max output tokens
    (default 512 — same as F's read budget; they agree so either flag is on
    the cap is 512 and the plumbing has no conflict)."""
    if not _read_summary_on():
        return None
    return env_int(
        "CLAUDE_REASONIX_GATEWAY_READ_SUMMARY_MAX_TOKENS",
        "CLAUDE_CODEX_GATEWAY_READ_SUMMARY_MAX_TOKENS", default=512)


def read_lane_summary_instruction(lane_type: str, tools: Any = None) -> str:
    """Lever A instruction layer — schema-enforced read summary.

    Returns "" unless:
      - READ_SUMMARY env is on, AND
      - lane_type is 'read', AND
      - no StructuredOutput tool is already injected (mutually exclusive with
        the tool-use path to avoid double-injection).

    When all conditions hold, returns a fixed-schema block appended LAST in the
    prompt (same slot as F's output_discipline_directive) instructing the model
    to reply ONLY with a compact JSON object:
      {"findings": [...], "files_read": [...], "flag": "..."}

    This is the SECOND-ORDER lever: the read lane's output is the downstream
    synthesize lane's input. Shrinking read output → smaller synth input →
    cheaper synth lane even with no change to the synth lane's own directives.

    Q4 contract: schema is FIXED (prefix-stable, no new metadata channel)."""
    if not _read_summary_on():
        return ""
    if lane_type != "read":
        return ""
    # Mutually exclusive with StructuredOutput tool injection
    if tools and isinstance(tools, list):
        for tool in tools:
            if isinstance(tool, dict):
                name = tool.get("name") or ""
                if is_structured_output_tool_name(name):
                    return ""
    return (
        "READ LANE SUMMARY REQUIREMENT: Your output is consumed by a downstream "
        "lane — reply with ONLY this JSON object and nothing else: "
        "{\"findings\": [\"<terse bullet, max 5>\"], "
        "\"files_read\": [\"<filename>\"], "
        "\"flag\": \"<empty, or one sentence if the task is too large for one lane>\"}. "
        "Do NOT paste raw file contents. No prose, no narration. Emit the JSON directly."
    )


def _tool_choice_forces(payload: JSON, tool_name: str) -> bool:
    """True when the caller forced this exact tool via tool_choice (Anthropic
    {type:'tool',name} or OpenAI {type:'function',function:{name}}) or via a
    blanket 'required'/'any'/{type:'any'} choice. A forced choice means the lane
    cannot proceed without a tool_use, so the gateway must guarantee one."""
    choice = payload.get("tool_choice")
    if isinstance(choice, str):
        return choice in {"required", "any"}
    if isinstance(choice, dict):
        ctype = str(choice.get("type") or "")
        if ctype in {"any", "required"}:
            return True
        name = tool_name_from_schema(choice)
        return bool(name) and is_structured_output_tool_name(name) and (
            not tool_name or name == tool_name
        )
    return False


# ---------------------------------------------------------------------------
# Lever G — reject-on-overscope (CLAUDE_REASONIX_GATEWAY_OVERSCOPE_REJECT,
# default OFF). When on, the gateway refuses a lane whose declared file scope
# is too large and returns a structured error telling the controller to
# decompose into per-file lanes. Byte-inert when off.
# ---------------------------------------------------------------------------

_PREFETCH_PATH_RE = re.compile(
    r"""(?<![\w./-])             # not mid-token
        (                        # capture the path
          (?:[\w./-]+/)?         # optional dir segments
          [\w-]+                 # stem
          \.(?:py|pyx|pyi|js|mjs|cjs|ts|tsx|jsx|md|json|jsonl|sh|bash|zsh|
              txt|toml|yaml|yml|cfg|ini|rs|go|java|c|h|cpp|hpp|rb|php|sql|html|css)
        )
        (?![\w/])                # not followed by more path chars
    """, re.VERBOSE)

_OVERSCOPE_BULK_RE = re.compile(
    # strong bulk verb + (optional whole/entire/full) + codebase/repo/project, where
    # the scope noun is at a phrase boundary (NOT followed by another noun like README/
    # plan/layout — "review the project plan in x.md" is a narrow lane, must NOT fire).
    # The qualifier is OPTIONAL so bare "audit the codebase" (the common 833-file shape)
    # still fires. Weak verbs (read/check/look-at) are excluded here: "read the project
    # README" is narrow; only audit/scan/analyze-style verbs imply whole-tree ingestion.
    r"\b(audit|scan|analyze|examine|inspect|go through)\s+"
    r"(the\s+)?(whole\s+|entire\s+|full\s+)?(codebase|repo|repository|project)\b"
    r"(?!\s+\w*(readme|plan|layout|file|doc|spec|config|structure))"
    # "all files in/under/across", "all (the) source files", "every file"
    r"|\ball\s+(the\s+)?(source\s+)?files?\b"
    r"|\bevery\s+(source\s+)?file\b"
    # "everything in/under src", "the whole repo/codebase"
    r"|\beverything\s+(in|under|across)\b"
    r"|\bthe\s+whole\s+(repo|codebase|repository|project)\b",
    re.I)


def _overscope_on() -> bool:
    return env_truthy("CLAUDE_REASONIX_GATEWAY_OVERSCOPE_REJECT",
                      os.getenv("CLAUDE_CODEX_GATEWAY_OVERSCOPE_REJECT", "0"))


def _overscope_max_files() -> int:
    return env_int("CLAUDE_REASONIX_GATEWAY_OVERSCOPE_MAX_FILES",
                   "CLAUDE_CODEX_GATEWAY_OVERSCOPE_MAX_FILES", default=10)


def lane_file_scope_count(task_text: str, cwd: str | None) -> int:
    """Count DISTINCT existing files the task text literally names under cwd (the same
    exists-under-cwd resolve as predict_prefetch_files, copied so the gateway is
    standalone). A token that does not resolve to a real file is not counted."""
    if not task_text or not cwd:
        return 0
    try:
        base = Path(cwd).expanduser().resolve()
    except Exception:
        return 0
    seen: set[str] = set()
    for match in _PREFETCH_PATH_RE.finditer(task_text):
        token = match.group(1)
        candidate = (base / token) if not os.path.isabs(token) else Path(token)
        try:
            resolved = candidate.expanduser().resolve()
        except Exception:
            continue
        if resolved.is_file():
            seen.add(str(resolved))
    return len(seen)


# Stable opening marker of the gateway-injected PREFIX_GUIDE advisory (hooks/
# reasonix-workflow.py PREFIX_GUIDE_TEXT) and its closing sentence. The guide is
# CACHE ADVICE appended to a Workflow's additionalContext — it is NOT the lane's
# task, but it contains scope words ("the full text of every file under review",
# "crams everything into one context") that would otherwise trip the bulk-scope
# classifier and reject EVERY lane. Scope classification must run on the real task,
# so we strip this contiguous advisory block before classifying.
_GUIDE_OPEN_MARKER = "PROMPT-CACHE NOTE for this Dynamic Workflow:"
_GUIDE_CLOSE_MARKER = "This is advisory — correctness first"


def _strip_injected_guide(text: str) -> str:
    """Remove the gateway-injected PREFIX_GUIDE advisory block from `text` so scope
    classification keys off the lane's actual task, not the cache-advice. The guide is
    a contiguous block from `_GUIDE_OPEN_MARKER` to the end of its closing sentence;
    the real task text comes before and/or after it. If the open marker is absent this
    is a no-op (byte-identical), so non-workflow lanes are unaffected."""
    if not text or _GUIDE_OPEN_MARKER not in text:
        return text
    start = text.find(_GUIDE_OPEN_MARKER)
    close = text.find(_GUIDE_CLOSE_MARKER, start)
    if close != -1:
        # cut through the end of the closing sentence (to the next newline or EOS)
        eol = text.find("\n", close)
        end = len(text) if eol == -1 else eol
    else:
        # closer missing (truncated guide) — drop to end of text; the guide is always
        # appended LAST, so everything from the marker on is advisory.
        end = len(text)
    return (text[:start] + text[end:]).strip()


def overscope_rejection(task_text: str, cwd: str | None) -> str | None:
    """None unless OVERSCOPE_REJECT is on AND the lane is over-broad (a bulk
    non-enumerable scope phrase, OR > max-files distinct named files). When it fires,
    returns a structured error telling the controller to decompose into per-file lanes."""
    if not _overscope_on():
        return None
    # Classify the LANE TASK, not the gateway-injected cache-advice (see
    # _strip_injected_guide): the guide's "every file under review" / "everything in
    # one context" phrases would otherwise reject every lane as bulk scope.
    pt = _strip_injected_guide(task_text or "")
    bulk = bool(_OVERSCOPE_BULK_RE.search(pt))
    n = lane_file_scope_count(pt, cwd)
    if not bulk and n <= _overscope_max_files():
        return None
    reason = ("a bulk codebase/directory scope" if bulk
              else f"{n} named files (> {_overscope_max_files()})")
    return ("LANE REJECTED (overscope): this lane covers " + reason + ". A single DeepSeek-flash "
            "lane that ingests many files balloons input tokens and collapses cache (measured: one "
            "833-file lane = 532K tokens, 75% cache, 18 min). DECOMPOSE: emit one lane per file / "
            "module / focused question via parallel(), then one synthesize lane. Re-dispatch as "
            "narrow lanes.")


def openai_messages_to_prompt(messages: list[JSON], tools: Any = None) -> str:
    # PREFIX-CACHE STABILITY: the shared, lane-invariant blocks (the leading system
    # message + the tools/structured-output instruction) are emitted FIRST and
    # CONTIGUOUSLY, before any conversation history. Previously the tools
    # instruction was appended LAST, so on multi-turn lanes the per-lane
    # ASSISTANT/USER history sat BETWEEN the shared task and the shared tools
    # instruction — splitting the prefix at ~char 3953 (measured). Hoisting the
    # tools instruction ahead of history makes the shared prefix one long
    # contiguous block identical across lanes, so DeepSeek caches more of it.
    rendered: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")
        content = text_from_content(message.get("content"))
        if not content and message.get("tool_calls"):
            content = json.dumps(message.get("tool_calls"), ensure_ascii=False)
        if content:
            rendered.append((role, f"{role.upper()}:\n{content}"))

    # Two kinds of tool instruction with OPPOSITE placement needs:
    #  - generic "tools were provided" note: lane-invariant -> hoist to front for
    #    prefix-cache stability (no effect on output content).
    #  - StructuredOutput schema+requirement: an INSTRUCTION the model must obey on
    #    THIS turn. It must sit LAST, right after the task, or the model answers the
    #    task in prose and ignores the JSON requirement (measured: schema hoisted to
    #    front -> DeepSeek returned prose -> workflow "no StructuredOutput" failure).
    #    Correctness beats the small cache loss for structured lanes.
    structured_instruction = structured_output_prompt_instruction(tools) if tools else ""
    generic_tools_block = None
    if tools and not structured_instruction:
        generic_tools_block = (
            "AVAILABLE CLAUDE CODE TOOL SCHEMAS WERE PROVIDED TO THE MODEL, "
            "but this Reasonix-backed gateway executes the worker task directly through Reasonix CLI. "
            "Use Reasonix CLI repository and shell capabilities instead of returning tool calls."
        )

    # Emit the leading run of system messages first, then the hoistable generic
    # tools note, then everything else (task + per-lane history), and finally the
    # structured-output requirement LAST so it is the freshest instruction.
    lead_system: list[str] = []
    rest: list[str] = []
    seen_non_system = False
    for role, text in rendered:
        if role == "system" and not seen_non_system:
            lead_system.append(text)
        else:
            seen_non_system = True
            rest.append(text)

    parts: list[str] = [*lead_system]
    # Context-budget guard: a lane that can read files/run commands must work within
    # a read budget so it doesn't balloon its own context (measured: 833 reads ->
    # 532K tokens -> 75% cache). Lane-invariant, so it sits at the FRONT with the
    # other shared blocks and does not break the prefix. Only for tool-capable lanes.
    if tools:
        guard = context_budget_directive()
        if guard:
            parts.append(guard)
    if generic_tools_block:
        parts.append(generic_tools_block)
    # Lever C (default off): inject cached read-summaries at the FIXED boundary —
    # AFTER the shared/lane-invariant blocks (system + guard + generic-tools note),
    # BEFORE the per-lane task+history (`rest`). The block is byte-deterministic for a
    # given set of referenced files (sorted, fixed-format, normalize_prefix-clean), so
    # two lanes that reference the same cached files share these bytes and the prefix
    # is NOT forked. Built from the per-lane TASK text (`rest`), not `parts`, so the
    # block reflects only the files this lane actually references. Off => zero
    # injection, byte-identical to pre-C (enforced by test-read-cache-bytestable.py).
    cache_block = read_cache_injection_block("\n\n".join(rest))
    if cache_block:
        parts.append(cache_block)
    parts.extend(rest)
    if structured_instruction:
        parts.append(structured_instruction)
        # Heavy nested-schema synthesis on a large prompt: tell reasonix to use the
        # in-engine map-reduce skill instead of looping on a single oversized turn.
        # Appended AFTER the structured instruction so the schema stays LAST.
        assembled_len = sum(len(p) for p in parts)
        if is_heavy_synthesis(tools, assembled_len, "\n\n".join(parts)):
            parts.append(mapreduce_directive())
    # Lever F SOFT layer (default off). Appended LAST — after the task and the
    # structured/summary instruction — so the terse/diff-only directive is the
    # freshest instruction the model reads (correctness beats the tiny cache
    # loss, the same trade-off the structured instruction makes). The HARD layer
    # (output_discipline_budget -> maxOutputTokens) is applied at the call site.
    discipline = output_discipline_directive()
    if discipline:
        parts.append(discipline)
    # Lever A SOFT layer (default off). Appended LAST in the same slot as F's
    # directive. Only fires for read lanes when READ_SUMMARY is on AND no
    # StructuredOutput tool was already injected (mutually exclusive). The HARD
    # layer (read_summary_budget -> maxOutputTokens) is applied at the call site.
    # CLASSIFY FROM THE PER-LANE TASK TEXT (`rest`), NOT the assembled prompt:
    # the injected directives (F's "For edits… NEVER write/apply…", the structured
    # instruction, the cache block) all contain edit/read keywords that would
    # POISON the classifier — making every lane classify as 'edit' and silently
    # disabling F's per-type cap (measured: F's directive flipped read/review
    # lanes to 'edit', so the 512/2048 caps never applied). The task text is what
    # actually determines the lane's intent.
    _task_text = "\n\n".join(rest)
    _a_lane_type = classify_lane_type(tools, _task_text)
    read_summary = read_lane_summary_instruction(_a_lane_type, tools)
    if read_summary:
        parts.append(read_summary)
    return "\n\n".join(parts).strip() or "Complete the requested Reasonix worker task."


def requested_structured_output_tool(payload: JSON) -> str:
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return ""

    tool_names = [
        tool_name_from_schema(tool)
        for tool in tools
        if isinstance(tool, dict)
    ]
    tool_names = [name for name in tool_names if name]
    structured_names = [name for name in tool_names if is_structured_output_tool_name(name)]
    if not structured_names:
        return ""

    tool_choice = payload.get("tool_choice")
    if isinstance(tool_choice, dict):
        choice_name = tool_name_from_schema(tool_choice)
        if choice_name and not is_structured_output_tool_name(choice_name):
            return ""
        if choice_name:
            return choice_name
    return structured_names[0]


def tool_name_from_schema(tool: JSON) -> str:
    direct = str(tool.get("name") or "")
    if direct:
        return direct
    function = tool.get("function")
    if isinstance(function, dict):
        return str(function.get("name") or "")
    return ""


def tool_names_from_payload(payload: JSON) -> list[str]:
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return []
    return [
        name
        for name in (tool_name_from_schema(tool) for tool in tools if isinstance(tool, dict))
        if name
    ]


def is_structured_output_tool_name(name: str) -> bool:
    normalized = "".join(ch for ch in name.lower() if ch.isalnum())
    return normalized == "structuredoutput" or normalized.endswith("structuredoutput")


def structured_output_success_text(content: Any) -> bool:
    return "structured output provided successfully" in text_from_content(content).lower()


def anthropic_has_successful_structured_output(messages: Any) -> bool:
    if not isinstance(messages, list):
        return False

    structured_use_ids: set[str] = set()
    saw_structured_use = False
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            if is_structured_output_tool_name(str(block.get("name") or "")):
                saw_structured_use = True
                use_id = str(block.get("id") or "")
                if use_id:
                    structured_use_ids.add(use_id)

    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            if not structured_output_success_text(block.get("content")):
                continue
            use_id = str(block.get("tool_use_id") or "")
            if use_id in structured_use_ids or (saw_structured_use and not use_id):
                return True
    return False


def parse_json_object_from_text(text: str) -> JSON | None:
    raw = text.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()

    candidates = [raw]
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        candidates.append(raw[start : end + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def anthropic_tool_use_response(requested_model: str, tool_name: str, tool_input: JSON, usage: JSON) -> JSON:
    return {
        "id": f"msg_{uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "model": requested_model,
        "content": [
            {
                "type": "tool_use",
                "id": f"toolu_{uuid4().hex}",
                "name": tool_name,
                "input": tool_input,
            }
        ],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": usage,
    }


def anthropic_end_turn_response(requested_model: str, usage: JSON | None = None, text: str = "") -> JSON:
    return {
        "id": f"msg_{uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "model": requested_model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": usage or {"input_tokens": 0, "output_tokens": 0},
    }


def weighted_cache(rows: list[JSON]) -> JSON:
    """Weighted cache-hit rate over reasonix-cost rows: sum(in*cache%)/sum(in).
    Only rows with a numeric cache_pct count; returns zeros on empty."""
    total_in = 0
    hit = 0.0
    n = 0
    for r in rows:
        it = r.get("input_tokens") or 0
        cp = r.get("cache_pct")
        if isinstance(cp, (int, float)):
            total_in += it
            hit += it * cp / 100.0
            n += 1
    miss = total_in - hit
    return {
        "weighted_pct": (100.0 * hit / total_in) if total_in else 0.0,
        "total_in": total_in,
        "total_miss": int(round(miss)),
        "n": n,
    }


def classify_miss(rows: list[JSON]) -> JSON:
    """Bucket missed tokens into cold_prefix (fixable by prime gate), loop_inflation
    (big lanes re-fed history, fixable by loop-breaker/map-reduce), and unique_tail
    (genuinely novel content). Heuristic by input size + cache band."""
    cold = loop = unique = 0
    for r in rows:
        it = r.get("input_tokens") or 0
        cp = r.get("cache_pct")
        if not isinstance(cp, (int, float)):
            continue
        miss = int(round(it * (1 - cp / 100.0)))
        if it > 150_000:
            loop += miss
        elif cp < 60 and it < 30_000:
            unique += miss
        else:
            cold += miss
    return {"cold_prefix": cold, "loop_inflation": loop, "unique_tail": unique}


def append_reasonix_cost(ledger_path: str, usage: JSON, cwd: str = "", model: str = "",
                         claude_equiv: float | None = None, lane_type: str = "unknown") -> None:
    """Append one per-lane cost record to the session cost ledger (JSONL).

    Fail-open: a broken/unwritable ledger path must never break a lane.
    The reasonix CLI's own ~/.reasonix/usage.jsonl has session=null and no cwd,
    so it can't attribute cost to a session/project — this ledger adds cwd + ts.
    `lane_type` classifies the lane (read/edit/review/workflow/...); the caller
    passes "unknown" until Task 2 wires real classification.
    """
    try:
        record = {
            "ts": time.time(),
            "cost_usd": usage.get("reasonix_cost_usd"),
            "claude_equiv_usd": claude_equiv,
            "cache_pct": usage.get("reasonix_cache_pct"),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "lane_type": lane_type,
            "cwd": cwd,
            "model": model,
        }
        path = Path(ledger_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def summarize_reasonix_cost(ledger_path: str) -> JSON:
    """Aggregate the cost ledger into a summary dict. Missing/empty → zeros."""
    lanes = 0
    total = 0.0
    claude_equiv = 0.0
    in_tok = 0
    out_tok = 0
    cache_vals: list[float] = []
    try:
        with open(ledger_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                lanes += 1
                c = rec.get("cost_usd")
                if isinstance(c, (int, float)):
                    total += float(c)
                ce = rec.get("claude_equiv_usd")
                if isinstance(ce, (int, float)):
                    claude_equiv += float(ce)
                if isinstance(rec.get("input_tokens"), int):
                    in_tok += rec["input_tokens"]
                if isinstance(rec.get("output_tokens"), int):
                    out_tok += rec["output_tokens"]
                cp = rec.get("cache_pct")
                if isinstance(cp, (int, float)):
                    cache_vals.append(float(cp))
    except FileNotFoundError:
        pass
    except Exception:
        pass
    avg_cache = round(sum(cache_vals) / len(cache_vals), 1) if cache_vals else 0.0
    saved = claude_equiv - total
    saved_pct = round(100.0 * saved / claude_equiv, 1) if claude_equiv > 0 else 0.0
    return {
        "lanes": lanes,
        "total_usd": total,
        "claude_equiv_usd": claude_equiv,
        "saved_usd": saved,
        "saved_pct": saved_pct,
        "avg_cache_pct": avg_cache,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "avg_per_lane_usd": round(total / lanes, 6) if lanes else 0.0,
    }


def retry_cap_for_empty(orig_cap: int | None, was_empty: bool, force: bool) -> int | None:
    """Decide the escalated output cap for an EMPTY-on-truncation retry. Returns the
    new (higher) cap to retry with, or None = do not retry.

    Root cause (measured, real DeepSeek): an A-capped read lane over a LARGE file
    spends its small output cap on tool-calls + reasoning + the file outline and gets
    TRUNCATED before emitting the answer, so the engine returns empty text. Empty rate
    scales with cap tightness (cap 512 ~50% empty, 1024 ~17%, no cap ~0%). Retrying the
    SAME cap is pointless (the budget is the cause); retrying at a HIGHER cap gives the
    model room to finish (verified: recovers 2/2). Only escalates when the lane was
    actually capped (orig_cap not None) and Lever A asked for this (force)."""
    if not force or not was_empty or orig_cap is None:
        return None
    try:
        mult = float(os.getenv(
            "CLAUDE_REASONIX_GATEWAY_READ_RETRY_CAP_MULT",
            os.getenv("CLAUDE_CODEX_GATEWAY_READ_RETRY_CAP_MULT", "2")))
    except (TypeError, ValueError):
        mult = 2.0
    new_cap = int(orig_cap * mult)
    return new_cap if new_cap > orig_cap else None


def run_reasonix_acp(prompt: str, config: JSON, max_output_tokens: int | None = None,
                     retry_empty_force: bool = False, harness: JSON | None = None) -> tuple[str, JSON]:
    # TEST HOOK: simulate reasonix's reply WITHOUT spawning the CLI / hitting
    # DeepSeek, so an e2e test can drive the FULL real path — including the
    # parse-text->StructuredOutput-tool_use and forced-fallback logic in
    # call_openai_compatible, which is exactly where workflow lanes live or die and
    # which the old text-only MOCK mode skipped entirely. Set
    # CLAUDE_REASONIX_GATEWAY_MOCK_REASONIX_TEXT to the text reasonix should "return"
    # (e.g. a JSON object, or prose to test the narrate->fallback path).
    _mock_text = os.getenv("CLAUDE_REASONIX_GATEWAY_MOCK_REASONIX_TEXT", os.getenv("CLAUDE_CODEX_GATEWAY_MOCK_REASONIX_TEXT"))
    # The general GATEWAY_MOCK switch must also short-circuit this path: lanes routed
    # through /v1/chat/completions (provider reasonix_cli) reach run_reasonix_acp,
    # and without a mock here they spawn the real CLI and hang in a CI/test env that
    # has no reasonix. Fall back to a deterministic reply so the full path is tested.
    if _mock_text is None and os.getenv(
        "CLAUDE_REASONIX_GATEWAY_MOCK", os.getenv("CLAUDE_CODEX_GATEWAY_MOCK", "")
    ).lower() in {"1", "true", "yes", "on"}:
        _mock_text = f"mock reasonix response for {prompt[:60]}"
    if _mock_text is not None:
        return _mock_text, {
            "input_tokens": max(1, len(prompt) // 4), "output_tokens": max(1, len(_mock_text) // 4),
            "cache_pct": 0.0, "reasonix_cost_usd": 0.0, "reasonix_cache_pct": 0.0,
        }
    # ENGINE SEAM: run ONE lane through the in-process owner's-fork engine shim
    # (`node engine/run-lane.mjs`) instead of spawning upstream `reasonix acp`.
    # The shim imports the built fork dist, constructs DeepSeekClient +
    # ImmutablePrefix + CacheFirstLoop + buildCodeToolset, drives loop.step() with
    # stream:true + session:undefined (ephemeral), and prints ONE JSON line:
    #   {text, usage:{prompt_tokens, completion_tokens,
    #                 prompt_cache_hit_tokens, prompt_cache_miss_tokens,
    #                 cache_hit_ratio}, cost_usd}
    # We re-map THAT to the gateway's internal usage dict (input_tokens /
    # output_tokens / cache_pct / reasonix_cost_usd / reasonix_cache_pct), which
    # downstream cost/cache logging + the realworld-bench cache metric consume.
    # The shim is JUST the lane producer — the gateway's streaming/heartbeat/
    # prime-gate/keepalive machinery (below + in send_sse_response_lazy) is
    # unchanged. A one-shot subprocess per lane is behaviourally identical to the
    # old per-lane acp spawn; DeepSeek's cache hits come from its server-side
    # prefix cache (same prefix bytes), not from any in-memory engine state.
    #
    # Resolve the install home the same way the gateway resolves its own dir (the
    # gateway lives at <INSTALL_HOME>/reasonix-native-gateway.py), so the shim is
    # at <INSTALL_HOME>/engine/run-lane.mjs.
    install_home = env_first(
        "CLAUDE_REASONIX_FLEET_INSTALL_HOME", "CLAUDE_CODEX_FLEET_INSTALL_HOME",
        default=os.path.dirname(os.path.abspath(__file__)),
    )
    shim_path = os.path.join(install_home, "engine", "run-lane.mjs")
    node_bin = env_first("CLAUDE_REASONIX_NODE_BIN", "NODE_BIN", default="node")
    model = str(config.get("target_model") or "deepseek-v4-flash")
    effort = env_first("CLAUDE_REASONIX_REASONIX_EFFORT", "CLAUDE_CODEX_REASONIX_EFFORT", default="high")
    budget = env_first("CLAUDE_REASONIX_REASONIX_BUDGET", "CLAUDE_CODEX_REASONIX_BUDGET", default="0.05")
    timeout = float(env_first("CLAUDE_REASONIX_GATEWAY_TIMEOUT", "CLAUDE_CODEX_GATEWAY_CODEX_TIMEOUT", "REASONIX_FLEET_TIMEOUT_SECONDS", default="600"))
    cwd = env_first("CLAUDE_REASONIX_GATEWAY_CWD", "CLAUDE_CODEX_GATEWAY_CODEX_CWD", default=os.getcwd())
    # Lever D — pre-index (default OFF). The gateway is the SOLE build trigger:
    # build the semantic index ONCE per codebase here, before any lane spawns, so
    # per-lane shims only check `indexCompatible()` read-only (no JSONL append
    # race). FAIL-OPEN: build_preindex never raises and is a no-op when PREINDEX is
    # off or no embedding model is reachable — the lane proceeds either way.
    if preindex_enabled():
        try:
            build_preindex(cwd)
        except Exception as _preindex_exc:  # belt-and-suspenders: must never block lanes
            gateway_trace("preindex_fail_open", reason="unexpected", error=str(_preindex_exc))
    max_attempts = max(1, env_int("CLAUDE_REASONIX_GATEWAY_MAX_ATTEMPTS", "CLAUDE_CODEX_GATEWAY_CODEX_MAX_ATTEMPTS", default=3))
    max_iter = max(1, env_int("CLAUDE_REASONIX_GATEWAY_MAX_ITER_PER_TURN", "CLAUDE_CODEX_GATEWAY_MAX_ITER_PER_TURN", default=50))
    semaphore = reasonix_cli_semaphore()
    # The lane system prompt: the gateway prepends the role/system text into the
    # prompt today (openai_messages_to_prompt builds a single prompt string), so
    # the shim's `system` is empty and the full instruction rides in `prompt` —
    # preserving the exact prefix bytes DeepSeek caches. An explicit override is
    # available for callers that want to split system out.
    system_text = str(config.get("system") or os.getenv("CLAUDE_REASONIX_LANE_SYSTEM", ""))

    # The shim is `node`; if the gateway was launched with a stripped PATH that
    # lacks the node dir, propagate the reasonix-bin dir (which historically holds
    # node) so `node` resolves regardless of how the gateway was started. Honor
    # REASONIX_ENGINE_DIST + DeepSeek auth via the child env.
    shim_env = dict(os.environ)
    # When this lane runs with the harness engaged (gateway flag on + an
    # ACCEPTANCE_TEST line present), turn ON the shim's harness gate in the child
    # env so the single gateway flag activates the whole chain (the shim gates its
    # retry loop on its OWN REASONIX_LANE_HARNESS). Only set when engaged — when the
    # harness is off this is never touched, so the child env is byte-identical.
    if harness:
        shim_env["REASONIX_LANE_HARNESS"] = "1"
    _reasonix_bin = env_first("REASONIX_BIN", default="")
    _bin_dir = os.path.dirname(os.path.abspath(_reasonix_bin)) if (_reasonix_bin and os.path.sep in _reasonix_bin) else ""
    if _bin_dir and os.path.exists(os.path.join(_bin_dir, "node")):
        _cur_path = shim_env.get("PATH", "")
        if _bin_dir not in _cur_path.split(os.pathsep):
            shim_env["PATH"] = _bin_dir + (os.pathsep + _cur_path if _cur_path else "")
    # Resolve the engine dist (the built fork). Default to the bundled vendor copy
    # next to the install home if not explicitly set; the shim has its own
    # fallback too, but setting it here keeps the resolution observable.
    if not shim_env.get("REASONIX_ENGINE_DIST"):
        _vendored = os.path.join(install_home, "vendor", "reasonix-engine", "dist", "index.js")
        if os.path.exists(_vendored):
            shim_env["REASONIX_ENGINE_DIST"] = _vendored

    def _attempt(cap_override: int | None = None) -> tuple[str, JSON]:
        request = {
            "prompt": prompt,
            "system": system_text,
            "rootDir": cwd,
            "model": model,
            "maxIterPerTurn": max_iter,
            # carried for parity/observability; the shim ignores unknown fields.
            "effort": effort,
            "budget": budget,
        }
        # C3: forward harness fields to the shim ONLY when harness is provided.
        # When harness is None (default, flag off) the request dict above is
        # byte-identical to the pre-harness baseline — byte-inert guarantee.
        if harness:
            request["acceptanceTest"] = harness["acceptanceTest"]
            request["budgetUsd"] = harness["budgetUsd"]
            request["harnessMaxAttempts"] = harness["harnessMaxAttempts"]
        # cap_override lets the empty-on-truncation retry re-run at a higher cap.
        # Sentinel: cap_override==0 means EXPLICITLY uncapped (omit maxOutputTokens);
        # None means "use the lane's original cap".
        if cap_override == 0:
            _cap = None
        else:
            _cap = cap_override if cap_override is not None else max_output_tokens
        if _cap is not None:
            request["maxOutputTokens"] = _cap
        try:
            proc = subprocess.Popen(
                [node_bin, shim_path],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, cwd=cwd, env=shim_env,
            )
        except OSError as exc:
            raise GatewayError(502, "reasonix_acp_error", f"failed to start engine shim: {exc}")
        try:
            stdout_text, stderr_text = proc.communicate(
                input=json.dumps(request) + "\n", timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.communicate(timeout=2)
            except Exception:
                pass
            _mk = lane_unverified_reply(f"engine shim timed out after {timeout:g}s")
            if _mk:
                return _mk, {"input_tokens": 0, "output_tokens": estimate_tokens({"text": _mk}),
                             "cache_pct": None, "reasonix_cost_usd": 0.0,
                             "reasonix_cache_pct": None, "reasonix_claude_equiv_usd": None}
            raise GatewayError(504, "reasonix_timeout", f"engine shim timed out after {timeout:g}s")
        if proc.returncode != 0:
            detail = (stderr_text or "").strip()[:500] or f"engine shim exited {proc.returncode}"
            raise GatewayError(502, "reasonix_acp_error", f"engine shim failed: {detail}")

        # The shim prints ONE JSON line on stdout. Parse the last non-empty line.
        out_line = ""
        for line in (stdout_text or "").splitlines():
            if line.strip():
                out_line = line.strip()
        if not out_line:
            raise GatewayError(502, "reasonix_acp_error", "engine shim produced no output")
        try:
            parsed = json.loads(out_line)
        except Exception as exc:
            raise GatewayError(502, "reasonix_acp_error", f"engine shim emitted non-JSON: {exc}")

        text = str(parsed.get("text") or "")
        su = parsed.get("usage") or {}
        in_tok = su.get("prompt_tokens")
        out_tok = su.get("completion_tokens")
        hit = su.get("prompt_cache_hit_tokens")
        miss = su.get("prompt_cache_miss_tokens")
        ratio = su.get("cache_hit_ratio")
        cost = parsed.get("cost_usd")
        # cache percent: prefer the shim's ratio (0..1 -> 0..100); fall back to
        # hit/(hit+miss) so the metric is non-null whenever token counts exist.
        cache = None
        if isinstance(ratio, (int, float)):
            cache = round(100.0 * float(ratio), 1)
        elif isinstance(hit, (int, float)) and isinstance(miss, (int, float)) and (hit + miss) > 0:
            cache = round(100.0 * float(hit) / float(hit + miss), 1)

        usage = {
            "input_tokens": int(in_tok) if isinstance(in_tok, (int, float))
            else estimate_tokens({"messages": [{"role": "user", "content": prompt}]}),
            "output_tokens": int(out_tok) if isinstance(out_tok, (int, float)) else max(1, len(text) // 4),
            # cache_pct is the ledger key (append_reasonix_cost reads reasonix_cache_pct
            # into a row's cache_pct); set both so cost/cache logging + realworld-bench
            # keep working.
            "cache_pct": cache,
            "reasonix_cost_usd": cost,
            "reasonix_cache_pct": cache,
            "reasonix_claude_equiv_usd": None,
        }
        # Prefix-cache diagnostics (opt-in via CLAUDE_REASONIX_GATEWAY_PREFIX_TRACE).
        # Unchanged from the acp path — hashes of the prompt prefix + this lane's
        # cache%, append-only JSONL, prompt text never logged.
        if os.getenv("CLAUDE_REASONIX_GATEWAY_PREFIX_TRACE", os.getenv("CLAUDE_CODEX_GATEWAY_PREFIX_TRACE", "")).lower() in {"1", "true", "yes", "on"}:
            try:
                import hashlib
                pfx4 = hashlib.sha1(prompt[:4096].encode("utf-8", "ignore")).hexdigest()[:12]
                pfx32 = hashlib.sha1(prompt[:32768].encode("utf-8", "ignore")).hexdigest()[:12]
                chunks = [prompt[i:i + 4096] for i in range(0, min(len(prompt), 131072), 4096)]
                chunk_hashes = [hashlib.sha1(c.encode("utf-8", "ignore")).hexdigest()[:10] for c in chunks]
                chunk_samples = [c[:80] for c in chunks]
                rec = {
                    "ts": _time.time(),
                    "prefix4k": pfx4,
                    "prefix32k": pfx32,
                    "prompt_len": len(prompt),
                    "cache_pct": cache,
                    "in_tok": usage["input_tokens"],
                    "chunk_hashes": chunk_hashes,
                    "chunk_samples": chunk_samples,
                }
                ledger_dir = Path(env_first(
                    "CLAUDE_REASONIX_FLEET_HOME", "CLAUDE_CODEX_FLEET_HOME",
                    default=os.path.dirname(os.path.abspath(__file__)))) / "runtime"
                ledger_dir.mkdir(parents=True, exist_ok=True)
                with open(ledger_dir / "prefix-trace.jsonl", "a", encoding="utf-8") as _pf:
                    _pf.write(json.dumps(rec) + "\n")
            except Exception:
                pass
        return text, usage

    # Prefix-prime gate: the first lane of a shared-prefix burst warms DeepSeek's
    # cache alone; later lanes wait (bounded) for that warm-up, then run together.
    is_primer, prime_gate = acquire_prime_role(prompt)
    if os.getenv("CLAUDE_REASONIX_GATEWAY_PREFIX_TRACE", os.getenv("CLAUDE_CODEX_GATEWAY_PREFIX_TRACE", "")).lower() in {"1", "true", "yes", "on"}:
        try:
            _pdir = Path(env_first("CLAUDE_REASONIX_FLEET_HOME", "CLAUDE_CODEX_FLEET_HOME",
                default=os.path.dirname(os.path.abspath(__file__)))) / "runtime"
            _pdir.mkdir(parents=True, exist_ok=True)
            with open(_pdir / "prime-trace.jsonl", "a", encoding="utf-8") as _pf:
                _pf.write(json.dumps({
                    "ts": _time.time(),
                    "role": "primer" if is_primer else ("waiter" if prime_gate is not None else "ungated"),
                    "prime_key": prefix_prime_key(prompt),
                    "prompt_len": len(prompt),
                }) + "\n")
        except Exception:
            pass
    # Staggered serialization: the prime gate releases ALL waiters at once when it
    # opens, so the first few still fire concurrently and race the prefix persist
    # (measured: 3 early lanes 65-83% while later lanes 97-99%). To eliminate that,
    # the first PRIME_SERIAL lanes of the family take a per-key lock and run ONE AT
    # A TIME — each finishes and persists more of the shared prefix before the next
    # starts. Lanes past the window skip the lock and run in parallel against the
    # now-warm prefix. The primer is lane 0 of its family, so it holds the slot too;
    # waiters that wake hold subsequent slots and serialize behind it.
    prime_key = prefix_prime_key(prompt)
    serial_slot = acquire_serial_slot(prime_key)
    serial_lock = serial_lock_for(prime_key) if serial_slot else None

    if prime_gate is not None and not is_primer:
        wait_s = env_float("CLAUDE_REASONIX_GATEWAY_PRIME_WAIT_SECONDS", "CLAUDE_CODEX_GATEWAY_PRIME_WAIT_SECONDS", default=20.0)
        opened = prime_gate.wait(timeout=wait_s)
        # Post-open grace settle: DeepSeek persists the primed prefix in "seconds"
        # (per its cache docs), so let it finish writing before the waiters fire, or
        # they race the primer and miss the shared prefix. Measured: 1.5s let early
        # waiters race the primer (cache 65-81%); a few seconds lifts them to ~99%.
        # SKIP grace for serial-slot lanes: the per-key serial lock already forces
        # them to run strictly after the prior lane completes + its settle sleep, so
        # an extra grace here only adds dead wall-clock without improving the cache.
        if opened and serial_slot is False:
            grace = env_float("CLAUDE_REASONIX_GATEWAY_PRIME_GRACE_SECONDS", "CLAUDE_CODEX_GATEWAY_PRIME_GRACE_SECONDS", default=4.0)
            if grace > 0:
                _time.sleep(min(grace, 15.0))
    if serial_slot and os.getenv("CLAUDE_REASONIX_GATEWAY_PREFIX_TRACE", os.getenv("CLAUDE_CODEX_GATEWAY_PREFIX_TRACE", "")).lower() in {"1", "true", "yes", "on"}:
        try:
            _sdir = Path(env_first("CLAUDE_REASONIX_FLEET_HOME", "CLAUDE_CODEX_FLEET_HOME",
                default=os.path.dirname(os.path.abspath(__file__)))) / "runtime"
            _sdir.mkdir(parents=True, exist_ok=True)
            with open(_sdir / "prime-trace.jsonl", "a", encoding="utf-8") as _pf:
                _pf.write(json.dumps({
                    "ts": _time.time(), "event": "serial_slot",
                    "prime_key": prime_key, "prompt_len": len(prompt),
                }) + "\n")
        except Exception:
            pass

    # Retry-on-empty, SCOPED to isolated single-shot lanes only (Option C from the
    # fix-retry-empty-variance workflow, vetted by 2 adversarial lenses). reasonix-flash
    # intermittently returns empty text (~1/15) — a lost task. Retrying recovers it,
    # BUT a retry inside a shared-prefix BURST fires a fresh cold lookup late, after the
    # warm prefix has aged/been displaced, re-reading the full ~19K prompt as a MISS
    # (in_tok ~19135->42967) and dragging the run's weighted cache from 99.7% to ~94%
    # (measured). So retry ONLY when this lane is NOT part of a prime-gate burst
    # (prime_gate is None => an isolated lane with no same-family waiters): that keeps
    # empty-recovery for single subagent calls while review/fan-out bursts never inject
    # a cold mid-burst lane. Env CLAUDE_REASONIX_GATEWAY_RETRY_EMPTY: "burst" (default) =
    # isolated-only; "1"/"all" = always (legacy, re-introduces burst variance);
    # "0"/off = never.
    _re = os.getenv("CLAUDE_REASONIX_GATEWAY_RETRY_EMPTY", os.getenv("CLAUDE_CODEX_GATEWAY_RETRY_EMPTY", "burst")).lower()
    retry_empty_isolated = _re not in {"0", "false", "no", "off"}
    retry_empty_in_burst = _re in {"1", "true", "yes", "on", "all"}

    def _run_attempts() -> tuple[str, JSON]:
        last_exc: Exception | None = None
        last_result: tuple[str, JSON] | None = None
        # Only an isolated lane (no same-family burst) may retry on empty, unless
        # forced on for all. prime_gate is None => isolated.
        may_retry_empty = retry_empty_isolated and (retry_empty_in_burst or prime_gate is None)
        for attempt in range(1, max_attempts + 1):
            try:
                gateway_trace("reasonix_acp_attempt", model=model, attempt=attempt)
                result = _attempt()
            except GatewayError as exc:
                last_exc = exc
                if exc.error_type == "reasonix_timeout":
                    raise
                continue
            # Truncation recovery (Lever A): an A-capped read lane can spend its small
            # cap on tool-calls/reasoning/outline and get truncated before emitting the
            # answer -> empty text. A SAME-cap retry won't help (the budget is the
            # cause); re-run at a PROGRESSIVELY higher cap until the model can finish.
            # One 2x bump recovers most lanes, but the heaviest (a "walk through every
            # function" on a 134KB file) need more; escalate 2x, 4x, ... up to a final
            # UNCAPPED attempt (measured: no-cap = 0% hollow). Runs even mid-burst
            # (force) because a lost summary is worse than a few extra-budget lanes.
            # Only when the lane was actually capped (max_output_tokens set).
            if (not str(result[0]).strip()) and retry_empty_force and max_output_tokens is not None:
                _max_escalations = env_int(
                    "CLAUDE_REASONIX_GATEWAY_READ_RETRY_MAX_ESCALATIONS",
                    "CLAUDE_CODEX_GATEWAY_READ_RETRY_MAX_ESCALATIONS", default=3)
                _cap = max_output_tokens
                _recovered = False
                for _esc in range(1, _max_escalations + 1):
                    _bigger = retry_cap_for_empty(_cap, True, True)
                    # final escalation drops the cap entirely (the proven 0% case);
                    # 0 is the "explicitly uncapped" sentinel for _attempt.
                    _override = _bigger if _esc < _max_escalations else 0
                    gateway_trace("reasonix_acp_uncap_retry", model=model,
                                  attempt=attempt, escalation=_esc, new_cap=_override)
                    _r2 = _attempt(cap_override=_override)
                    if str(_r2[0]).strip():
                        return _r2
                    last_result = _r2
                    if _override == 0:
                        break  # already uncapped; nothing higher to try
                    _cap = _bigger
            if may_retry_empty and not str(result[0]).strip() and attempt < max_attempts:
                gateway_trace("reasonix_acp_empty_retry", model=model, attempt=attempt)
                last_result = result
                continue
            return result
        if last_result is not None:
            return last_result
        if last_exc:
            raise last_exc
        raise GatewayError(502, "reasonix_acp_error", "reasonix acp produced no result")

    def _run_serialized() -> tuple[str, JSON]:
        # A serial-slot lane runs under the per-key lock so only one family member
        # runs at a time; after it completes it sleeps a short settle so DeepSeek
        # persists what this lane just warmed before the next serial lane starts.
        if serial_lock is None:
            return _run_attempts()
        serial_lock.acquire()
        try:
            return _run_attempts()
        finally:
            settle = env_float("CLAUDE_REASONIX_GATEWAY_PRIME_SERIAL_SETTLE_SECONDS", "CLAUDE_CODEX_GATEWAY_PRIME_SERIAL_SETTLE_SECONDS", default=4.0)
            if settle > 0:
                _time.sleep(min(settle, 15.0))
            serial_lock.release()

    with semaphore:
        try:
            return _run_serialized()
        finally:
            # The primer must release waiters whether it succeeded or failed, so a
            # failed prime can't deadlock the burst. The warmed prefix (if any)
            # stays cached server-side regardless.
            if is_primer and prime_gate is not None:
                prime_gate.set()


def call_openai_chat_completion(payload: JSON, requested_model: str, config: JSON) -> JSON:
    if config.get("provider") == "reasonix_cli":
        # CCR routes every workflow subagent lane through /v1/chat/completions,
        # which lands here. Without this branch reasonix_cli fell through to the
        # api_key check below and 401'd with "needs an API key" — the real cause
        # of "Not logged in" on every workflow lane. Mirror the /v1/messages
        # reasonix path (run_reasonix_acp + cost ledger) but emit OpenAI shape.
        messages = payload.get("messages") or []
        normalized = [item for item in messages if isinstance(item, dict)]
        prompt = openai_messages_to_prompt(normalized, payload.get("tools"))
        register_lane_attempt(prompt)
        record_keepalive_prefix(prompt)
        lane_type = classify_lane_type(payload.get("tools"), lane_task_text(normalized))
        # Lever F HARD layer (default off): cap output by lane-type budget.
        # Lever A HARD layer (default off): for read lanes, read_summary_budget()
        # returns 512 when READ_SUMMARY is on.  Both levers agree on 512 for read
        # lanes; pick the tighter (smallest) non-None cap.
        _f_cap = output_discipline_budget(lane_type)
        _a_cap = read_summary_budget() if lane_type == "read" else None
        _caps = [c for c in (_f_cap, _a_cap) if c is not None]
        _max_out = min(_caps) if _caps else None
        # Lever G (default off): reject lanes whose file scope is too broad.
        _rej = overscope_rejection(lane_task_text(normalized),
                                   env_first("CLAUDE_REASONIX_GATEWAY_CWD",
                                             "CLAUDE_CODEX_GATEWAY_CODEX_CWD",
                                             default=os.getcwd()))
        if _rej is not None:
            return {
                "id": f"chatcmpl_{uuid4().hex}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": requested_model,
                "choices": [{"index": 0,
                              "message": {"role": "assistant", "content": _rej},
                              "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
        # Lever A truncation recovery (see /v1/messages path): retry an empty A-capped
        # read lane once at a higher cap. Same flag/gate.
        _retry_hollow = (_a_cap is not None) and env_truthy(
            "CLAUDE_REASONIX_GATEWAY_READ_RETRY_HOLLOW",
            "CLAUDE_CODEX_GATEWAY_READ_RETRY_HOLLOW", default="1")
        # C3: symmetric with /v1/messages path (flag off -> byte-identical).
        _harness = None
        if _lane_harness_on():
            _at = lane_acceptance_test(normalized)
            if _at:
                _harness = {
                    "acceptanceTest": _at,
                    "budgetUsd": env_float("CLAUDE_REASONIX_GATEWAY_LANE_BUDGET_USD",
                                          "CLAUDE_CODEX_GATEWAY_LANE_BUDGET_USD", default=0.05),
                    "harnessMaxAttempts": env_int("CLAUDE_REASONIX_GATEWAY_LANE_MAX_ATTEMPTS",
                                                  "CLAUDE_CODEX_GATEWAY_LANE_MAX_ATTEMPTS", default=4),
                }
        text, usage = run_reasonix_acp(
            prompt, config, max_output_tokens=_max_out,
            retry_empty_force=_retry_hollow, harness=_harness)
        # C3: fold harness reply BEFORE populate_read_cache / ledger.
        _hp = parse_harness_result(text)
        if _hp is not None:
            text = harness_lane_reply(_hp)
        # Lever C (default off): populate the shared read-cache from this lane's
        # summary (see /v1/messages path). No-op when off; best-effort.
        populate_read_cache(prompt, text)
        gateway_trace("reasonix_acp_openai_response", model=requested_model,
                      cost=usage.get("reasonix_cost_usd"), cache=usage.get("reasonix_cache_pct"))
        ledger = env_first(
            "CLAUDE_REASONIX_REASONIX_COST_LEDGER", "CLAUDE_CODEX_REASONIX_COST_LEDGER",
            default=str(Path(env_first("CLAUDE_REASONIX_FLEET_HOME", "CLAUDE_CODEX_FLEET_HOME",
                                       default=os.path.dirname(os.path.abspath(__file__)))) / "runtime" / "reasonix-cost.jsonl"),
        )
        append_reasonix_cost(
            ledger, usage,
            cwd=env_first("CLAUDE_REASONIX_GATEWAY_CWD", "CLAUDE_CODEX_GATEWAY_CODEX_CWD", default=os.getcwd()),
            model=str(config.get("target_model") or ""),
            claude_equiv=usage.get("reasonix_claude_equiv_usd"),
            lane_type=lane_type,
        )
        prompt_tokens = int(usage.get("prompt_tokens") or estimate_tokens(prompt))
        completion_tokens = int(usage.get("completion_tokens") or max(1, len(text) // 4))
        usage_block = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
        # Same StructuredOutput contract as the /v1/messages path: when a workflow
        # agent({schema}) lane (routed here via CCR /v1/chat/completions) asked for
        # a StructuredOutput tool, emit the model's JSON as a tool_calls response so
        # the harness gets the tool-call it requires instead of prose.
        structured_tool = requested_structured_output_tool(payload)
        if os.getenv("CLAUDE_REASONIX_GATEWAY_STRUCTURED_DEBUG", os.getenv("CLAUDE_CODEX_GATEWAY_STRUCTURED_DEBUG", "")).lower() in {"1", "true", "yes", "on"}:
            try:
                _dbg_dir = Path(env_first("CLAUDE_REASONIX_FLEET_HOME", "CLAUDE_CODEX_FLEET_HOME",
                    default=os.path.dirname(os.path.abspath(__file__)))) / "runtime"
                _dbg_dir.mkdir(parents=True, exist_ok=True)
                _parsed = parse_json_object_from_text(text) if structured_tool else None
                with open(_dbg_dir / "structured-debug.jsonl", "a", encoding="utf-8") as _df:
                    _df.write(json.dumps({
                        "ts": _time.time(), "path": "chat/completions",
                        "tool_names": tool_names_from_payload(payload),
                        "structured_tool": structured_tool,
                        "tool_choice": payload.get("tool_choice"),
                        "text_len": len(text), "text_head": text[:500],
                        "parsed_ok": _parsed is not None,
                    }, ensure_ascii=False) + "\n")
            except Exception:
                pass
        if structured_tool:
            tool_input = parse_json_object_from_text(text)
            if tool_input is not None:
                clear_lane_count(prompt)  # real output -> family not looping, reset count
            if tool_input is None and (_tool_choice_forces(payload, structured_tool) or should_force_fallback(prompt)):
                # Forced tool but the model narrated instead of emitting JSON, OR the
                # lane looped past the retry limit — synthesize a schema-valid object
                # so the lane completes (mirror of the /v1/messages path).
                tool_input = structured_timeout_fallback(
                    payload.get("tools"), structured_tool,
                    "model did not emit a JSON object; schema-valid fallback used",
                )
            if tool_input is not None:
                return {
                    "id": f"chatcmpl_{uuid4().hex}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": requested_model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": f"call_{uuid4().hex[:24]}",
                                        "type": "function",
                                        "function": {
                                            "name": structured_tool,
                                            "arguments": json.dumps(tool_input, ensure_ascii=False),
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": usage_block,
                }
        return {
            "id": f"chatcmpl_{uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": requested_model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": usage_block,
        }

    raise GatewayError(400, "unsupported_provider", f"unsupported provider: {config.get('provider')!r}; this gateway serves only claude-reasonix-flash")


class GatewayError(Exception):
    def __init__(self, status: int, error_type: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.error_type = error_type
        self.message = message


class ClientGone(Exception):
    """The streaming client disconnected mid-response (BrokenPipe/ConnectionReset).
    Normal, not an error — the handler stops streaming and does NOT try to write an
    error body down the dead socket."""


class Handler(BaseHTTPRequestHandler):
    server_version = "claude-reasonix-gateway/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        if os.getenv("CLAUDE_REASONIX_GATEWAY_QUIET", os.getenv("CLAUDE_CODEX_GATEWAY_QUIET", "1")).lower() in {"1", "true", "yes", "on"}:
            return
        super().log_message(fmt, *args)

    def read_json(self) -> JSON:
        length = int(self.headers.get("content-length") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception as exc:
            raise GatewayError(400, "invalid_request_error", f"invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise GatewayError(400, "invalid_request_error", "request body must be a JSON object")
        return data

    def send_json(self, status: int, data: Any, headers: dict[str, str] | None = None) -> None:
        body = json_bytes(data)
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, exc: GatewayError) -> None:
        self.send_json(exc.status, {"type": "error", "error": {"type": exc.error_type, "message": exc.message}})

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/health":
            self.send_json(200, {"ok": True, "time": time.time()})
            return
        if path == "/v1/models":
            models = [
                {
                    "id": model_id,
                    "type": "model",
                    "display_name": config["display_name"],
                    "created_at": 0,
                }
                for model_id, config in model_registry().items()
            ]
            self.send_json(200, {"data": models})
            return
        self.send_json(404, {"type": "error", "error": {"type": "not_found_error", "message": self.path}})

    def do_POST(self) -> None:
        try:
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path in {"/v1/chat/completions", "/chat/completions"}:
                payload = self.read_json()
                model = str(payload.get("model") or "")
                registry = model_registry()
                if model not in registry:
                    raise GatewayError(400, "invalid_request_error", f"unknown model: {model}")
                config = registry[model]
                provider = config.get("provider")
                if payload.get("stream"):
                    # reasonix_cli runs a blocking subprocess that can exceed the 180s
                    # workflow watchdog; it needs the heartbeat-lazy SSE path so a lane
                    # is not killed mid-run with no visible progress.
                    if provider == "reasonix_cli":
                        self.send_openai_sse_response_lazy(
                            lambda: call_openai_chat_completion(payload, model, config)
                        )
                    else:
                        response = call_openai_chat_completion(payload, model, config)
                        self.send_openai_sse_response(response)
                else:
                    response = call_openai_chat_completion(payload, model, config)
                    self.send_json(200, response)
                return
            if path == "/v1/messages/count_tokens":
                payload = self.read_json()
                self.send_json(200, {"input_tokens": estimate_tokens(payload)})
                return
            if path == "/v1/messages":
                payload = self.read_json()
                model = str(payload.get("model") or "")
                registry = model_registry()
                if model in registry:
                    config = registry[model]
                    # reasonix_cli runs a blocking subprocess that can take >180s. The
                    # Claude Code workflow watchdog interrupts an agent() lane at
                    # exactly 180s if it sees no visible content progress. So ALWAYS
                    # take the heartbeat-streaming path for reasonix_cli, regardless of
                    # the client's stream flag: ~34% of real lanes were sent without
                    # stream=true and died silently at 180s on the old blocking blob
                    # path. The Claude Code client parses the SSE stream fine even
                    # when it did not request stream=true.
                    provider = config.get("provider")
                    if provider == "reasonix_cli":
                        self.send_sse_response_lazy(
                            lambda: call_openai_compatible(payload, model, config),
                            model,
                        )
                    elif payload.get("stream"):
                        response = call_openai_compatible(payload, model, config)
                        self.send_sse_response(response)
                    else:
                        response = call_openai_compatible(payload, model, config)
                        self.send_json(200, response)
                    return
                self.forward_anthropic(payload)
                return
            self.send_json(404, {"type": "error", "error": {"type": "not_found_error", "message": self.path}})
        except (ClientGone, BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Client hung up mid-response. Nothing to send (the socket is dead) and
            # nothing to log — this is normal streaming churn, not a gateway fault.
            return
        except GatewayError as exc:
            self._safe_send_error(exc)
        except Exception as exc:
            if os.getenv("CLAUDE_REASONIX_GATEWAY_DEBUG", os.getenv("CLAUDE_CODEX_GATEWAY_DEBUG", "")).lower() in {"1", "true", "yes", "on"}:
                traceback.print_exc(file=sys.stderr)
            self._safe_send_error(GatewayError(500, "api_error", str(exc)))

    def _safe_send_error(self, exc: "GatewayError") -> None:
        # Sending the error body can itself hit a dead socket (the client that caused
        # the error may already be gone). Never let that raise a second, noisy
        # traceback — the original error is what matters.
        try:
            self.send_error_json(exc)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, ValueError):
            return

    def send_sse_event(self, event: str, data: Any) -> None:
        # A streaming client (CCR / the Claude Code workflow runtime) routinely
        # disconnects mid-stream — on timeout, cancel, or when a lane is superseded.
        # The socket write then raises BrokenPipe/ConnectionReset. That is NORMAL,
        # not a gateway error: swallow it and signal the caller to stop streaming so
        # we don't spew 272 tracebacks (measured in prod) or try to send an error
        # body down a dead socket. ClientGone is caught by the streaming loop.
        try:
            self.wfile.write(f"event: {event}\n".encode("utf-8"))
            self.wfile.write(b"data: ")
            self.wfile.write(json_bytes(data))
            self.wfile.write(b"\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as exc:
            raise ClientGone() from exc

    def wait_for_stream_response(self, producer: Any, on_keepalive: Any = None) -> Any:
        result_queue: queue.Queue = queue.Queue(maxsize=1)

        def worker() -> None:
            try:
                result_queue.put(("response", producer()))
            except Exception as exc:
                result_queue.put(("error", exc))

        threading.Thread(target=worker, daemon=True).start()
        interval = max(1.0, float(os.getenv("CLAUDE_REASONIX_GATEWAY_STREAM_KEEPALIVE_SECONDS", os.getenv("CLAUDE_CODEX_GATEWAY_STREAM_KEEPALIVE_SECONDS", "10"))))
        while True:
            try:
                kind, value = result_queue.get(timeout=interval)
            except queue.Empty:
                # An idle tick. For the Anthropic lazy path we emit a real
                # content_block_delta heartbeat (via on_keepalive) so the Claude
                # Code workflow watchdog sees visible content progress and does not
                # fire its no-progress interrupt while reasonix exec is still buffering.
                # A bare ": keepalive" SSE comment keeps the socket warm but is
                # invisible to that watchdog, so it is only the fallback.
                try:
                    if on_keepalive is not None:
                        on_keepalive()
                    else:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as exc:
                    raise ClientGone() from exc
                continue
            if kind == "error":
                raise value
            return value

    def send_sse_response(self, message: JSON) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()
        self.write_sse_response_body(message)

    def send_sse_response_lazy(self, producer: Any, model: str = "") -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()
        # Preamble: emit message_start + open a heartbeat text block at index 0
        # BEFORE the producer is awaited. Anthropic streaming requires message_start
        # to precede any content_block event, so the synthetic envelope must be sent
        # first; the real blocks are then emitted shifted to indices >= 1.
        start_message = {
            "id": f"msg_{uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
        self.send_sse_event("message_start", {"type": "message_start", "message": start_message})
        self.send_sse_event(
            "content_block_start",
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        )

        def on_keepalive() -> None:
            # A real content_block_delta (single space) resets the workflow watchdog.
            self.send_sse_event(
                "content_block_delta",
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": " "}},
            )

        try:
            message = self.wait_for_stream_response(producer, on_keepalive=on_keepalive)
        except Exception as exc:
            # message_start is already on the wire: close the heartbeat block, then error.
            self.send_sse_event("content_block_stop", {"type": "content_block_stop", "index": 0})
            self.send_sse_event("error", {"type": "error", "error": {"type": "api_error", "message": str(exc)}})
            return
        # Finalize: close heartbeat block, then emit the real blocks at indices >= 1
        # (do NOT re-emit message_start) followed by message_delta/message_stop.
        self.send_sse_event("content_block_stop", {"type": "content_block_stop", "index": 0})
        self.write_sse_response_body(message, start_index=1, emit_message_start=False)

    def write_sse_response_body(self, message: JSON, start_index: int = 0, emit_message_start: bool = True) -> None:
        if emit_message_start:
            start_message = dict(message)
            start_message["content"] = []
            self.send_sse_event("message_start", {"type": "message_start", "message": start_message})
        next_index = start_index
        emitted_real = 0
        for index, block in enumerate(message.get("content") or [], start=start_index):
            next_index = index + 1
            block_type = block.get("type")
            if block_type == "text":
                if block.get("text", "").strip():
                    emitted_real += 1
                self.send_sse_event(
                    "content_block_start",
                    {"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}},
                )
                self.send_sse_event(
                    "content_block_delta",
                    {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": block.get("text", "")}},
                )
                self.send_sse_event("content_block_stop", {"type": "content_block_stop", "index": index})
            elif block_type == "tool_use":
                emitted_real += 1
                self.send_sse_event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {"type": "tool_use", "id": block.get("id"), "name": block.get("name"), "input": {}},
                    },
                )
                self.send_sse_event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "input_json_delta", "partial_json": json.dumps(block.get("input") or {}, ensure_ascii=False)},
                    },
                )
                self.send_sse_event("content_block_stop", {"type": "content_block_stop", "index": index})

        # HOLLOW-LANE GUARD (found by the multi-agent audit): if the producer returned
        # no real content (empty/whitespace-only reasonix reply), the stream so far
        # carries zero answer and no error — the workflow lane comes back silently
        # empty. Emit an explicit text block so the lane surfaces the problem instead
        # of looking like a clean empty success. Off via
        # CLAUDE_REASONIX_GATEWAY_HOLLOW_GUARD=0.
        if emitted_real == 0 and os.getenv("CLAUDE_REASONIX_GATEWAY_HOLLOW_GUARD", os.getenv("CLAUDE_CODEX_GATEWAY_HOLLOW_GUARD", "1")).lower() in {"1", "true", "yes", "on"}:
            self.send_sse_event(
                "content_block_start",
                {"type": "content_block_start", "index": next_index, "content_block": {"type": "text", "text": ""}},
            )
            self.send_sse_event(
                "content_block_delta",
                {"type": "content_block_delta", "index": next_index, "delta": {"type": "text_delta",
                 "text": "[reasonix lane returned no content — the task may be too large for one "
                         "lane or the model produced nothing. Split this into smaller lanes and retry.]"}},
            )
            self.send_sse_event("content_block_stop", {"type": "content_block_stop", "index": next_index})

        self.send_sse_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": message.get("stop_reason"), "stop_sequence": None},
                "usage": {"output_tokens": message.get("usage", {}).get("output_tokens", 0)},
            },
        )
        self.send_sse_event("message_stop", {"type": "message_stop"})

    def send_openai_sse_data(self, data: Any) -> None:
        self.wfile.write(b"data: ")
        if isinstance(data, str):
            self.wfile.write(data.encode("utf-8"))
        else:
            self.wfile.write(json_bytes(data))
        self.wfile.write(b"\n\n")
        self.wfile.flush()

    def send_openai_sse_response(self, response: JSON) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()
        self.write_openai_sse_response_body(response)

    def send_openai_sse_response_lazy(self, producer: Any) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()
        # The OpenAI /v1/chat/completions lazy path intentionally keeps the bare
        # ": keepalive" comment (no on_keepalive). The deep-research workflow routes
        # through the Anthropic /v1/messages path, which is where the workflow
        # watchdog heartbeat is required. Revisit if CLAUDE_REASONIX_GATEWAY_BACKEND ever
        # routes workflow subagents through chat/completions.
        try:
            response = self.wait_for_stream_response(producer)
        except Exception as exc:
            self.send_openai_sse_data({"error": {"type": "api_error", "message": str(exc)}})
            self.send_openai_sse_data("[DONE]")
            return
        self.write_openai_sse_response_body(response)

    def write_openai_sse_response_body(self, response: JSON) -> None:
        choice = (response.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        base = {
            "id": response.get("id") or f"chatcmpl_{uuid4().hex}",
            "object": "chat.completion.chunk",
            "created": int(response.get("created") or time.time()),
            "model": response.get("model"),
        }

        first = dict(base)
        first["choices"] = [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]
        self.send_openai_sse_data(first)

        text = message.get("content")
        if isinstance(text, str) and text:
            chunk = dict(base)
            chunk["choices"] = [{"index": 0, "delta": {"content": text}, "finish_reason": None}]
            self.send_openai_sse_data(chunk)

        for call in message.get("tool_calls") or []:
            chunk = dict(base)
            chunk["choices"] = [{"index": 0, "delta": {"tool_calls": [call]}, "finish_reason": None}]
            self.send_openai_sse_data(chunk)

        final = dict(base)
        final["choices"] = [{"index": 0, "delta": {}, "finish_reason": choice.get("finish_reason") or "stop"}]
        self.send_openai_sse_data(final)
        self.send_openai_sse_data("[DONE]")

    def forward_anthropic(self, payload: JSON) -> None:
        upstream_base = env_first("CLAUDE_REASONIX_GATEWAY_ANTHROPIC_BASE_URL", "CLAUDE_CODEX_GATEWAY_ANTHROPIC_BASE_URL", default="https://api.anthropic.com").rstrip("/")
        url = upstream_base + self.path
        headers: dict[str, str] = {"content-type": "application/json"}
        for name in ("anthropic-beta", "anthropic-version", "accept"):
            value = self.headers.get(name)
            if value:
                headers[name] = value

        auth_token = env_first("CLAUDE_REASONIX_GATEWAY_ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODEX_GATEWAY_ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN")
        api_key = env_first("CLAUDE_REASONIX_GATEWAY_ANTHROPIC_API_KEY", "CLAUDE_CODEX_GATEWAY_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY")
        if auth_token:
            headers["authorization"] = f"Bearer {auth_token}"
        elif api_key:
            headers["x-api-key"] = api_key
        else:
            incoming_auth = self.headers.get("authorization")
            incoming_key = self.headers.get("x-api-key")
            if incoming_auth:
                headers["authorization"] = incoming_auth
            if incoming_key:
                headers["x-api-key"] = incoming_key

        req = urllib.request.Request(url, data=json_bytes(payload), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=float(os.getenv("CLAUDE_REASONIX_GATEWAY_TIMEOUT", os.getenv("CLAUDE_CODEX_GATEWAY_TIMEOUT", "600")))) as response:
                body = response.read()
                self.send_response(response.status)
                for key, value in response.headers.items():
                    if key.lower() in {"connection", "transfer-encoding", "content-encoding"}:
                        continue
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)
        except urllib.error.HTTPError as exc:
            body = exc.read()
            self.send_response(exc.code)
            self.send_header("content-type", exc.headers.get("content-type", "application/json"))
            self.end_headers()
            self.wfile.write(body)


def _keepalive_loop() -> None:
    """Background thread: every interval, re-touch each recently-seen shared prefix
    with a tiny request so DeepSeek's LRU keeps it resident between same-codebase
    workflows. Each ping carries ONLY the stored head (the cacheable shared block) +
    a 1-token ask, so it costs ~one cache-hit-priced request and refreshes recency.
    Best-effort: swallows all errors; never affects real lanes."""
    interval = env_float("CLAUDE_REASONIX_GATEWAY_KEEPALIVE_INTERVAL_SECONDS", "CLAUDE_CODEX_GATEWAY_KEEPALIVE_INTERVAL_SECONDS", default=120.0)
    config = model_registry().get("claude-reasonix-flash", {})
    while True:
        try:
            _time.sleep(max(15.0, interval))
            if not _keepalive_enabled():
                continue
            for _key, head in keepalive_targets():
                try:
                    # A minimal ping: the shared head + a 1-word ask. Hits the warm
                    # prefix, refreshes its LRU recency, returns fast.
                    run_reasonix_acp(head + "\nReply with one word.", config)
                    gateway_trace("keepalive_ping", key=_key[:12])
                except Exception:
                    pass
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Local native-model gateway for claude-reasonix")
    parser.add_argument("--host", default=os.getenv("CLAUDE_REASONIX_GATEWAY_HOST", os.getenv("CLAUDE_CODEX_GATEWAY_HOST", "127.0.0.1")))
    parser.add_argument("--port", type=int, default=int(os.getenv("CLAUDE_REASONIX_GATEWAY_PORT", os.getenv("CLAUDE_CODEX_GATEWAY_PORT", "0"))))
    parser.add_argument("--port-file", default="")
    args = parser.parse_args()

    # Lever C (default off): load any persisted read-summary cache on startup, dropping
    # entries whose file changed since they were cached (mtime-freshness on load, Q10).
    load_read_cache()

    if _keepalive_enabled():
        threading.Thread(target=_keepalive_loop, daemon=True).start()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    actual_port = int(server.server_address[1])
    if args.port_file:
        Path(args.port_file).write_text(str(actual_port), encoding="utf-8")
    print(f"claude-reasonix native gateway listening on http://{args.host}:{actual_port}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
