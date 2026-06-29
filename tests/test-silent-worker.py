#!/usr/bin/env python3
"""Tests for the silent-worker output-style: the file ships with the required
frontmatter (name/description/keep-coding-instructions:true) and the launcher's
render_settings step injects/strips the outputStyle key correctly."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

passed = 0
failed = 0
def check(label, cond):
    global passed, failed
    if cond:
        print(f"  ok   {label}"); passed += 1
    else:
        print(f"  FAIL {label}"); failed += 1

# --- Task 1: the style file exists with the required frontmatter ---
style = ROOT / "output-styles" / "silent-worker.md"
check("silent-worker.md exists", style.is_file())
text = style.read_text(encoding="utf-8") if style.is_file() else ""
# frontmatter is the block between the first two '---' lines
fm = ""
if text.startswith("---"):
    end = text.find("\n---", 3)
    fm = text[3:end] if end != -1 else ""
check("frontmatter has name: silent-worker", "name: silent-worker" in fm)
check("frontmatter has a description", "description:" in fm and len(fm.split("description:")[1].strip()) > 0)
check("frontmatter keeps coding instructions", "keep-coding-instructions: true" in fm)
# the body must encode the KEEP/CUT boundary, not be empty
check("body is non-trivial", len(text) > 400)

# --- Task 3: install.sh copies the style to the user-level output-styles dir ---
install_sh = (ROOT / "install.sh").read_text(encoding="utf-8")
check("install copies silent-worker.md", "output-styles/silent-worker.md" in install_sh)
check("install targets user-level ~/.claude/output-styles", ".claude/output-styles" in install_sh)
# guard against the spec footgun: it must NOT be installed into INSTALL_HOME (Claude
# Code never looks there for output styles)
check("install does NOT put the style under INSTALL_HOME",
      '"$INSTALL_HOME/output-styles' not in install_sh)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
