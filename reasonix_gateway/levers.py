"""Lever layer — all gateway feature-flag functions and their module-level state.

Extracted from reasonix-native-gateway.py (PURE MOVE — no logic changes).
Imports only from lower layers (env, text). engine_seam (Task 6) imports FROM here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import time as _time
from pathlib import Path
from typing import Any

from .env import JSON, env_first, env_int, env_float, env_truthy
from .text import text_from_content, lane_task_text


_REASONIX_CLI_SEMAPHORE_LOCK = threading.Lock()
_REASONIX_CLI_SEMAPHORE: tuple[int, threading.BoundedSemaphore] | None = None


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


# normalize_prefix lives here (levers layer) because read_cache_injection_block
# (a lever function) calls it. engine_seam (Task 6) will import it from levers.
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


# ---------------------------------------------------------------------------
# Tool-schema helpers — used by is_heavy_synthesis and read_lane_summary_instruction
# (both lever functions). Moved here so levers.py is self-contained.
# engine_seam (Task 6) will import these from levers.
# ---------------------------------------------------------------------------

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


def is_structured_output_tool_name(name: str) -> bool:
    normalized = "".join(ch for ch in name.lower() if ch.isalnum())
    return normalized == "structuredoutput" or normalized.endswith("structuredoutput")


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


# A bulk-scope phrase that is NEGATED is a SCOPE-NARROWING instruction, not an
# over-broad lane: "read ONLY these files, do NOT read the whole repo" tells the lane
# to stay narrow. Without this, the bulk regex matched "the whole repo" inside the
# negation and rejected a perfectly narrow lane (measured on the deno_lint run).
_NEGATION_RE = re.compile(r"\b(do not|don'?t|never|avoid|without|rather than|not)\b",
                          re.I)


def _bulk_scope_match(pt: str) -> bool:
    """True only for a GENUINE over-broad scope phrase. A bulk phrase preceded (within
    a short window) by a negation is treated as scope-narrowing and does NOT count."""
    for m in _OVERSCOPE_BULK_RE.finditer(pt):
        head = pt[max(0, m.start() - 40):m.start()]
        if _NEGATION_RE.search(head):
            continue  # negated bulk phrase = "do not <bulk>" = narrowing, not over-broad
        return True
    return False


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
    bulk = _bulk_scope_match(pt)
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
