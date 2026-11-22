"""Runtime data types shared by sources, the engine and publishers."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .config import TagConfig


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class Reading:
    """One tag value as read from a device."""

    value: Any
    timestamp: int
    good: bool = True


def effective_datatype(tag: TagConfig) -> str:
    """Datatype of the published value after scaling."""
    if tag.scale is not None or tag.offset is not None:
        return "float64"
    return tag.datatype


def apply_scaling(tag: TagConfig, value: Any) -> Any:
    if value is None or isinstance(value, (bool, str)):
        return value
    if tag.scale is None and tag.offset is None:
        return value
    scaled = float(value) * (tag.scale if tag.scale is not None else 1.0) + (tag.offset or 0.0)
    return round(scaled, 9)


def remove_scaling(tag: TagConfig, value: Any) -> Any:
    if tag.scale is None and tag.offset is None:
        return value
    raw = (float(value) - (tag.offset or 0.0)) / (tag.scale if tag.scale is not None else 1.0)
    if tag.datatype.startswith(("int", "uint")):
        return round(raw)
    return raw


def coerce(tag: TagConfig, value: Any) -> Any:
    """Convert an incoming write value (from JSON or Sparkplug) to the tag's type."""
    dt = effective_datatype(tag)
    if dt == "bool":
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("1", "true", "on", "yes"):
                return True
            if lowered in ("0", "false", "off", "no"):
                return False
            raise ValueError(f"cannot convert {value!r} to bool")
        return bool(value)
    if dt.startswith(("int", "uint")):
        if isinstance(value, float) and not value.is_integer():
            raise ValueError(f"{value!r} is not an integer")
        return int(value)
    if dt.startswith("float"):
        return float(value)
    if dt == "string":
        return str(value)
    return value


def changed(tag: TagConfig, old: Reading | None, new: Reading) -> bool:
    """Report-by-exception check with an optional absolute deadband."""
    if old is None or old.good != new.good:
        return True
    if old.value is None or new.value is None:
        return old.value is not new.value
    if (
        tag.deadband
        and isinstance(new.value, (int, float))
        and not isinstance(new.value, bool)
        and isinstance(old.value, (int, float))
    ):
        return abs(float(new.value) - float(old.value)) > tag.deadband
    return bool(old.value != new.value)
