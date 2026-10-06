"""Local argument validation helpers for Hypervisor operations."""

from __future__ import annotations

import re
from collections.abc import Mapping

_LABEL_KEY_RE = re.compile(r"\A[A-Za-z0-9._/-]{1,63}\Z")


def _memory_mb(memory: int | None) -> int | None:
    if memory is None:
        return None
    if isinstance(memory, bool) or not isinstance(memory, int) or memory <= 0:
        raise ValueError("memory must be a positive GiB count")
    return memory * 1024


def _validate_labels(labels: Mapping[str, str] | None) -> dict[str, str] | None:
    if labels is None:
        return None
    if not isinstance(labels, Mapping):
        raise TypeError("labels must be a mapping of string keys to string values")
    normalized: dict[str, str] = {}
    for key, value in labels.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise TypeError("labels must be a mapping of string keys to string values")
        if not _LABEL_KEY_RE.fullmatch(key):
            raise ValueError(
                f"VM label key {key!r} must be 1..=63 ASCII characters in [A-Za-z0-9._/-]"
            )
        if len(value.encode("utf-8")) > 255:
            raise ValueError(f"VM label value for {key!r} too long (max 255 bytes)")
        if any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in value):
            raise ValueError(f"VM label value for {key!r} must not contain control characters")
        normalized[key] = value
    if len(normalized) > 64:
        raise ValueError("too many VM labels (max 64)")
    return normalized

