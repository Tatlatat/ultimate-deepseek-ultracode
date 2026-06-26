#!/usr/bin/env python3
"""Engine-seam byte-identical guard. On first run with REASONIX_FREEZE_GOLDEN=1 it
records the golden output of the cache-critical prompt-building functions; every later
run asserts the CURRENT code produces byte-identical output. This is the net that proves
the refactor did not change a single byte of the assembled prompt (which would collapse
the 96-99% prefix cache)."""
import importlib.util, json, os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = Path(__file__).resolve().parent / "fixtures" / "engine_seam_golden.json"
_spec = importlib.util.spec_from_file_location("gw", ROOT / "reasonix-native-gateway.py")
gw = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(gw)

# A FIXED, representative input — a multi-message lane with tools + a system message.
MSGS = [
    {"role": "system", "content": "You are a careful refactoring agent."},
    {"role": "user", "content": "Extract parseX into a module.\nACCEPTANCE_TEST: bun test x\nFiles: src/a.ts, src/b.ts"},
    {"role": "assistant", "content": "Understood; reading the files."},
    {"role": "user", "content": "Now wire it and run the test."},
]
TOOLS = [{"type": "function", "function": {"name": "read_file",
          "description": "Read a file", "parameters": {"type": "object",
          "properties": {"path": {"type": "string"}}, "required": ["path"]}}}]
SAMPLE_PREFIX = "x-anthropic-billing-header: abc\nYou are a careful agent.\nTool specs here."

def current():
    return {
        "openai_messages_to_prompt": gw.openai_messages_to_prompt(MSGS, TOOLS),
        "lane_task_text": gw.lane_task_text(MSGS),
        "normalize_prefix": gw.normalize_prefix(SAMPLE_PREFIX),
    }

def main():
    cur = current()
    if os.getenv("REASONIX_FREEZE_GOLDEN") == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
        print("froze golden:", GOLDEN)
        return 0
    if not GOLDEN.exists():
        print("FAIL no golden — run once with REASONIX_FREEZE_GOLDEN=1 first")
        return 1
    want = json.loads(GOLDEN.read_text(encoding="utf-8"))
    p = f = 0
    for k, v in cur.items():
        if v == want.get(k):
            p += 1; print(f"  ok   {k} byte-identical")
        else:
            f += 1; print(f"  FAIL {k} DIFFERS — cache-critical change!")
    print(f"\n{p} passed, {f} failed")
    return 1 if f else 0

if __name__ == "__main__":
    sys.exit(main())
