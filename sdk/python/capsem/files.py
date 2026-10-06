"""Allowlist path sanitization matching `capsem-service::fs_utils::sanitize_file_path`."""

from __future__ import annotations

from ._transport import CapsemError


class InvalidPathError(CapsemError, ValueError):
    """Raised when a workspace file path is empty after sanitization or contains `..`."""


def sanitize_file_path(raw: str) -> str:
    """Sanitize a workspace-relative path using the service's inner allowlist rules.

    Strips any character outside ASCII `[a-zA-Z0-9._\\-/]`, collapses consecutive
    slashes, strips leading `/`, and rejects empty results or `..` segments
    with `InvalidPathError` (`crates/capsem-service/src/fs_utils.rs::sanitize_file_path`).
    Note that this function applies the inner allowlist only; when `exact=False`,
    the service's `resolve_file_path` first strips a leading `/root` (or
    container `/workspace`) prefix via `workspace_relative`.
    """
    if not isinstance(raw, str):
        raise TypeError("path must be a string")
    cleaned = "".join(ch for ch in raw if (ch.isascii() and ch.isalnum()) or ch in "._-/")
    collapsed_chars: list[str] = []
    prev_slash = False
    for ch in cleaned:
        if ch == "/":
            if not prev_slash:
                collapsed_chars.append(ch)
            prev_slash = True
        else:
            collapsed_chars.append(ch)
            prev_slash = False
    trimmed = "".join(collapsed_chars).lstrip("/")
    if not trimmed:
        raise InvalidPathError("empty path after sanitization")
    if ".." in trimmed:
        raise InvalidPathError("path traversal rejected")
    return trimmed
