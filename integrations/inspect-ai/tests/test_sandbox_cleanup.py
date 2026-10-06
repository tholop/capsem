"""`CapsemSandboxEnvironment` cleanup, leftover sweep, and task isolation tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import inspect_capsem._lifecycle as lifecycle_mod
import inspect_capsem.sandbox as sb
import pytest
from inspect_capsem import CapsemSandboxConfig, CapsemSandboxEnvironment

from .conftest import LocalFakeCapsemController, Scripted


def test_cli_cleanup_branches(caplog: pytest.LogCaptureFixture) -> None:
    managed = {"managed-by": "inspect-capsem"}

    class PartialFailStop(Scripted):
        async def stop_vm(self, vm_id: str, *, timeout: float | None = None) -> None:
            del timeout
            if vm_id == "vm-fail":
                raise RuntimeError("stop failed on first vm")
            await super().stop_vm(vm_id)

    ctrl = PartialFailStop()
    ctrl.vms = [
        {"id": "vm-fail", "labels": managed},
        {"id": "vm-2", "labels": managed},
        {"id": "keep", "name": "user-vm", "labels": {}},
        {"name": ""},
    ]
    owned = CapsemSandboxEnvironment("vm-owned", ctrl)
    other = CapsemSandboxEnvironment("vm-other", ctrl)
    original = sb.SdkCapsemController
    cast(Any, sb).SdkCapsemController = lambda: ctrl
    try:
        asyncio.run(CapsemSandboxEnvironment.cli_cleanup("vm-owned"))
        assert "vm-owned" in ctrl.stopped
        asyncio.run(CapsemSandboxEnvironment.cli_cleanup("unmatched-id"))
        assert "keep" not in ctrl.stopped and ctrl.commands == []
        with caplog.at_level("WARNING", logger="inspect_capsem.sandbox"):
            asyncio.run(CapsemSandboxEnvironment.cli_cleanup("keep"))
        assert "keep" not in ctrl.stopped
        assert "VM 'keep' was not created by inspect-capsem" in caplog.text
        asyncio.run(CapsemSandboxEnvironment.cli_cleanup("vm-2"))
        assert "vm-2" in ctrl.stopped
        ctrl.stopped.remove("vm-2")
        caplog.clear()
        with caplog.at_level("WARNING", logger="inspect_capsem.sandbox"):
            asyncio.run(CapsemSandboxEnvironment.cli_cleanup(None))
        assert "Failed to clean up Capsem VM vm-fail" in caplog.text
        assert "vm-2" in ctrl.stopped and "keep" not in ctrl.stopped
        assert ctrl.commands == []

        class Down(Scripted):
            async def list_vms(self) -> list[dict[str, Any]]:
                raise RuntimeError("down")

        cast(Any, sb).SdkCapsemController = Down
        caplog.clear()
        with caplog.at_level("WARNING", logger="inspect_capsem.sandbox"):
            asyncio.run(CapsemSandboxEnvironment.cli_cleanup(None))
        assert "Controller list_vms failed during cli_cleanup" in caplog.text
    finally:
        cast(Any, sb).SdkCapsemController = original
    del owned, other


def test_sweep_deletes_only_idle_managed_vms(monkeypatch: pytest.MonkeyPatch) -> None:
    managed = {"managed-by": "inspect-capsem"}

    class Flaky(Scripted):
        async def stop_vm(self, vm_id: str) -> None:
            if vm_id == "e":
                raise RuntimeError("delete failed")
            await super().stop_vm(vm_id)

    ctrl = Flaky()
    ctrl.vms = [
        {"id": "a", "status": "Stopped", "persistent": False, "labels": managed},
        {"id": "b", "status": "Running", "persistent": False, "labels": managed},
        {"id": "c", "name": "user-vm", "status": "Stopped", "labels": {}},
        {"id": "d", "status": "Defunct", "persistent": False, "labels": managed},
        {"id": "e", "status": "Stopped", "persistent": False, "labels": managed},
        {"name": "unlabeled-no-id", "status": "Stopped", "labels": managed},
    ]
    assert asyncio.run(sb.sweep_leftover_vms(ctrl)) == ["a", "d"]
    assert ctrl.stopped == ["a", "d"]

    closed: list[bool] = []

    class Closing(Scripted):
        async def close(self) -> None:
            closed.append(True)

    closing = Closing()
    closing.vms = [{"id": "z", "status": "Stopped", "labels": managed}]
    monkeypatch.setattr(sb, "SdkCapsemController", lambda: closing)
    asyncio.run(CapsemSandboxEnvironment.task_init("t", None))
    assert closing.stopped == ["z"] and closed == [True]

    class Down(Scripted):
        async def list_vms(self) -> list[dict[str, Any]]:
            raise RuntimeError("down")

    assert asyncio.run(sb.sweep_leftover_vms(Down())) == []

    def broken_factory() -> Any:
        raise RuntimeError("no controller")

    monkeypatch.setattr(sb, "SdkCapsemController", broken_factory)
    asyncio.run(CapsemSandboxEnvironment.task_init("t", None))


def test_process_owned_vms_and_leftover_sweep_status_filtering(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    managed = {"managed-by": "inspect-capsem"}
    ctrl = Scripted()
    ctrl.vms = [
        {"id": "v-stop", "status": "Stopped", "labels": managed},
        {"id": "v-fail", "status": "Failed", "labels": managed},
        {"id": "v-boot", "status": "Booting", "labels": managed},
        {"id": "v-start", "status": "Starting", "labels": managed},
        {"id": "v-pause", "status": "Paused", "labels": managed},
        {"id": "v-own", "status": "Stopped", "labels": managed},
    ]
    monkeypatch.setattr(sb, "SdkCapsemController", lambda: ctrl)
    lifecycle_mod._register_process_owned_vm("v-own")
    try:
        swept = asyncio.run(sb.sweep_leftover_vms(ctrl))
        assert swept == ["v-stop", "v-fail"]
        assert "v-boot" not in ctrl.stopped
        assert "v-start" not in ctrl.stopped
        assert "v-pause" not in ctrl.stopped
        assert "v-own" not in ctrl.stopped
    finally:
        sb._atexit_sweep_process_owned_vms()
        assert "v-own" in ctrl.stopped

    # task_cleanup stops only entries matching task_name and leaves untagged (None) entries intact.
    ctrl2 = Scripted()
    monkeypatch.setattr(sb, "SdkCapsemController", lambda: ctrl2)
    env = CapsemSandboxEnvironment("v-env", ctrl2, task_name="t")
    env_untagged = CapsemSandboxEnvironment("v-untagged", ctrl2, task_name=None)
    lifecycle_mod._register_process_owned_vm("v-env", task_name="t")
    lifecycle_mod._register_process_owned_vm("v-orphan", task_name="t")
    lifecycle_mod._register_process_owned_vm("v-untagged", task_name=None)
    asyncio.run(CapsemSandboxEnvironment.task_cleanup("other", None, cleanup=False))
    assert ctrl2.stopped == []
    asyncio.run(CapsemSandboxEnvironment.task_cleanup("t", None, cleanup=True))
    assert set(ctrl2.stopped) == {"v-env", "v-orphan"}
    assert "v-untagged" in lifecycle_mod._PROCESS_OWNED_VMS
    asyncio.run(CapsemSandboxEnvironment.task_cleanup(cast(Any, None), None, cleanup=True))
    assert "v-untagged" in ctrl2.stopped
    del env, env_untagged

    # If stop_vm fails during sample_cleanup, log WARNING and keep vm_id registered so task_cleanup retries.
    class FlakyStopController(Scripted):
        def __init__(self) -> None:
            super().__init__()
            self.fail_next_stop = True

        async def stop_vm(self, vm_id: str, *, timeout: float | None = None) -> None:
            del timeout
            if self.fail_next_stop:
                self.fail_next_stop = False
                raise RuntimeError("transient gateway error")
            await super().stop_vm(vm_id)

    flaky_ctrl = FlakyStopController()
    monkeypatch.setattr(sb, "SdkCapsemController", lambda: flaky_ctrl)
    env_flaky = CapsemSandboxEnvironment("v-flaky", flaky_ctrl, task_name="t-flaky")
    lifecycle_mod._register_process_owned_vm("v-flaky", task_name="t-flaky")
    with caplog.at_level("WARNING", logger="inspect_capsem.sandbox"):
        asyncio.run(
            CapsemSandboxEnvironment.sample_cleanup(
                "t-flaky", None, {"default": env_flaky}, interrupted=False
            )
        )
    assert "Failed to stop Capsem VM v-flaky" in caplog.text
    assert "v-flaky" in lifecycle_mod._PROCESS_OWNED_VMS
    assert "v-flaky" not in flaky_ctrl.stopped
    asyncio.run(CapsemSandboxEnvironment.task_cleanup("t-flaky", None, cleanup=True))
    assert "v-flaky" in flaky_ctrl.stopped
    assert "v-flaky" not in lifecycle_mod._PROCESS_OWNED_VMS


@pytest.mark.asyncio
async def test_cli_cleanup_cleans_out_of_process_vms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cli_cleanup queries controller.list_vms() for out-of-process VMs and ignores test/sdk prefixes."""
    controller = LocalFakeCapsemController(tmp_path)
    monkeypatch.setattr(sb, "SdkCapsemController", lambda: controller)

    vid = await controller.start_vm(template="harbor", cpu_count=2, ram_gb=4)
    controller.vms["my-dev-vm"] = tmp_path / "my-dev-vm"
    controller.vms["sdk-vm-user"] = tmp_path / "sdk-vm-user"
    controller.vms["test-vm-user"] = tmp_path / "test-vm-user"
    CapsemSandboxEnvironment._active_environments.clear()

    await CapsemSandboxEnvironment.cli_cleanup("my-dev-vm")
    assert "my-dev-vm" not in controller.stopped_vms

    await CapsemSandboxEnvironment.cli_cleanup(None)
    assert vid in controller.stopped_vms
    assert "my-dev-vm" not in controller.stopped_vms
    assert "sdk-vm-user" not in controller.stopped_vms
    assert "test-vm-user" not in controller.stopped_vms


@pytest.mark.asyncio
async def test_multi_task_cleanup_isolation_and_no_sandbox_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """task_cleanup scopes teardown to task_name and prints surviving VMs on cleanup=False."""
    controller = LocalFakeCapsemController(tmp_path)
    monkeypatch.setattr(sb, "SdkCapsemController", lambda: controller)

    envs_a = await CapsemSandboxEnvironment.sample_init(
        task_name="task_a",
        config=CapsemSandboxConfig(working_dir=str(tmp_path)),
        metadata={},
    )
    envs_b = await CapsemSandboxEnvironment.sample_init(
        task_name="task_b",
        config=CapsemSandboxConfig(working_dir=str(tmp_path)),
        metadata={},
    )
    vm_a = cast(CapsemSandboxEnvironment, envs_a["default"]).vm_id
    vm_b = cast(CapsemSandboxEnvironment, envs_b["default"]).vm_id
    vm_untagged = await controller.start_vm(template="code", cpu_count=1, ram_gb=1)
    env_untagged = CapsemSandboxEnvironment(
        vm_id=vm_untagged,
        controller=cast(Any, controller),
        working_dir=str(tmp_path),
        task_name=None,
    )
    lifecycle_mod._register_process_owned_vm(vm_untagged, task_name=None)
    assert vm_a in lifecycle_mod._PROCESS_OWNED_VMS
    assert vm_b in lifecycle_mod._PROCESS_OWNED_VMS
    assert vm_untagged in lifecycle_mod._PROCESS_OWNED_VMS

    # Cleaning up task_a with cleanup=True stops only task_a's VM and leaves task_b + untagged active.
    await CapsemSandboxEnvironment.task_cleanup("task_a", None, cleanup=True)
    assert vm_a in controller.stopped_vms
    assert vm_b not in controller.stopped_vms
    assert vm_untagged not in controller.stopped_vms
    assert vm_a not in lifecycle_mod._PROCESS_OWNED_VMS
    assert vm_b in lifecycle_mod._PROCESS_OWNED_VMS
    assert vm_untagged in lifecycle_mod._PROCESS_OWNED_VMS
    assert any(e.vm_id == vm_b for e in CapsemSandboxEnvironment._active_environments.values())
    assert any(
        e.vm_id == vm_untagged for e in CapsemSandboxEnvironment._active_environments.values()
    )

    # --no-sandbox-cleanup (cleanup=False) prints the surviving VM ID and cleanup hint,
    # disarms atexit for task_b without stopping vm_b, and closes the controller.
    capsys.readouterr()
    await CapsemSandboxEnvironment.task_cleanup("task_b", None, cleanup=False)
    out_single = capsys.readouterr().out
    assert vm_b in out_single
    assert f"inspect sandbox cleanup capsem {vm_b}" in out_single
    assert vm_b not in controller.stopped_vms
    assert vm_b not in lifecycle_mod._PROCESS_OWNED_VMS
    assert not any(e.vm_id == vm_b for e in CapsemSandboxEnvironment._active_environments.values())

    # Multi-VM cleanup=False prints all surviving VM IDs and one cleanup command per ID.
    lifecycle_mod._register_process_owned_vm("vm-m1", task_name="task_m")
    lifecycle_mod._register_process_owned_vm("vm-m2", task_name="task_m")
    await CapsemSandboxEnvironment.task_cleanup("task_m", None, cleanup=False)
    out_multi = capsys.readouterr().out
    assert "vm-m1, vm-m2" in out_multi
    assert "inspect sandbox cleanup capsem vm-m1" in out_multi
    assert "inspect sandbox cleanup capsem vm-m2" in out_multi

    # Clean up the untagged VM before verifying sweep_process_owned_vms() is empty.
    await env_untagged.cleanup()
    assert await sb.sweep_process_owned_vms() == []
    assert vm_b not in controller.stopped_vms

    # cli_cleanup(None) narrows VM teardown to inspect-capsem-* prefix only.
    controller.vms["user-preexisting-vm"] = tmp_path / "user-preexisting-vm"
    await CapsemSandboxEnvironment.cli_cleanup(None)
    assert vm_b in controller.stopped_vms
    assert "user-preexisting-vm" not in controller.stopped_vms
    assert controller.close_count >= 2
