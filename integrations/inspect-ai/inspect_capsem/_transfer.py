"""Staged multi-part file upload and download helpers for Capsem VMs."""

from __future__ import annotations

import contextlib
import posixpath
import shlex
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from capsem import HttpError, InvalidPathError, sanitize_file_path
from capsem.execution import MAX_REQUEST_BODY_BYTES

if TYPE_CHECKING:
    from inspect_capsem._controller import CommandResult

__all__ = [
    "_XFER_PART_BYTES",
    "_XFER_STAGE_DIR",
    "_exec_checked",
    "_rel_to_stage_dir",
    "_staged_download",
    "_staged_upload",
]

_XFER_PART_BYTES = MAX_REQUEST_BODY_BYTES - 2 * 1024 * 1024
_XFER_STAGE_DIR = "/root"


async def _exec_checked(
    controller: Any, vm_id: str, command: str, *, timeout: int, what: str
) -> CommandResult:
    res = await controller.exec_in_vm(vm_id, command, timeout=timeout)
    if res.exit_code != 0:
        raise RuntimeError(
            f"{what} failed in VM {vm_id} (exit {res.exit_code}): {res.stderr or res.stdout}"
        )
    return res


def _rel_to_stage_dir(guest_path: str, stage_dir: str) -> str | None:
    norm_stage = posixpath.normpath(stage_dir)
    norm_guest = posixpath.normpath(guest_path)
    prefix = "/" if norm_stage == "/" else f"{norm_stage}/"
    if not norm_guest.startswith(prefix):
        return None
    rel = norm_guest[len(prefix) :]
    if not rel:
        return None
    try:
        sanitized = sanitize_file_path(rel)
    except (InvalidPathError, ValueError):
        return None
    return sanitized if sanitized == rel.strip("/") else None


async def _staged_upload(
    controller: Any,
    vm_id: str,
    guest_path: str,
    data: bytes,
    write_part: Callable[[str, bytes], Awaitable[None]],
) -> None:
    dest_q = shlex.quote(guest_path)
    parent_q = shlex.quote(posixpath.dirname(guest_path) or "/")
    if not data:
        await _exec_checked(
            controller,
            vm_id,
            f"mkdir -p {parent_q} && : > {dest_q}",
            timeout=60,
            what="Empty upload",
        )
        return
    stage_dir = _XFER_STAGE_DIR
    part_bytes = _XFER_PART_BYTES
    rel = _rel_to_stage_dir(guest_path, stage_dir)
    if rel is not None and len(data) <= part_bytes:
        try:
            await write_part(rel, data)
            return
        except HttpError as exc:
            if exc.status not in (403, 413):
                raise
    stage = f".capsem-xfer-{uuid.uuid4().hex[:12]}"
    stage_q = shlex.quote(posixpath.join(stage_dir, stage))
    cleaned_inline = False
    try:
        for index, offset in enumerate(range(0, len(data), part_bytes)):
            part = data[offset : offset + part_bytes]
            await write_part(f"{stage}/{index:06d}", part)
        res = await controller.exec_in_vm(
            vm_id,
            f"mkdir -p {parent_q} && cat {stage_q}/* > {dest_q}; "
            f"__ec=$?; rm -rf {stage_q}; exit $__ec",
            timeout=300,
        )
        cleaned_inline = True
        if res.exit_code != 0:
            raise RuntimeError(
                f"Assembling upload to {guest_path} failed in VM {vm_id} "
                f"(exit {res.exit_code}): {res.stderr or res.stdout}"
            )
    finally:
        if not cleaned_inline:
            with contextlib.suppress(Exception):
                await controller.exec_in_vm(vm_id, f"rm -rf {stage_q}", timeout=60)


async def _staged_download(
    controller: Any,
    vm_id: str,
    guest_path: str,
    read_part: Callable[[str], Awaitable[bytes]],
    *,
    max_bytes: int | None = None,
) -> bytes:
    stage_dir = _XFER_STAGE_DIR
    part_bytes = _XFER_PART_BYTES
    rel = _rel_to_stage_dir(guest_path, stage_dir)
    if rel is not None:
        try:
            direct = await read_part(rel)
            return direct[: max_bytes + 1] if max_bytes is not None else direct
        except HttpError as exc:
            if exc.status not in (403, 413):
                raise
    stage = f".capsem-xfer-{uuid.uuid4().hex[:12]}"
    stage_q = shlex.quote(posixpath.join(stage_dir, stage))
    guest_q = shlex.quote(guest_path)
    split_cmd = (
        f"head -c {max_bytes + 1} -- {guest_q} | split -b {part_bytes} -d -a 6 - part."
        if max_bytes is not None
        else f"split -b {part_bytes} -d -a 6 {guest_q} part."
    )
    try:
        listing = await _exec_checked(
            controller,
            vm_id,
            f"set -o pipefail; [ -f {guest_q} ] && mkdir -p {stage_q} "
            f"&& cd {stage_q} && {split_cmd} && ls -1",
            timeout=300,
            what=f"Splitting {guest_path} for download",
        )
        names = sorted(line.strip() for line in listing.stdout.splitlines() if line.strip())
        parts: list[bytes] = []
        total = 0
        for name in names:
            chunk = await read_part(f"{stage}/{name}")
            if max_bytes is not None and total + len(chunk) > max_bytes:
                parts.append(chunk[: max_bytes + 1 - total])
                break
            parts.append(chunk)
            total += len(chunk)
        return b"".join(parts)
    finally:
        with contextlib.suppress(Exception):
            await controller.exec_in_vm(vm_id, f"rm -rf {stage_q}", timeout=60)
