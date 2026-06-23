# tests/test-prefix-stability-check.py
from __future__ import annotations
import importlib.util, hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gw", ROOT / "codex-native-gateway.py")
gw = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(gw)


def expect(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")


def pfx32(s):
    return hashlib.sha1(s[:32768].encode("utf-8", "ignore")).hexdigest()[:12]


def test_same_shared_system_one_family():
    # Same system text + same tools, only the trailing user task differs ->
    # the leading 32KB (system + tools block) must be byte-identical.
    # The shared system block is sized to fill the 32KB prefix window so the
    # per-lane user task (which the gateway correctly keeps AFTER the shared
    # blocks, before the LAST structured-output instruction) lands beyond the
    # window and does not bust the prefix family.
    sys_text = "SYS COMMON " * 4000
    tools = [{"name": "StructuredOutput", "input_schema": {"type": "object", "properties": {"x": {"type": "string"}}}}]
    a = gw.openai_messages_to_prompt([{"role": "system", "content": sys_text}, {"role": "user", "content": "lane A"}], tools)
    b = gw.openai_messages_to_prompt([{"role": "system", "content": sys_text}, {"role": "user", "content": "lane B different"}], tools)
    expect(pfx32(a) == pfx32(b), "shared system+tools -> identical 32KB prefix family")
    # Negative control: a difference INSIDE the first 32KB must produce a
    # different prefix family (the check is real, not vacuous).
    c = gw.openai_messages_to_prompt([{"role": "system", "content": "DIFF " + sys_text}, {"role": "user", "content": "lane A"}], tools)
    expect(pfx32(a) != pfx32(c), "difference inside the 32KB head -> different family")


if __name__ == "__main__":
    test_same_shared_system_one_family()
    print("PASS: prefix stability check")
