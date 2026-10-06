"""Structured exceptions raised by the Capsem gateway SDK."""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING

from . import models
from ._transport import CapsemError, HttpError

if TYPE_CHECKING:
    from .vm import VM

__all__ = [
    "CapsemError",
    "CreateTimeoutError",
    "ExecTimeoutError",
    "HttpError",
    "VmNotFoundError",
]


def _parse_error_response(body: str) -> models.ErrorResponse | None:
    with suppress(ValueError):
        return models.ErrorResponse.model_validate_json(body)
    return None


def _extract_create_timeout_vm_id(body: str) -> str | None:
    parsed = _parse_error_response(body)
    return parsed.vm_id if parsed is not None and parsed.code == "create_timeout" else None


def _extract_exec_timeout_secs(body: str) -> int | None:
    parsed = _parse_error_response(body)
    return parsed.timeout_secs if parsed is not None and parsed.code == "exec_timeout" else None


def _is_exec_timeout_http_error(error: HttpError) -> bool:
    if error.status in (408, 504):
        return True
    parsed = _parse_error_response(error.body)
    return error.status == 500 and parsed is not None and parsed.code == "exec_timeout"


def _is_vm_not_found_http_error(error: HttpError) -> bool:
    if error.status != 404:
        return False
    parsed = _parse_error_response(error.body)
    return parsed is not None and parsed.code == "vm_not_found"


class CreateTimeoutError(TimeoutError, CapsemError):
    """Raised when `Hypervisor.create` crosses the HTTP 504 or client deadline."""

    status: int | None
    body: str | None
    vm_id: str | None
    vm_name: str | None
    vm: VM | None
    deadline_secs: float | None

    def __init__(
        self,
        message: str,
        *,
        vm_id: str | None = None,
        vm_name: str | None = None,
        vm: VM | None = None,
        deadline_secs: float | None = None,
        status: int | None = None,
        body: str | None = None,
    ) -> None:
        self.vm_id = vm_id
        self.vm_name = vm_name
        self.vm = vm
        self.deadline_secs = deadline_secs
        self.status = status
        self.body = body
        TimeoutError.__init__(self, message)


class ExecTimeoutError(TimeoutError, CapsemError):
    """Raised when `VM.exec` or `Hypervisor.run` times out on the service or client."""

    status: int | None
    body: str | None
    vm_id: str | None
    command: str | None
    timeout_secs: int | None

    def __init__(
        self,
        message: str,
        *,
        vm_id: str | None = None,
        command: str | None = None,
        timeout_secs: int | None = None,
        status: int | None = None,
        body: str | None = None,
    ) -> None:
        self.vm_id = vm_id
        self.command = command
        self.timeout_secs = timeout_secs
        self.status = status
        self.body = body
        TimeoutError.__init__(self, message)


class VmNotFoundError(HttpError, LookupError):
    """Raised when a VM handle cannot be resolved by name or ID (HTTP 404)."""

    vm_id: str | None
    vm_name: str | None

    def __init__(
        self,
        body: str,
        *,
        vm_id: str | None = None,
        vm_name: str | None = None,
        status: int = 404,
    ) -> None:
        self.vm_id = vm_id
        self.vm_name = vm_name
        HttpError.__init__(self, status, body)
