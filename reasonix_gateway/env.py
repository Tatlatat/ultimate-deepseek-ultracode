import os
from typing import Any

JSON = dict[str, Any]


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
