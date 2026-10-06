"""`CapsemSandboxEnvironment` initialization, properties, and teardown tests."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, cast

import inspect_capsem.sandbox as sb
import pytest
from inspect_capsem import (
    CapsemSandboxConfig,
    CapsemSandboxEnvironment,
    CommandResult,
)

from .conftest import (
    LocalFakeCapsemController,
    Scripted,
    env_for,
    run_init,
)


def test_environment_properties_and_connection() -> None:
    ctrl = Scripted()
    original = sb.SdkCapsemController
    cast(Any, sb).SdkCapsemController = lambda: ctrl
    try:
        default = CapsemSandboxEnvironment("vm-2")
        assert default.vm_id == "vm-2" and default._controller is ctrl
    finally:
        cast(Any, sb).SdkCapsemController = original
    assert CapsemSandboxEnvironment.config_files() == []
    assert not CapsemSandboxEnvironment.is_docker_compatible()
    assert CapsemSandboxEnvironment.default_concurrency() == 4
    conn = asyncio.run(env_for(ctrl).connection())
    assert conn.command == "capsem exec vm-s -- bash" and conn.container == "vm-s"


def test_sample_init_vm_mode() -> None:
    ctrl = Scripted()
    env = run_init(ctrl, CapsemSandboxConfig(environment={"FOO": "bar"}))
    assert env.vm_id == "vm-s"
    assert ctrl.started[0]["env"] == {"FOO": "bar"}
    assert ctrl.started[0]["labels"]["managed-by"] == "inspect-capsem"
    assert ctrl.started[0]["labels"]["inspect-capsem-task"] == "t"
    asyncio.run(env.cleanup())
    assert ctrl.stopped == ["vm-s"]


def test_failed_bake_tears_down_what_init_created() -> None:
    def hang(command: str) -> CommandResult:
        raise TimeoutError(command)

    ctrl = Scripted([("test -x", hang)])
    with pytest.raises(TimeoutError):
        run_init(ctrl, CapsemSandboxConfig())
    assert ctrl.stopped == ["vm-s"]


def test_failed_init_teardown_does_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def hang(command: str) -> CommandResult:
        raise TimeoutError(command)

    class SlowStop(Scripted):
        async def stop_vm(self, vm_id: str) -> None:
            await asyncio.sleep(0.2)
            if vm_id == "vm-s" and self.stopped:
                raise RuntimeError("second stop fails")
            await super().stop_vm(vm_id)

    ctrl = SlowStop([("test -x", hang)])
    ticks: list[float] = []

    async def ticker() -> None:
        while True:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.01)

    async def main() -> None:
        tick_task = asyncio.create_task(ticker())
        monkeypatch.setattr(sb, "SdkCapsemController", lambda: ctrl)
        try:
            with pytest.raises(TimeoutError):
                await CapsemSandboxEnvironment.sample_init("t", CapsemSandboxConfig(), {})
            # A failing teardown is logged; the original error still propagates.
            with pytest.raises(TimeoutError):
                await CapsemSandboxEnvironment.sample_init("t", CapsemSandboxConfig(), {})
        finally:
            tick_task.cancel()

    asyncio.run(main())
    assert ctrl.stopped == ["vm-s"]
    assert len(ticks) > 10


def test_failed_init_teardown_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    def hang(command: str) -> CommandResult:
        raise TimeoutError(command)

    class HungStop(Scripted):
        async def stop_vm(self, vm_id: str) -> None:
            del vm_id
            await asyncio.sleep(10.0)

    monkeypatch.setattr(sb, "_INIT_TEARDOWN_TIMEOUT_SECS", 0.1)
    t0 = time.monotonic()
    with pytest.raises(TimeoutError, match="test -x"):
        run_init(HungStop([("test -x", hang)]), CapsemSandboxConfig())
    assert time.monotonic() - t0 < 5


def test_sample_init_cancellation_cleans_up_started_vm(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    started_vms: list[str] = []

    async def _run() -> None:
        entered_bake = asyncio.Event()

        class CancelMidInitController(Scripted):
            async def start_vm(self, **_: Any) -> str:
                vid = f"vm-{len(started_vms) + 1}"
                started_vms.append(vid)
                return vid

            async def exec_in_vm(
                self, vm_id: str, command: str, *, timeout: int = 120
            ) -> CommandResult:
                if command.startswith("test -x"):
                    entered_bake.set()
                    await asyncio.sleep(10.0)
                return await super().exec_in_vm(vm_id, command, timeout=timeout)

        ctrl = CancelMidInitController()
        monkeypatch.setattr(sb, "SdkCapsemController", lambda: ctrl)
        task = asyncio.create_task(
            CapsemSandboxEnvironment.sample_init("t", CapsemSandboxConfig(), {})
        )
        await asyncio.wait_for(entered_bake.wait(), timeout=2.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert started_vms == ["vm-1"]
        assert ctrl.stopped == ["vm-1"]

        # Cancel while start_vm is still awaiting: finishes inside shield and stops VM
        # (and logs a warning if stop_vm fails).
        entered_start = asyncio.Event()
        finish_start = asyncio.Event()

        class CancelDuringStartController(Scripted):
            def __init__(self, *, fail_stop: bool = False) -> None:
                super().__init__()
                self.fail_stop = fail_stop

            async def start_vm(self, **_: Any) -> str:
                entered_start.set()
                await finish_start.wait()
                return "vm-during-start"

            async def stop_vm(self, vm_id: str, *, timeout: float | None = None) -> None:
                del timeout
                if self.fail_stop:
                    raise RuntimeError("stop after cancel failed")
                await super().stop_vm(vm_id)

        ctrl_start_ok = CancelDuringStartController(fail_stop=False)
        monkeypatch.setattr(sb, "SdkCapsemController", lambda: ctrl_start_ok)
        t_start = asyncio.create_task(
            CapsemSandboxEnvironment.sample_init("t", CapsemSandboxConfig(), {})
        )
        await asyncio.wait_for(entered_start.wait(), timeout=2.0)
        t_start.cancel()
        finish_start.set()
        with pytest.raises(asyncio.CancelledError):
            await t_start
        assert ctrl_start_ok.stopped == ["vm-during-start"]

        entered_start.clear()
        finish_start.clear()
        ctrl_start_fail = CancelDuringStartController(fail_stop=True)
        monkeypatch.setattr(sb, "SdkCapsemController", lambda: ctrl_start_fail)
        t_start_fail = asyncio.create_task(
            CapsemSandboxEnvironment.sample_init("t", CapsemSandboxConfig(), {})
        )
        await asyncio.wait_for(entered_start.wait(), timeout=2.0)
        t_start_fail.cancel()
        finish_start.set()
        with (
            caplog.at_level("WARNING", logger="inspect_capsem.sandbox"),
            pytest.raises(asyncio.CancelledError),
        ):
            await t_start_fail
        assert "Failed to stop Capsem VM vm-during-start after cancelled start_vm" in caplog.text

    asyncio.run(_run())


@pytest.mark.asyncio
async def test_controller_closed_on_sample_cleanup_and_failed_init(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-sample controller is closed in sample_cleanup and when sample_init fails."""
    created_controllers: list[LocalFakeCapsemController] = []

    def make_controller() -> LocalFakeCapsemController:
        c = LocalFakeCapsemController(tmp_path)
        created_controllers.append(c)
        return c

    monkeypatch.setattr(sb, "SdkCapsemController", make_controller)

    envs = await CapsemSandboxEnvironment.sample_init(
        task_name="close_ok",
        config=CapsemSandboxConfig(working_dir=str(tmp_path)),
        metadata={},
    )
    assert len(created_controllers) == 1
    assert created_controllers[0].close_count == 0
    await CapsemSandboxEnvironment.sample_cleanup("close_ok", None, envs, interrupted=False)
    assert created_controllers[0].close_count == 1

    # Idempotent second cleanup when stop_vm raises on already-stopped VM.
    env = envs["default"]
    assert isinstance(env, CapsemSandboxEnvironment)
    created_controllers[0].fail_stop_vm = True
    await env.cleanup()

    # Failed sample_init also closes its controller.
    with pytest.raises(NotImplementedError):
        await CapsemSandboxEnvironment.sample_init(
            task_name="close_fail",
            config=CapsemSandboxConfig(
                template="unknown",
            ),
            metadata={},
        )
    assert len(created_controllers) == 2
    assert created_controllers[1].close_count == 1
