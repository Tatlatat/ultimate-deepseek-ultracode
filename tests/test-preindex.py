#!/usr/bin/env python3
"""Lever D — PRE-INDEX unit test (mocks only; NO real embedding model required).

Lever D builds a semantic index ONCE per codebase (gateway is the SOLE build
trigger) so read-exploration lanes QUERY it via the EXISTING `semantic_search`
tool instead of reading raw files. NO prefix injection — sidesteps byte-stability.
Default OFF; FAIL-OPEN when no embedding provider/model is reachable (the current
state: Ollama runs with 0 models).

Covers:
  (a) build_preindex with PREINDEX off (default) is a no-op (returns False, no
      build, never raises).
  (b) build_preindex with PREINDEX on but NO reachable embedding provider/model
      FAILS OPEN — returns False, raises NO exception, leaves lanes runnable.
      (Simulated with a fake `node` that exits non-zero, like a probe failure.)
  (c) build_preindex is idempotent per-cwd: a second call for the same root is a
      no-op (the gateway is the sole trigger — no double-build).
  (d) With PREINDEX off, the per-lane toolSpecs (buildCodeToolset) are
      BYTE-IDENTICAL to the PREINDEX-off baseline — no `semantic_search` leaked
      into the spec, so the immutable prefix is undisturbed.
  (e) The per-lane path (buildCodeToolset) is READ-ONLY: it does NOT create a
      `.reasonix/semantic` index dir (no build, no JSONL append race).

(d) and (e) drive the REAL vendored engine over a temp repo with NO index — that
needs NO embedding model (no index exists, so `indexCompatible()` returns false
and nothing builds). (a)-(c) drive the Python gateway with a fake node.
"""
from __future__ import annotations
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "reasonix_native_gateway", ROOT / "reasonix-native-gateway.py")
gw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gw)

VENDORED_DIST = ROOT / "vendor" / "reasonix-engine" / "dist" / "index.js"

_PASS = 0
_FAIL = 0


def check(cond: bool, msg: str) -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ok   {msg}")
    else:
        _FAIL += 1
        print(f"  FAIL {msg}")


_PREINDEX_ENVS = (
    "CLAUDE_REASONIX_PREINDEX",
    "CLAUDE_REASONIX_PREINDEX_TIMEOUT",
    "CLAUDE_REASONIX_NODE_BIN",
    "NODE_BIN",
    "REASONIX_ENGINE_DIST",
    "REASONIX_EMBED_PROVIDER",
    "REASONIX_EMBED_MODEL",
    "REASONIX_EMBED_BASE_URL",
    "REASONIX_EMBED_API_KEY",
)


def _clear_env() -> None:
    for k in _PREINDEX_ENVS:
        os.environ.pop(k, None)
    gw._PREINDEX_DONE.clear()


def _write_fake_node(dirpath: Path, exit_code: int) -> str:
    """A fake `node` that ignores its args/stdin and exits with `exit_code`,
    writing a probe-style error on stderr — simulating 'no embedding model'."""
    p = dirpath / "node"
    p.write_text(
        "#!/bin/sh\n"
        'echo "preindex build failed: no reachable embedding model" 1>&2\n'
        f"exit {exit_code}\n"
    )
    p.chmod(0o755)
    return str(p)


# --- (d)/(e) helper: drive the REAL vendored engine over a tmp repo ------------
_TOOLSPEC_DRIVER = r"""
import { createRequire } from "node:module";
if (typeof globalThis.require !== "function") {
  globalThis.require = createRequire(import.meta.url);
}
const { buildCodeToolset } = await import(process.env.REASONIX_ENGINE_DIST);
const rootDir = process.env.REASONIX_TEST_ROOT;
const toolset = await buildCodeToolset({ rootDir });
const names = toolset.tools.specs().map((s) => s.function?.name ?? s.name).sort();
process.stdout.write(JSON.stringify({
  names,
  semanticEnabled: toolset.semantic?.enabled ?? false,
}) + "\n");
"""


def _toolspec_names(root: str, preindex_on: bool) -> dict:
    env = dict(os.environ)
    env["REASONIX_ENGINE_DIST"] = str(VENDORED_DIST)
    env["REASONIX_TEST_ROOT"] = root
    # PREINDEX is a GATEWAY flag, never read by buildCodeToolset, but set it to
    # prove the per-lane toolspec is unaffected by it.
    if preindex_on:
        env["CLAUDE_REASONIX_PREINDEX"] = "1"
    else:
        env.pop("CLAUDE_REASONIX_PREINDEX", None)
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", _TOOLSPEC_DRIVER],
        capture_output=True, text=True, env=env, timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"toolspec driver failed: {proc.stderr.strip()[:400]}")
    last = [l for l in proc.stdout.splitlines() if l.strip()][-1]
    return json.loads(last)


def main() -> int:
    _clear_env()

    # --- (a) PREINDEX off (default) => no-op, returns False, never raises ------
    with tempfile.TemporaryDirectory() as d:
        try:
            res = gw.build_preindex(d)
            raised = False
        except Exception as exc:  # noqa: BLE001
            raised = True
            res = exc
        check(raised is False, "(a) build_preindex OFF does not raise")
        check(res is False, "(a) build_preindex OFF returns False (no-op)")
        check(not os.path.exists(os.path.join(d, ".reasonix", "semantic")),
              "(a) build_preindex OFF creates no index dir")
    _clear_env()

    # --- (b) PREINDEX on, no reachable embedding model => FAIL OPEN ------------
    with tempfile.TemporaryDirectory() as d:
        bindir = Path(d) / "bin"
        bindir.mkdir()
        fake_node = _write_fake_node(bindir, exit_code=3)  # probe/build failure
        os.environ["CLAUDE_REASONIX_PREINDEX"] = "1"
        os.environ["CLAUDE_REASONIX_NODE_BIN"] = fake_node
        os.environ["REASONIX_ENGINE_DIST"] = str(VENDORED_DIST)
        os.environ["CLAUDE_REASONIX_PREINDEX_TIMEOUT"] = "30"
        try:
            res = gw.build_preindex(d)
            raised = False
        except Exception as exc:  # noqa: BLE001
            raised = True
            res = exc
        check(raised is False,
              "(b) build_preindex ON w/ unreachable embed model does NOT raise (fail-open)")
        check(res is False,
              "(b) build_preindex returns False when no embed model is reachable")
        check(not os.path.exists(os.path.join(d, ".reasonix", "semantic")),
              "(b) failed build leaves no partial index dir")
    _clear_env()

    # --- (c) idempotent per-cwd: gateway is the SOLE trigger ------------------
    with tempfile.TemporaryDirectory() as d:
        bindir = Path(d) / "bin"
        bindir.mkdir()
        fake_node = _write_fake_node(bindir, exit_code=0)  # would "succeed"
        os.environ["CLAUDE_REASONIX_PREINDEX"] = "1"
        os.environ["CLAUDE_REASONIX_NODE_BIN"] = fake_node
        os.environ["REASONIX_ENGINE_DIST"] = str(VENDORED_DIST)
        first = gw.build_preindex(d)
        second = gw.build_preindex(d)
        check(first is True,
              "(c) first build_preindex for a fresh cwd attempts a build (True)")
        check(second is False,
              "(c) second build_preindex for the SAME cwd is a no-op (no double-build race)")
    _clear_env()

    # --- (d) PREINDEX off => per-lane toolSpecs byte-identical, no leak --------
    if not VENDORED_DIST.exists():
        check(False, f"(d/e) vendored engine dist missing at {VENDORED_DIST}")
    else:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "sample.py").write_text("def f():\n    return 1\n")
            off = _toolspec_names(d, preindex_on=False)
            on = _toolspec_names(d, preindex_on=True)
            check(json.dumps(off["names"]) == json.dumps(on["names"]),
                  "(d) per-lane toolSpecs are byte-identical with PREINDEX off vs on (no index)")
            check("semantic_search" not in off["names"],
                  "(d) semantic_search NOT leaked into toolSpecs when no index exists")
            check(off["semanticEnabled"] is False,
                  "(d) toolset.semantic.enabled is False when no index exists")

            # --- (e) per-lane path is READ-ONLY (no build, no JSONL append) ----
            idx_dir = os.path.join(d, ".reasonix", "semantic")
            check(not os.path.exists(idx_dir),
                  "(e) buildCodeToolset (per-lane path) created NO index dir (read-only)")
    _clear_env()

    # --- Summary --------------------------------------------------------------
    total = _PASS + _FAIL
    print(f"\n{'PASS' if _FAIL == 0 else 'FAIL'}  {_PASS}/{total} checks passed")
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
