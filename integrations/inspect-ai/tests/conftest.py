"""Shared pytest fixtures and fake controllers for inspect-capsem tests."""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import inspect_capsem.sandbox as sb
import pytest
from inspect_ai.util._sandbox import self_check as inspect_self_check
from inspect_capsem import CapsemSandboxConfig, CapsemSandboxEnvironment, CommandResult
from inspect_capsem._controller import _managed_vm_labels

Result = CommandResult | Callable[[str], CommandResult]
Rules = Sequence[tuple[str, Result]]


def ok(stdout: str = "", stderr: str = "") -> CommandResult:
    return CommandResult(exit_code=0, stdout=stdout, stderr=stderr)


def fail(code: int = 1, stdout: str = "", stderr: str = "boom") -> CommandResult:
    return CommandResult(exit_code=code, stdout=stdout, stderr=stderr)


class Scripted:
    """Async controller answering each command from the first rule whose needle it contains."""

    def __init__(self, rules: Rules = ()) -> None:
        self.rules: list[tuple[str, Result]] = list(rules)
        self.commands: list[str] = []
        self.started: list[dict[str, Any]] = []
        self.stopped: list[str] = []
        self.vms: list[dict[str, Any]] = []
        self.uploads: dict[str, bytes] = {}
        self.files: dict[str, bytes] = {}
        self.download_max_bytes: list[int | None] = []
        self.closed: bool = False

    async def start_vm(self, **kwargs: Any) -> str:
        self.started.append(kwargs)
        return "vm-s"

    async def stop_vm(self, vm_id: str) -> None:
        self.stopped.append(vm_id)

    async def list_vms(self) -> list[dict[str, Any]]:
        return self.vms

    async def exec_in_vm(self, vm_id: str, command: str, *, timeout: int = 120) -> CommandResult:
        del vm_id, timeout
        self.commands.append(command)
        for needle, res in self.rules:
            if needle in command:
                return res if isinstance(res, CommandResult) else res(command)
        return ok()

    async def upload_to_vm(self, vm_id: str, guest_path: str, data: bytes) -> None:
        del vm_id
        self.uploads[guest_path] = data

    async def download_from_vm(
        self, vm_id: str, guest_path: str, *, max_bytes: int | None = None
    ) -> bytes:
        del vm_id
        self.download_max_bytes.append(max_bytes)
        data = self.files.get(guest_path, b"downloaded")
        return data[: max_bytes + 1] if max_bytes is not None else data

    async def close(self) -> None:
        self.closed = True


class LocalFakeCapsemController:
    """Host-backed controller mapping VM IDs to local directories for self_check."""

    def __init__(self, root_dir: Path, *, skip_bake: bool = True) -> None:
        self.root_dir = root_dir
        self.skip_bake = skip_bake
        self.vms: dict[str, Path] = {}
        self.vm_labels: dict[str, dict[str, str]] = {}
        self.started_vms: list[dict[str, Any]] = []
        self.stopped_vms: list[str] = []
        self.commands: list[str] = []
        self.fail_stop_vm: bool = False
        self.close_count: int = 0

    async def close(self) -> None:
        self.close_count += 1

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
        del image, command, env
        if template == "unknown":
            raise NotImplementedError(f"Unsupported template: {template}")
        vm_id = f"vm-fake-{len(self.started_vms)}"
        vm_root = self.root_dir / vm_id
        vm_root.mkdir(parents=True, exist_ok=True)
        self.vms[vm_id] = vm_root
        vm_labels = {**_managed_vm_labels(), **(dict(labels) if labels else {})}
        self.vm_labels[vm_id] = vm_labels
        self.started_vms.append(
            {
                "vm_id": vm_id,
                "template": template,
                "cpu_count": cpu_count,
                "ram_gb": ram_gb,
                "labels": vm_labels,
            }
        )
        return vm_id

    async def stop_vm(self, vm_id: str) -> None:
        if self.fail_stop_vm:
            raise RuntimeError("404 not found")
        self.stopped_vms.append(vm_id)
        self.vms.pop(vm_id, None)
        self.vm_labels.pop(vm_id, None)

    async def list_vms(self) -> list[dict[str, Any]]:
        return [
            {
                "id": vid,
                "name": vid,
                "status": "Running",
                "persistent": False,
                "labels": dict(self.vm_labels.get(vid, {})),
            }
            for vid in self.vms
        ]

    def _resolve_path(self, vm_id: str, guest_path: str) -> Path:
        vm_root = self.vms.setdefault(vm_id, self.root_dir / vm_id)
        vm_root.mkdir(parents=True, exist_ok=True)
        for guest_prefix in (
            "/var/tmp/sandbox-services",
            "/var/tmp/.da7be258e003d428",
        ):
            if guest_path.startswith(guest_prefix):
                host_equiv = str(vm_root / guest_prefix.lstrip("/"))
                return Path(guest_path.replace(guest_prefix, host_equiv, 1))
        return Path(guest_path)

    async def upload_to_vm(self, vm_id: str, guest_path: str, data: bytes) -> None:
        target = self._resolve_path(vm_id, guest_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    async def download_from_vm(
        self, vm_id: str, guest_path: str, *, max_bytes: int | None = None
    ) -> bytes:
        target = self._resolve_path(vm_id, guest_path)
        with target.open("rb") as f:
            return f.read(max_bytes + 1) if max_bytes is not None else f.read()

    def _translate_cmd(self, vm_root: Path, command: str) -> str:
        text = command
        if self.skip_bake and text.startswith("test -x /var/tmp/sandbox-services/"):
            return "true"
        for guest_prefix in (
            "/var/tmp/sandbox-services",
            "/var/tmp/.da7be258e003d428",
        ):
            host_equiv = str(vm_root / guest_prefix.lstrip("/"))
            text = text.replace(guest_prefix, host_equiv)
        return text

    async def exec_in_vm(self, vm_id: str, command: str, *, timeout: int = 120) -> CommandResult:
        self.commands.append(command)
        vm_root = self.vms.setdefault(vm_id, self.root_dir / vm_id)
        vm_root.mkdir(parents=True, exist_ok=True)
        translated = self._translate_cmd(vm_root, command)
        proc = subprocess.run(
            ["bash", "-c", translated],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return CommandResult(
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )


def env_for(ctrl: Any, **kwargs: Any) -> CapsemSandboxEnvironment:
    kwargs.setdefault("execution_mode", "vm")
    return CapsemSandboxEnvironment(vm_id="vm-s", controller=ctrl, **kwargs)


def run_init(
    ctrl: Any, cfg: CapsemSandboxConfig, metadata: dict[str, str] | None = None
) -> CapsemSandboxEnvironment:
    original = sb.SdkCapsemController
    cast(Any, sb).SdkCapsemController = lambda: ctrl
    try:
        envs = asyncio.run(CapsemSandboxEnvironment.sample_init("t", cfg, metadata or {}))
    finally:
        cast(Any, sb).SdkCapsemController = original
    env = envs["default"]
    assert isinstance(env, CapsemSandboxEnvironment)
    return env


def _host_timeout_skips() -> set[str]:
    """Self-checks that host-run fakes can pass only with GNU `timeout`.

    uutils `timeout` (Ubuntu 26.04) exits 15, not GNU's 143, when its child dies
    of SIGTERM. Capsem guests ship GNU coreutils, so the live check runs it.
    """
    version = subprocess.run(
        ["timeout", "--version"], capture_output=True, text=True, check=False
    ).stdout
    return {"test_exec_timeout_not_raised_on_fast_signal_death"} if "uutils" in version else set()


async def _run_inspect_self_check(
    env: CapsemSandboxEnvironment, *, skip: set[str] | None = None
) -> dict[str, bool | str]:
    results: dict[str, bool | str] = {}
    for name in inspect_self_check.__all__:
        if skip and name in skip:
            continue
        fn = getattr(inspect_self_check, name)
        try:
            await fn(env)
            results[name] = True
        except Exception as exc:
            results[name] = f"{type(exc).__name__}: {exc}"
    return results


@pytest.fixture(autouse=True)
def _clear_process_owned_vms() -> Iterator[None]:
    import inspect_capsem._lifecycle as lifecycle_mod

    lifecycle_mod._PROCESS_OWNED_VMS.clear()
    CapsemSandboxEnvironment._active_environments.clear()
    yield
    lifecycle_mod._PROCESS_OWNED_VMS.clear()
    CapsemSandboxEnvironment._active_environments.clear()
