"""Capsem VM controller (gateway SDK), protocol, and helpers for Inspect AI."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from capsem import (
    CreateTimeoutError,
    ExecTimeoutError,
    HttpError,
    Hypervisor,
    VmNotFoundError,
)
from capsem.execution import (
    CREATE_READY_SECS,
    EXEC_TIMEOUT_CEILING_SECS,
    GATEWAY_REQUEST_BUDGET_SECS,
    decode_exec_output,
)

from inspect_capsem._transfer import _OCI_STAGE_DIR, _exec_checked, _staged_download, _staged_upload

__all__ = [
    "_MANAGED_BY_LABEL",
    "_MANAGED_BY_VALUE",
    "_PREFIX_LABEL",
    "_TASK_LABEL",
    "CapsemController",
    "CommandResult",
    "SdkCapsemController",
    "_exec_checked",
    "_is_managed_vm",
    "_managed_vm_labels",
    "_managed_vm_prefix_slug",
    "_normalize_image_ref",
    "is_root_user_spec",
]

logger = logging.getLogger(__name__)

_MANAGED_BY_LABEL, _MANAGED_BY_VALUE = "managed-by", "inspect-capsem"
_PREFIX_LABEL, _TASK_LABEL = "inspect-capsem-prefix", "inspect-capsem-task"
_SDK_CALL_TIMEOUT_SECS = 180.0
_BUILTIN_TEMPLATES = ("default", "code")


@dataclass(frozen=True)
class CommandResult:
    """Result of executing a shell command via a Capsem controller."""

    exit_code: int
    stdout: str
    stderr: str
    truncated: bool = False


@runtime_checkable
class CapsemController(Protocol):
    """Protocol for interacting with Capsem VMs."""

    async def start_vm(
        self,
        *,
        template: str,
        cpu_count: int,
        ram_gb: int,
        image: str | None = None,
        command: Sequence[str] | None = None,
        env: dict[str, str] | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> str: ...
    async def stop_vm(self, vm_id: str) -> None: ...
    async def list_vms(self) -> list[dict[str, Any]]: ...
    async def exec_in_vm(
        self, vm_id: str, command: str, *, timeout: int = 120
    ) -> CommandResult: ...
    async def upload_to_vm(self, vm_id: str, guest_path: str, data: bytes) -> None: ...
    async def download_from_vm(
        self, vm_id: str, guest_path: str, *, max_bytes: int | None = None
    ) -> bytes: ...
    async def close(self) -> None: ...


def is_root_user_spec(user: str | None) -> bool:
    """Return True when `user` is unset, empty, or resolves to root (`root`, `0`, `0:0`)."""
    if user is None:
        return True
    u, _, g = user.strip().partition(":")
    return u.strip().lower() in ("", "root", "0") and g.strip().lower() in ("", "root", "0")


def _normalize_image_ref(image: str | None) -> str | None:
    if not image or not image.strip():
        return None
    ref = image.strip()
    if "://" in ref or ref.startswith(("/", "./", "../")):
        return ref
    return f"docker://{ref}"


def _managed_vm_prefix_slug() -> str:
    raw = os.environ.get("CAPSEM_VM_PREFIX", "").strip()
    if not raw:
        return ""
    chars = [
        ch if ch.isalnum() and ch.isascii() else "-" for ch in raw.removeprefix("inspect-capsem-")
    ]
    return "-".join(part for part in "".join(chars).split("-") if part)[:63]


def _managed_vm_labels(*, task_name: str | None = None) -> dict[str, str]:
    labels = {_MANAGED_BY_LABEL: _MANAGED_BY_VALUE}
    if prefix := _managed_vm_prefix_slug():
        labels[_PREFIX_LABEL] = prefix
    if task_name:
        labels[_TASK_LABEL] = task_name[:255]
    return labels


def _is_managed_vm(vm_info: Mapping[str, Any], prefix_slug: str | None = None) -> bool:
    labels = vm_info.get("labels")
    if not isinstance(labels, Mapping) or labels.get(_MANAGED_BY_LABEL) != _MANAGED_BY_VALUE:
        return False
    return str(labels.get(_PREFIX_LABEL) or "") == (
        _managed_vm_prefix_slug() if prefix_slug is None else prefix_slug
    )


class SdkCapsemController:
    """`CapsemController` backed by `capsem.Hypervisor` and `capsem.VM`."""

    def __init__(
        self, hypervisor: Any | None = None, *, url: str | None = None, token: str | None = None
    ) -> None:
        self._call_timeout_secs = _SDK_CALL_TIMEOUT_SECS
        if hypervisor is None:
            hypervisor = Hypervisor.connect(url, token, timeout=self._call_timeout_secs)
        self._hypervisor = hypervisor
        self._sessions: dict[str, Any] = {}
        self._oci_vms: set[str] = set()

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._hypervisor.close(), timeout=30)

    async def _cleanup_failed_create(self, exc: BaseException) -> None:
        if isinstance(exc, CreateTimeoutError) and exc.vm_id:
            with contextlib.suppress(Exception):
                await self.stop_vm(exc.vm_id)

    async def start_vm(
        self,
        *,
        template: str,
        cpu_count: int,
        ram_gb: int,
        image: str | None = None,
        command: Sequence[str] | None = None,
        env: dict[str, str] | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> str:
        norm_image = _normalize_image_ref(image)
        if not norm_image and template not in ("", *_BUILTIN_TEMPLATES):
            raise NotImplementedError(f"Unsupported or unknown Capsem template: {template!r}")
        kwargs: dict[str, Any] = {
            "cpus": cpu_count,
            "memory": ram_gb,
            "labels": {**_managed_vm_labels(), **(dict(labels) if labels else {})},
        }
        if norm_image:
            kwargs["image"] = norm_image
        if command is not None:
            kwargs["command"] = list(command)
        if env:
            kwargs["env"] = dict(env)
        wait = float(CREATE_READY_SECS + GATEWAY_REQUEST_BUDGET_SECS)
        try:
            session = await asyncio.wait_for(self._hypervisor.create(**kwargs), timeout=wait)
        except (CreateTimeoutError, TimeoutError) as exc:
            await self._cleanup_failed_create(exc)
            if isinstance(exc, CreateTimeoutError) and exc.status is not None:
                raise
            raise TimeoutError(f"Capsem SDK call did not finish within {wait:.0f}s") from exc
        except BaseException as exc:
            await self._cleanup_failed_create(exc)
            raise
        vm_id = str(session.id)
        self._sessions[vm_id] = session
        if norm_image:
            self._oci_vms.add(vm_id)
        return vm_id

    async def stop_vm(self, vm_id: str, *, timeout: float = _SDK_CALL_TIMEOUT_SECS) -> None:
        self._oci_vms.discard(vm_id)
        session = self._sessions.pop(vm_id, None) or self._hypervisor.vm(id=vm_id)
        try:
            await asyncio.wait_for(session.delete(), timeout=timeout)
        except TimeoutError as exc:
            raise TimeoutError(f"Capsem SDK call did not finish within {timeout:.0f}s") from exc
        except (VmNotFoundError, HttpError) as exc:
            if isinstance(exc, VmNotFoundError) or exc.status == 404:
                logger.debug("VM %s already gone during stop_vm", vm_id)
            else:
                raise

    async def list_vms(self, *, timeout: float = _SDK_CALL_TIMEOUT_SECS) -> list[dict[str, Any]]:
        try:
            remote = await asyncio.wait_for(self._hypervisor.list(), timeout=timeout)
        except TimeoutError as exc:
            raise TimeoutError(f"Capsem SDK call did not finish within {timeout:.0f}s") from exc
        return [
            {
                "id": str(item.id or item.name or ""),
                "name": str(item.name or item.id or ""),
                "status": str(item.status.value),
                "persistent": bool(item.persistent),
                "labels": dict(item.labels) if item.labels else {},
            }
            for item in remote.sandboxes
        ]

    def _session_for(self, vm_id: str) -> Any:
        session = self._sessions.get(vm_id)
        if session is None:
            session = self._hypervisor.vm(id=vm_id)
            self._sessions[vm_id] = session
        return session

    async def exec_in_vm(self, vm_id: str, command: str, *, timeout: int = 120) -> CommandResult:
        session = self._session_for(vm_id)
        timeout = min(timeout, EXEC_TIMEOUT_CEILING_SECS)
        wait = float(timeout + GATEWAY_REQUEST_BUDGET_SECS)
        try:
            res = await asyncio.wait_for(session.exec(command, timeout_secs=timeout), timeout=wait)
        except (ExecTimeoutError, TimeoutError) as exc:
            if isinstance(exc, ExecTimeoutError) and exc.status is not None:
                raise TimeoutError(f"Capsem exec timed out after {timeout}s: {exc}") from exc
            raise TimeoutError(f"Capsem SDK call did not finish within {wait:.0f}s") from exc
        return CommandResult(
            exit_code=int(res.exit_code),
            stdout=decode_exec_output(res.stdout).decode("utf-8", errors="replace"),
            stderr=decode_exec_output(res.stderr).decode("utf-8", errors="replace"),
            truncated=bool(res.truncated),
        )

    async def upload_to_vm(self, vm_id: str, guest_path: str, data: bytes) -> None:
        files = self._session_for(vm_id).files
        stage_dir = _OCI_STAGE_DIR if vm_id in self._oci_vms else None
        await _staged_upload(
            self,
            vm_id,
            guest_path,
            data,
            lambda p, b: asyncio.wait_for(files.write(p, b), timeout=self._call_timeout_secs),
            stage_dir=stage_dir,
        )

    async def download_from_vm(
        self, vm_id: str, guest_path: str, *, max_bytes: int | None = None
    ) -> bytes:
        files = self._session_for(vm_id).files
        stage_dir = _OCI_STAGE_DIR if vm_id in self._oci_vms else None

        async def _read(p: str) -> bytes:
            return bytes(await asyncio.wait_for(files.read(p), timeout=self._call_timeout_secs))

        return await _staged_download(
            self, vm_id, guest_path, _read, max_bytes=max_bytes, stage_dir=stage_dir
        )
