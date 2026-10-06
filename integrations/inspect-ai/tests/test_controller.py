"""SDK controller gateway resolution, template validation, and ephemeral VM creation unit tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import capsem
import capsem._operations as api
import inspect_capsem._controller as ctrl_mod
import inspect_capsem.sandbox as sb_mod
import pytest
from capsem import CreateTimeoutError, discover_gateway, models
from capsem._transport import HttpError
from inspect_capsem import CapsemSandboxEnvironment, SdkCapsemController

from .conftest import Scripted


def test_gateway_url_and_token_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    for k in ("CAPSEM_GATEWAY_URL", "CAPSEM_GATEWAY_TOKEN", "CAPSEM_RUN_DIR", "CAPSEM_HOME"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError, match="gateway bearer token not found"):
        SdkCapsemController()
    assert (discover_gateway(url="http://x", token="t").url, discover_gateway(token="t").url) == (
        "http://x",
        "http://127.0.0.1:19222",
    )

    run = tmp_path / ".capsem" / "run"
    run.mkdir(parents=True)
    (run / "gateway.port").write_text("4242\n")
    (run / "gateway.token").write_text("filetok\n")
    ctrl = SdkCapsemController()
    assert (ctrl._hypervisor._transport._url, ctrl._hypervisor._transport._token) == (
        "http://127.0.0.1:4242",
        "filetok",
    )
    custom_home_run = tmp_path / "custom-home" / "run"
    custom_home_run.mkdir(parents=True)
    (custom_home_run / "gateway.port").write_text("5353\n")
    (custom_home_run / "gateway.token").write_text("hometok\n")
    monkeypatch.setenv("CAPSEM_HOME", str(tmp_path / "custom-home"))
    ctrl_home = SdkCapsemController()
    assert (ctrl_home._hypervisor._transport._url, ctrl_home._hypervisor._transport._token) == (
        "http://127.0.0.1:5353",
        "hometok",
    )
    custom_run = tmp_path / "custom-run"
    custom_run.mkdir(parents=True)
    (custom_run / "gateway.port").write_text("6464\n")
    (custom_run / "gateway.token").write_text("runtok\n")
    monkeypatch.setenv("CAPSEM_RUN_DIR", str(custom_run))
    ctrl_run = SdkCapsemController()
    assert (ctrl_run._hypervisor._transport._url, ctrl_run._hypervisor._transport._token) == (
        "http://127.0.0.1:6464",
        "runtok",
    )
    monkeypatch.setenv("CAPSEM_GATEWAY_URL", "http://env")
    monkeypatch.setenv("CAPSEM_GATEWAY_TOKEN", "envtok")
    ctrl_env = SdkCapsemController()
    assert (ctrl_env._hypervisor._transport._url, ctrl_env._hypervisor._transport._token) == (
        "http://env",
        "envtok",
    )


def test_sdk_controller_builds_default_hypervisor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAPSEM_GATEWAY_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("CAPSEM_GATEWAY_TOKEN", "tok")

    async def run() -> None:
        ctrl = SdkCapsemController()
        try:
            assert type(ctrl._hypervisor).__name__ == "Hypervisor"
            assert ctrl._hypervisor._transport.timeout == ctrl_mod._SDK_CALL_TIMEOUT_SECS
        finally:
            await ctrl.close()

    asyncio.run(run())


def test_sdk_controller_ephemeral_create_and_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[Any] = []
    monkeypatch.delenv("CAPSEM_VM_PREFIX", raising=False)

    async def fake_create_vm(transport: Any, *, body: Any, request_timeout: Any = None) -> Any:
        del transport, request_timeout
        created.append(body)
        if body.ram_mb == 999 * 1024:
            return SimpleNamespace(id="", name=None)
        return SimpleNamespace(id=f"eph-{len(created)}", name=body.name)

    async def broken_close(self: Any) -> None:
        del self
        raise RuntimeError("close failure is ignored")

    monkeypatch.setattr(api, "create_vm", fake_create_vm)
    monkeypatch.setattr(capsem.Hypervisor, "close", broken_close)

    async def run() -> None:
        ctrl = SdkCapsemController(hypervisor=capsem.Hypervisor("http://127.0.0.1:1", "t"))
        try:
            vid = await ctrl.start_vm(template="default", cpu_count=1, ram_gb=1)
            assert vid == "eph-1" and created[-1].name is None and created[-1].persistent is False
            assert created[-1].labels == {"managed-by": "inspect-capsem"}
            vid_env = await ctrl.start_vm(
                template="code",
                cpu_count=2,
                ram_gb=4,
                env={"FOO": "bar"},
                labels={"inspect-capsem-task": "task-1"},
            )
            assert (
                vid_env == "eph-2" and created[-1].name is None and created[-1].persistent is False
            )
            assert created[-1].labels == {
                "managed-by": "inspect-capsem",
                "inspect-capsem-task": "task-1",
            }
            assert created[-1].env == {"FOO": "bar"}
            with pytest.raises(ValueError, match="select a VM by exactly one nonempty name or id"):
                await ctrl.start_vm(template="code", cpu_count=1, ram_gb=999)
        finally:
            await ctrl.close()

    asyncio.run(run())


def test_sdk_create_cleanup_on_504(monkeypatch: pytest.MonkeyPatch) -> None:
    """When create raises CreateTimeoutError with a vm_id, _cleanup_failed_create stops that VM."""
    deleted_ids: list[str] = []
    mode = {"kind": "504"}

    async def create_vm(transport: Any, *, body: Any, request_timeout: Any = None) -> Any:
        del transport, body, request_timeout
        if mode["kind"] == "504":
            raise HttpError(
                504,
                '{"code": "create_timeout", "vm_id": "orphan-504", "error": "container workload for VM orphan-504 did not become ready"}',
            )
        if mode["kind"] == "504_no_vm":
            raise HttpError(504, '{"code": "create_timeout", "error": "gateway timeout"}')
        if mode["kind"] == "transport_timeout":
            raise TimeoutError("client timeout")
        raise RuntimeError("VM creation failed (max 504)")

    async def delete_vm(transport: Any, *, id: str) -> Any:
        del transport
        deleted_ids.append(id)
        return models.VmActionResponse(success=True)

    monkeypatch.setattr(api, "create_vm", create_vm)
    monkeypatch.setattr(api, "delete_vm", delete_vm)

    async def run() -> None:
        ctrl = SdkCapsemController(hypervisor=capsem.Hypervisor("http://127.0.0.1:1", "t"))
        try:
            with pytest.raises(CreateTimeoutError, match="orphan-504") as exc_info:
                await ctrl.start_vm(template="code", cpu_count=1, ram_gb=1)
            assert exc_info.value.vm_id == "orphan-504"
            assert deleted_ids == ["orphan-504"]
            deleted_ids.clear()
            await ctrl._cleanup_failed_create(
                CreateTimeoutError(
                    "unnamed timeout", vm_id="vm-unnamed", status=504, body="unnamed timeout"
                )
            )
            assert deleted_ids == ["vm-unnamed"]
            deleted_ids.clear()
            mode["kind"] = "504_no_vm"
            with pytest.raises(CreateTimeoutError, match="gateway timeout"):
                await ctrl.start_vm(template="code", cpu_count=1, ram_gb=1)
            assert deleted_ids == []
            mode["kind"] = "transport_timeout"
            with pytest.raises(TimeoutError, match="Capsem SDK call did not finish within"):
                await ctrl.start_vm(template="code", cpu_count=1, ram_gb=1)
            assert deleted_ids == []
            mode["kind"] = "non_504"
            with pytest.raises(RuntimeError, match="VM creation failed"):
                await ctrl.start_vm(template="code", cpu_count=1, ram_gb=1)
            assert deleted_ids == []
        finally:
            await ctrl.close()

    asyncio.run(run())


@pytest.mark.asyncio
async def test_sdk_controller_rejects_unknown_template_and_prefix_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SdkCapsemController rejects unknown templates and isolates CAPSEM_VM_PREFIX via labels."""
    last_create: dict[str, Any] = {}

    class FakeHypervisor:
        async def create(
            self,
            *,
            name: str = "",
            cpus: int | None = None,
            memory: int | None = None,
            labels: dict[str, str] | None = None,
        ) -> Any:
            last_create.update({"name": name, "cpus": cpus, "memory": memory, "labels": labels})
            return SimpleNamespace(id="vm-ephemeral-1", name=name or None)

        async def close(self) -> None:
            return None

    ctrl = SdkCapsemController(hypervisor=FakeHypervisor())
    try:
        with pytest.raises(NotImplementedError, match="template"):
            await ctrl.start_vm(template="nonexistent-template", cpu_count=2, ram_gb=4)
        with pytest.raises(NotImplementedError, match="harbor"):
            await ctrl.start_vm(template="harbor", cpu_count=2, ram_gb=4)

        monkeypatch.setenv("CAPSEM_VM_PREFIX", "bench-a")
        assert ctrl_mod._managed_vm_prefix_slug() == "bench-a"
        assert ctrl_mod._managed_vm_labels(task_name="t1") == {
            "managed-by": "inspect-capsem",
            "inspect-capsem-prefix": "bench-a",
            "inspect-capsem-task": "t1",
        }
        vid = await ctrl.start_vm(template="code", cpu_count=2, ram_gb=4)
        assert vid == "vm-ephemeral-1"
        assert last_create["name"] == ""
        assert last_create["labels"] == {
            "managed-by": "inspect-capsem",
            "inspect-capsem-prefix": "bench-a",
        }

        monkeypatch.setenv("CAPSEM_VM_PREFIX", "inspect-capsem-run_2/x-")
        assert ctrl_mod._managed_vm_prefix_slug() == "run-2-x"
        monkeypatch.setenv("CAPSEM_VM_PREFIX", "inspect-capsem----")
        assert ctrl_mod._managed_vm_prefix_slug() == ""
        monkeypatch.setenv("CAPSEM_VM_PREFIX", "   ")
        assert ctrl_mod._managed_vm_prefix_slug() == ""

        monkeypatch.setenv("CAPSEM_VM_PREFIX", "run")
        scripted = Scripted()
        scripted.vms = [
            {
                "id": "vm-run-stopped",
                "status": "Stopped",
                "labels": {"managed-by": "inspect-capsem", "inspect-capsem-prefix": "run"},
            },
            {
                "id": "vm-run2-stopped",
                "status": "Stopped",
                "labels": {"managed-by": "inspect-capsem", "inspect-capsem-prefix": "run-2"},
            },
            {
                "id": "vm-default-stopped",
                "status": "Stopped",
                "labels": {"managed-by": "inspect-capsem"},
            },
            {"id": "vm-user-running", "name": "user-dev-vm", "status": "Running", "labels": {}},
        ]
        assert await sb_mod.sweep_leftover_vms(scripted) == ["vm-run-stopped"]

        scripted.stopped.clear()
        scripted.vms = [
            {
                "id": "vm-run-live",
                "status": "Running",
                "labels": {"managed-by": "inspect-capsem", "inspect-capsem-prefix": "run"},
            },
            {
                "id": "vm-run2-live",
                "status": "Running",
                "labels": {"managed-by": "inspect-capsem", "inspect-capsem-prefix": "run-2"},
            },
            {"id": "vm-user-live", "name": "user-dev-vm", "status": "Running", "labels": {}},
        ]
        monkeypatch.setattr(sb_mod, "SdkCapsemController", lambda: scripted)
        CapsemSandboxEnvironment._active_environments.clear()
        await CapsemSandboxEnvironment.cli_cleanup(None)
        assert scripted.stopped == ["vm-run-live"]

        monkeypatch.delenv("CAPSEM_VM_PREFIX", raising=False)
        scripted.stopped.clear()
        scripted.vms = [
            {
                "id": "vm-default-stopped",
                "status": "Stopped",
                "labels": {"managed-by": "inspect-capsem"},
            },
            {
                "id": "vm-run2-stopped",
                "status": "Stopped",
                "labels": {"managed-by": "inspect-capsem", "inspect-capsem-prefix": "run2"},
            },
        ]
        assert await sb_mod.sweep_leftover_vms(scripted) == ["vm-default-stopped"]
    finally:
        await ctrl.close()
