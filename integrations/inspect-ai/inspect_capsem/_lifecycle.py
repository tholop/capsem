"""Process-owned VM registry, teardown, and leftover VM sweep helpers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shlex
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from inspect_capsem._controller import (
    _is_managed_vm,
    _managed_vm_labels,
    _managed_vm_prefix_slug,
)

if TYPE_CHECKING:
    from inspect_capsem._controller import CapsemController
    from inspect_capsem.config import CapsemSandboxConfig

__all__ = [
    "_INIT_TEARDOWN_TIMEOUT_SECS",
    "_PROCESS_OWNED_VMS",
    "_abandon_process_owned_vms",
    "_init_sample_vm",
    "_oci_create_command",
    "_register_process_owned_vm",
    "_report_surviving_vms",
    "_stop_vms",
    "_sweep_untracked_managed_vms",
    "_teardown_failed_init",
    "_unregister_process_owned_vm",
    "sweep_leftover_vms",
    "sweep_process_owned_vms",
]

logger = logging.getLogger("inspect_capsem.sandbox")

_INIT_TEARDOWN_TIMEOUT_SECS = 25.0
_TERMINAL_VM_STATUSES = frozenset(
    {"stopped", "exited", "failed", "error", "dead", "terminated", "crashed", "aborted", "defunct"}
)
_PROCESS_OWNED_VMS: dict[str, str | None] = {}


def _register_process_owned_vm(vm_id: str, task_name: str | None = None) -> None:
    if vm_id:
        _PROCESS_OWNED_VMS[vm_id] = task_name


def _unregister_process_owned_vm(vm_id: str) -> None:
    if vm_id:
        _PROCESS_OWNED_VMS.pop(vm_id, None)


def _abandon_process_owned_vms(task_name: str | None = None) -> list[str]:
    """Disarm process-owned tracking for task VMs so atexit will not sweep them."""
    surviving_ids = [
        vid
        for vid, rec in list(_PROCESS_OWNED_VMS.items())
        if task_name is None or rec == task_name
    ]
    for vid in surviving_ids:
        _PROCESS_OWNED_VMS.pop(vid, None)
    return surviving_ids


def _oci_create_command(cfg: CapsemSandboxConfig) -> tuple[str, ...] | None:
    if cfg.command is None:
        return None
    if isinstance(cfg.command, str):
        return tuple(shlex.split(cfg.command)) if cfg.command.strip() else None
    return tuple(cfg.command)


async def _init_sample_vm(
    controller: CapsemController, cfg: CapsemSandboxConfig, task_name: str
) -> str:
    """Start and register a VM for sample_init, cleaning up on cancellation."""
    oci_image = cfg.image if cfg.execution_mode == "container" else None
    oci_cmd = _oci_create_command(cfg) if cfg.execution_mode == "container" else None
    start_task = asyncio.ensure_future(
        controller.start_vm(
            template=cfg.template,
            cpu_count=cfg.cpu_count,
            ram_gb=cfg.ram_gb,
            image=oci_image,
            command=oci_cmd,
            env=dict(cfg.environment) if cfg.environment else None,
            labels=_managed_vm_labels(task_name=task_name),
        )
    )
    try:
        vm_id = await asyncio.shield(start_task)
    except asyncio.CancelledError:
        with contextlib.suppress(BaseException):
            vm_id = await asyncio.wait_for(
                asyncio.shield(start_task), timeout=_INIT_TEARDOWN_TIMEOUT_SECS
            )
            try:
                await controller.stop_vm(vm_id)
            except Exception as exc:
                logger.warning(
                    "Failed to stop Capsem VM %s after cancelled start_vm: %s", vm_id, exc
                )
        raise
    _register_process_owned_vm(vm_id, task_name=task_name)
    return vm_id


async def _stop_vms(controller: CapsemController, vm_ids: list[str], *, reason: str) -> list[str]:
    swept: list[str] = []
    for vid in vm_ids:
        try:
            await controller.stop_vm(vid)
            _unregister_process_owned_vm(vid)
            swept.append(vid)
        except Exception as exc:
            logger.warning(
                "Failed to stop %s Capsem VM %s "
                "(run 'inspect sandbox cleanup capsem' to retry): %s",
                reason,
                vid,
                exc,
            )
    if swept:
        logger.info("Stopped %d %s Capsem VM(s): %s", len(swept), reason, swept)
    return swept


async def sweep_process_owned_vms(
    *,
    controller_factory: Callable[[], CapsemController],
    task_name: str | None = None,
    controller: CapsemController | None = None,
) -> list[str]:
    """Stop any VMs started by this process (or by `task_name`) that have not yet been stopped."""
    matching_ids = [
        vid for vid, rec in _PROCESS_OWNED_VMS.items() if task_name is None or rec == task_name
    ]
    if not matching_ids:
        return []
    owns_controller = controller is None
    if controller is None:
        try:
            controller = controller_factory()
        except Exception:
            logger.debug("Controller creation failed during process VM sweep", exc_info=True)
            return []
    try:
        return await _stop_vms(controller, matching_ids, reason="orphaned process-owned")
    finally:
        if owns_controller:
            with contextlib.suppress(Exception):
                await controller.close()


async def _teardown_failed_init(
    controller: CapsemController, vm_id: str, timeout: float = _INIT_TEARDOWN_TIMEOUT_SECS
) -> None:
    try:
        await asyncio.wait_for(asyncio.shield(controller.stop_vm(vm_id)), timeout=timeout)
        _unregister_process_owned_vm(vm_id)
    except Exception as exc:
        logger.warning(
            "Cleanup after failed sample_init of VM %s failed "
            "(run 'inspect sandbox cleanup capsem' to retry): %s",
            vm_id,
            exc,
        )


async def sweep_leftover_vms(controller: CapsemController) -> list[str]:
    """Delete leaked `inspect-capsem` VMs in a terminal state; return their ids."""
    try:
        vms = await controller.list_vms()
    except Exception:
        logger.debug("list_vms failed during leftover VM sweep", exc_info=True)
        return []
    prefix, owned = _managed_vm_prefix_slug(), set(_PROCESS_OWNED_VMS.keys())
    target_ids = [
        vid
        for m in vms
        if (vid := str(m.get("id") or ""))
        and _is_managed_vm(m, prefix)
        and vid not in owned
        and str(m.get("status") or "").strip().lower() in _TERMINAL_VM_STATUSES
    ]
    return (
        await _stop_vms(controller, target_ids, reason="leftover inspect-capsem")
        if target_ids
        else []
    )


def _report_surviving_vms(surviving_ids: list[str]) -> None:
    if not surviving_ids:
        return
    if len(surviving_ids) == 1:
        sid = surviving_ids[0]
        print(
            f"Capsem sandbox VM left running: {sid}\n"
            f"Clean up with: inspect sandbox cleanup capsem {sid}"
        )
    else:
        cleanup = "\n".join(f"  inspect sandbox cleanup capsem {sid}" for sid in surviving_ids)
        joined = ", ".join(surviving_ids)
        print(f"Capsem sandbox VMs left running: {joined}\nClean up with:\n{cleanup}")


async def _sweep_untracked_managed_vms(
    controller_factory: Callable[[], CapsemController],
    id_filter: str | None,
    stopped_vms: set[str],
) -> None:
    prefix_slug = _managed_vm_prefix_slug()
    controller: CapsemController | None = None
    try:
        try:
            controller = controller_factory()
            vms = await controller.list_vms()
        except Exception:
            logger.warning("Controller list_vms failed during cli_cleanup", exc_info=True)
            vms = []
        for vm_info in vms:
            vid = str(vm_info.get("id") or vm_info.get("name") or "")
            vname = str(vm_info.get("name") or vid)
            if not vid or vid in stopped_vms:
                continue
            labels = vm_info.get("labels") or {}
            has_managed = (
                isinstance(labels, Mapping) and labels.get("managed-by") == "inspect-capsem"
            )
            is_managed = _is_managed_vm(vm_info, prefix_slug)
            if id_filter is not None:
                if id_filter not in (vid, vname):
                    continue
                if not is_managed:
                    reason = (
                        "belongs to a different inspect-capsem prefix"
                        if has_managed
                        else "was not created by inspect-capsem"
                    )
                    logger.warning(
                        "VM %r %s; use 'capsem' directly to manage it", id_filter, reason
                    )
                    continue
            elif not is_managed:
                continue
            if controller is not None:
                try:
                    await controller.stop_vm(vid)
                    _unregister_process_owned_vm(vid)
                    stopped_vms.add(vid)
                except Exception:
                    logger.warning("Failed to clean up Capsem VM %s", vid, exc_info=True)
    finally:
        if controller is not None:
            with contextlib.suppress(Exception):
                await controller.close()
