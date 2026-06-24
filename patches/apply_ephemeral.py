#!/usr/bin/env python3
"""Re-apply the reasonix ACP ephemeral-session patch (idempotent).

WHY THIS EXISTS
---------------
claude-reasonix fans out many concurrent reasonix `acp` lanes. Stock reasonix names
each acp session by a minute-granular timestamp (`acp-<minute>`), so lanes that start
in the same minute share a session name and load each other's history — inflating
input tokens and wrecking the prompt cache (measured: +~10.8K in_tok/lane, fan-out
cache stuck at 60-94%). Setting REASONIX_ACP_EPHEMERAL_SESSION=1 makes each lane use
session=null (no shared history); the launcher exports that env var.

But the toggle only works if reasonix's compiled `acp-*.js` actually reads the env
var. The reasonix dist ships WITHOUT that branch, and a `reasonix` upgrade overwrites
any edit — so this patch must be (re-)applied after install and after every upgrade.
The launcher's install/doctor flow runs this; it is safe to run any number of times.

WHAT IT DOES
------------
Finds the reasonix module (resolving $REASONIX_BIN / PATH / fnm), then in every
dist/cli/acp-*.js replaces the stock session name expression

    session: `acp-${timestampSuffix()}`

with the env-gated form

    session: (process.env.REASONIX_ACP_EPHEMERAL_SESSION === "1" ? null
              : `acp-${timestampSuffix()}`)

Idempotent: a file already carrying REASONIX_ACP_EPHEMERAL_SESSION is left untouched.

Exit codes: 0 = patched or already-patched (success); 1 = reasonix/module not found;
2 = found the module but no acp file matched the expected stock pattern (reasonix
internals changed — the patch needs updating, surfaced loudly rather than silently).
"""
from __future__ import annotations
import glob
import os
import re
import shutil
import subprocess
import sys

STOCK = "session: `acp-${timestampSuffix()}`"
PATCHED = (
    "session: (process.env.REASONIX_ACP_EPHEMERAL_SESSION === \"1\" ? null "
    ": `acp-${timestampSuffix()}`)"
)
SENTINEL = "REASONIX_ACP_EPHEMERAL_SESSION"


def find_reasonix_bin() -> str | None:
    env = os.getenv("REASONIX_BIN")
    if env and os.path.exists(env):
        return env
    onpath = shutil.which("reasonix")
    if onpath:
        return onpath
    home = os.path.expanduser("~")
    for pat in (
        f"{home}/.local/share/fnm/node-versions/*/installation/bin/reasonix",
        f"{home}/.local/state/fnm_multishells/*/bin/reasonix",
    ):
        hits = sorted(glob.glob(pat), reverse=True)
        if hits:
            return hits[0]
    return None


def module_root(reasonix_bin: str) -> str | None:
    """Resolve the bin (a symlink to dist/cli/index.js) back to the module root."""
    real = os.path.realpath(reasonix_bin)
    # real is .../node_modules/reasonix/dist/cli/index.js — walk up to .../reasonix
    parts = real.split(os.sep)
    if "reasonix" in parts:
        idx = len(parts) - 1 - parts[::-1].index("reasonix")
        root = os.sep.join(parts[: idx + 1])
        if os.path.isdir(os.path.join(root, "dist")):
            return root
    # Fallback: ask npm where global modules live.
    try:
        groot = subprocess.run(
            ["npm", "root", "-g"], capture_output=True, text=True, timeout=15
        ).stdout.strip()
        cand = os.path.join(groot, "reasonix")
        if os.path.isdir(os.path.join(cand, "dist")):
            return cand
    except Exception:
        pass
    return None


def patch_file(path: str) -> str:
    """Return 'already' | 'patched' | 'nomatch' for one acp file."""
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    if SENTINEL in text:
        return "already"
    if STOCK not in text:
        return "nomatch"
    _write_atomic(path, text.replace(STOCK, PATCHED))
    return "patched"


def revert_file(path: str) -> str:
    """Undo the patch: 'reverted' | 'notpatched' for one acp file."""
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    if PATCHED not in text:
        return "notpatched"
    _write_atomic(path, text.replace(PATCHED, STOCK))
    return "reverted"


def _write_atomic(path: str, text: str) -> None:
    tmp = path + ".tmp-ephemeral"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def main() -> int:
    rx = find_reasonix_bin()
    if not rx:
        print("apply_ephemeral: reasonix CLI not found "
              "(set REASONIX_BIN or put reasonix on PATH)", file=sys.stderr)
        return 1
    root = module_root(rx)
    if not root:
        print(f"apply_ephemeral: could not locate the reasonix module from {rx}",
              file=sys.stderr)
        return 1
    acp_files = glob.glob(os.path.join(root, "dist", "cli", "acp-*.js"))
    if not acp_files:
        print(f"apply_ephemeral: no dist/cli/acp-*.js under {root}", file=sys.stderr)
        return 2

    if "--revert" in sys.argv[1:]:
        for f in acp_files:
            r = revert_file(f)
            print(f"apply_ephemeral: {r} {f}")
        return 0

    results = {f: patch_file(f) for f in acp_files}
    patched = [f for f, r in results.items() if r == "patched"]
    already = [f for f, r in results.items() if r == "already"]
    nomatch = [f for f, r in results.items() if r == "nomatch"]
    for f in patched:
        print(f"apply_ephemeral: patched {f}")
    for f in already:
        print(f"apply_ephemeral: already patched {f}")
    # 'nomatch' is only a hard failure if NOTHING in the module is/was patched —
    # i.e. we found acp files but none carry the session name we know how to gate.
    if nomatch and not patched and not already:
        for f in nomatch:
            print(f"apply_ephemeral: ERROR no stock session pattern in {f} "
                  "(reasonix internals changed — update this patch)", file=sys.stderr)
        return 2
    print(f"apply_ephemeral: OK ({len(patched)} patched, {len(already)} already, "
          f"module={root})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
