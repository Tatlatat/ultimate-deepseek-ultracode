from __future__ import annotations
import importlib.util, os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gw", ROOT / "reasonix-native-gateway.py")
gw = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(gw)


def expect(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")


def _build_env(monkeyenv: dict) -> dict:
    """Reproduce the gateway's reasonix_env construction for the ephemeral default,
    so the test pins the exact decision without spawning reasonix."""
    saved = dict(os.environ)
    try:
        for k in ("CLAUDE_REASONIX_GATEWAY_REASONIX_EPHEMERAL", "REASONIX_ACP_EPHEMERAL_SESSION"):
            os.environ.pop(k, None)
        os.environ.update(monkeyenv)
        reasonix_env = dict(os.environ)
        if gw.env_first("CLAUDE_REASONIX_GATEWAY_REASONIX_EPHEMERAL", "CLAUDE_CODEX_GATEWAY_REASONIX_EPHEMERAL", default="1") not in {"0", "false", "no", "off"}:
            reasonix_env.setdefault("REASONIX_ACP_EPHEMERAL_SESSION", "1")
        return reasonix_env
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_default_on():
    env = _build_env({})
    expect(env.get("REASONIX_ACP_EPHEMERAL_SESSION") == "1",
           "ephemeral defaults ON so fan-out lanes don't inherit same-minute sessions")


def test_kill_switch_disables():
    env = _build_env({"CLAUDE_REASONIX_GATEWAY_REASONIX_EPHEMERAL": "0"})
    expect("REASONIX_ACP_EPHEMERAL_SESSION" not in env,
           "kill-switch leaves the reasonix var unset -> stock session behavior")


def test_explicit_user_value_preserved():
    env = _build_env({"REASONIX_ACP_EPHEMERAL_SESSION": "0"})
    expect(env.get("REASONIX_ACP_EPHEMERAL_SESSION") == "0",
           "an explicit user value is not overridden by the default")


if __name__ == "__main__":
    test_default_on()
    test_kill_switch_disables()
    test_explicit_user_value_preserved()
    print("PASS: reasonix ephemeral session")
