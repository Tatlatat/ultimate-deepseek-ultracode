#!/usr/bin/env python3
"""Lever E (speculative context prefetch) — ADVISORY MODE tests.

Q7 decision: ship advisory mode FIRST. Advisory = predict which files each
fan-out lane will read + LOG precision, with ZERO prompt/prefix change (no
injection). These tests assert:

  1. predict_prefetch_files(task_text, cwd) returns a BOUNDED (<=MAX_FILES) list
     of files that ACTUALLY EXIST under cwd for a task that names real files.
  2. It returns [] for a task that names no real file.
  3. The list is bounded to _MAX_FILES even when the task names more.
  4. ADVISORY MODE emits NO prompt change: the assembled hook output (updatedInput
     script + additionalContext) is BYTE-IDENTICAL with prefetch off vs advisory.
     This is the hard zero-cache-risk guarantee — advisory only measures.
  5. 'inject' mode is a documented stub that does nothing to the prompt yet.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "hooks" / "reasonix-workflow.py"

# hooks/reasonix-workflow.py is hyphenated → load by path.
_spec = importlib.util.spec_from_file_location("reasonix_workflow_hook", HOOK)
rw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rw)


def expect(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")


# --- predict_prefetch_files ----------------------------------------------------
def test_predicts_real_files_bounded():
    # A task naming several real repo files (relative paths + bare filenames).
    task = (
        "Review reasonix-native-gateway.py and hooks/reasonix-workflow.py, then "
        "cross-check runtime/realworld-bench.py and README.md for consistency. "
        "Also look at system-prompt-reasonix.md."
    )
    files = rw.predict_prefetch_files(task, str(ROOT))
    expect(isinstance(files, list), "predict_prefetch_files must return a list")
    expect(len(files) <= rw._PREFETCH_MAX_FILES,
           f"must be bounded to {rw._PREFETCH_MAX_FILES}, got {len(files)}")
    expect(len(files) > 0, "must find at least one real file in a file-naming task")
    for f in files:
        p = Path(f)
        expect(p.is_absolute(), f"predicted path must be absolute: {f}")
        expect(p.is_file(), f"predicted path must EXIST under cwd: {f}")
    # The named real files must be among the predictions.
    names = {Path(f).name for f in files}
    expect("reasonix-native-gateway.py" in names, "must predict the named gateway file")
    expect("realworld-bench.py" in names, "must predict the named bench file")


def test_empty_for_no_files():
    task = "Summarize the overall token-reduction strategy in two sentences. No files."
    files = rw.predict_prefetch_files(task, str(ROOT))
    expect(files == [], f"a task naming no real file must predict []; got {files}")


def test_nonexistent_filenames_excluded():
    # Names that look like files but do not exist under cwd → excluded.
    task = "Open totally-made-up-file.py and another_fake_thing.md and fix them."
    files = rw.predict_prefetch_files(task, str(ROOT))
    expect(files == [], f"non-existent filenames must not be predicted; got {files}")


def test_bound_respected_with_many_files():
    # Name MORE than _MAX_FILES real files; the result must still be capped.
    real = [
        "reasonix-native-gateway.py",
        "hooks/reasonix-workflow.py",
        "hooks/only-reasonix-fleet.py",
        "hooks/workflow_selfheal.py",
        "runtime/realworld-bench.py",
        "runtime/lever-matrix-bench.py",
        "runtime/realworld-bench.py",
        "system-prompt-reasonix.md",
        "README.md",
        "tests/test-prefetch-precision.py",
        "tests/test-workflow-prefix-guide.py",
    ]
    task = "Audit all of: " + " ".join(real)
    files = rw.predict_prefetch_files(task, str(ROOT))
    expect(len(files) <= rw._PREFETCH_MAX_FILES,
           f"must cap at {rw._PREFETCH_MAX_FILES}; got {len(files)}")
    expect(len(set(files)) == len(files), "predictions must be de-duplicated")


# --- advisory mode: ZERO prompt change ----------------------------------------
SCRIPT = (
    "export const meta = { name: 'x', description: 'y' }\n"
    "// task references reasonix-native-gateway.py and README.md\n"
    "const a = await agent('audit reasonix-native-gateway.py', "
    "{label:'arch', agentType:'reasonix-worker'})\n"
)
PAYLOAD = {
    "tool_name": "Workflow",
    "tool_input": {"script": SCRIPT},
    "cwd": str(ROOT),
}


def _run_hook(prefetch_mode):
    env = dict(os.environ)
    env["CLAUDE_REASONIX_WORKFLOW_MODE"] = "native"
    # Pin the prefix-guide deterministically so it is not the variable under test.
    env["CLAUDE_REASONIX_WORKFLOW_PREFIX_GUIDE"] = "1"
    if prefetch_mode is None:
        env.pop("CLAUDE_REASONIX_PREFETCH_CONTEXT", None)
    else:
        env["CLAUDE_REASONIX_PREFETCH_CONTEXT"] = prefetch_mode
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(PAYLOAD),
        capture_output=True,
        text=True,
        env=env,
    )
    line = proc.stdout.strip().splitlines()[-1]
    return json.loads(line)


def _output_signature(out):
    hso = out["hookSpecificOutput"]
    return (
        hso["updatedInput"]["script"],
        hso["additionalContext"],
    )


def test_advisory_changes_no_prompt_byte():
    off = _output_signature(_run_hook("off"))
    adv = _output_signature(_run_hook("advisory"))
    expect(off == adv,
           "advisory mode MUST emit a byte-identical script + additionalContext as off")


def test_default_off_changes_no_prompt_byte():
    off = _output_signature(_run_hook("off"))
    default = _output_signature(_run_hook(None))
    expect(off == default, "default (env unset) must equal off — no prompt change")


def test_inject_is_stub_no_prompt_byte_change():
    # inject mode is NOT implemented this task; it must NOT alter the prompt either
    # (documented stub). It may log, but the assembled output bytes stay identical.
    off = _output_signature(_run_hook("off"))
    inj = _output_signature(_run_hook("inject"))
    expect(off == inj, "inject stub must not change the prompt this task")


if __name__ == "__main__":
    test_predicts_real_files_bounded()
    test_empty_for_no_files()
    test_nonexistent_filenames_excluded()
    test_bound_respected_with_many_files()
    test_advisory_changes_no_prompt_byte()
    test_default_off_changes_no_prompt_byte()
    test_inject_is_stub_no_prompt_byte_change()
    print("PASS: lever E prefetch advisory (predict + zero-prompt-change)")
