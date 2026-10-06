"""Local argument validation and structured timeout/error wrapping for SDK operations."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING

from . import models
from ._transport import HttpError, Transport
from .errors import (
    CreateTimeoutError,
    ExecTimeoutError,
    VmNotFoundError,
    _extract_create_timeout_vm_id,
    _extract_exec_timeout_secs,
    _is_exec_timeout_http_error,
    _is_vm_not_found_http_error,
)
from .execution import (
    CREATE_READY_SECS,
    EXEC_TIMEOUT_CEILING_SECS,
    ExecResult,
)

if TYPE_CHECKING:
    from .vm import VM

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


async def _create_vm_with_timeout(
    op: Awaitable[models.ProvisionResponse],
    transport: Transport,
    *,
    bind_vm: Callable[..., VM],
    name: str,
    container: bool,
    request_timeout: float,
) -> VM:
    vm_name = name or None
    try:
        response = await op
    except HttpError as error:
        if error.status != 504:
            raise
        vm_id = _extract_create_timeout_vm_id(error.body)
        vm = (
            None
            if vm_id is None and vm_name is None
            else bind_vm(transport, id=vm_id, name=vm_name, container=container)
        )
        raise CreateTimeoutError(
            f"HTTP {error.status}: {error.body}",
            vm_id=vm_id,
            vm_name=vm_name,
            vm=vm,
            deadline_secs=float(CREATE_READY_SECS),
            status=error.status,
            body=error.body,
        ) from error
    except TimeoutError as error:
        vm = (
            None
            if vm_name is None
            else bind_vm(transport, id=None, name=vm_name, container=container)
        )
        raise CreateTimeoutError(
            str(error) or "create timed out",
            vm_id=None,
            vm_name=vm_name,
            vm=vm,
            deadline_secs=float(request_timeout),
        ) from error
    return bind_vm(transport, id=response.id, name=response.name, container=container)


async def _exec_with_timeout(
    op: Awaitable[models.ExecResponse],
    *,
    command: str,
    timeout_secs: int | None,
    vm_id: str | None = None,
    vm_name: str | None = None,
) -> ExecResult:
    effective_secs = (
        EXEC_TIMEOUT_CEILING_SECS
        if timeout_secs is None
        else min(timeout_secs, EXEC_TIMEOUT_CEILING_SECS)
    )
    try:
        response = await op
    except HttpError as error:
        if vm_id is not None and _is_vm_not_found_http_error(error):
            raise VmNotFoundError(
                error.body, vm_id=vm_id, vm_name=vm_name, status=error.status
            ) from error
        if not _is_exec_timeout_http_error(error):
            raise
        parsed_secs = _extract_exec_timeout_secs(error.body) or effective_secs
        raise ExecTimeoutError(
            f"HTTP {error.status}: {error.body}",
            vm_id=vm_id,
            command=command,
            timeout_secs=parsed_secs,
            status=error.status,
            body=error.body,
        ) from error
    except TimeoutError as error:
        raise ExecTimeoutError(
            str(error) or "command timed out",
            vm_id=vm_id,
            command=command,
            timeout_secs=effective_secs,
        ) from error
    return ExecResult.from_wire(response)
