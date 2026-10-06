"""SDK controller execution, timeout forwarding, and gateway error mapping tests."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import capsem
import capsem._operations as api
import inspect_capsem._controller as ctrl_mod
import pytest
from capsem import models
from capsem._transport import HttpError
from inspect_capsem import CommandResult, SdkCapsemController

from .conftest import Scripted, env_for


def _exec_response(
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
    *,
    truncated: bool = False,
    stderr_encoding: models.ExecOutputEncoding = models.ExecOutputEncoding.UTF8,
) -> models.ExecResponse:
    return models.ExecResponse(
        exit_code=exit_code,
        stdout=models.ExecOutput(data=stdout, encoding=models.ExecOutputEncoding.UTF8),
        stderr=models.ExecOutput(data=stderr, encoding=stderr_encoding),
        truncated=truncated,
    )


def test_inspect_exec_timeout_reaches_capsem() -> None:
    """A 900 s Inspect exec must give Capsem at least 900 s, not the server default."""
    seen: list[int] = []

    class Recorder(Scripted):
        async def exec_in_vm(
            self, vm_id: str, command: str, *, timeout: int = 120
        ) -> CommandResult:
            seen.append(timeout)
            return await super().exec_in_vm(vm_id, command, timeout=timeout)

    env = env_for(Recorder())
    asyncio.run(env.exec(["make"], timeout=900))
    assert seen[-1] >= 900


def test_sdk_exec_forwards_timeout_secs(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int | None] = []

    async def fake_create_vm(transport: Any, *, body: Any, request_timeout: Any = None) -> Any:
        del transport, request_timeout
        return SimpleNamespace(id="s", name=body.name)

    async def fake_exec_vm(
        transport: Any, *, id: str, body: Any, request_timeout: Any = None
    ) -> Any:
        del transport, id, request_timeout
        calls.append(body.timeout_secs)
        return _exec_response(0)

    monkeypatch.setattr(api, "create_vm", fake_create_vm)
    monkeypatch.setattr(api, "exec_vm", fake_exec_vm)

    async def run() -> None:
        ctrl = SdkCapsemController(hypervisor=capsem.Hypervisor("http://127.0.0.1:1", "t"))
        try:
            vid = await ctrl.start_vm(template="code", cpu_count=1, ram_gb=1)
            await ctrl.exec_in_vm(vid, "true", timeout=910)
            await ctrl.exec_in_vm(vid, "true", timeout=3610)
            assert calls == [910, 3600]
        finally:
            await ctrl.close()

    asyncio.run(run())


@pytest.mark.parametrize(("requested", "guest"), [(3600, 3590), (7200, 3590), (900, 900)])
def test_inspect_exec_timeout_stays_within_service_max(requested: int, guest: int) -> None:
    """The service rejects timeout_secs > 3600; the guest `timeout` fires first."""
    seen: list[tuple[int, str]] = []

    class Recorder(Scripted):
        async def exec_in_vm(
            self, vm_id: str, command: str, *, timeout: int = 120
        ) -> CommandResult:
            seen.append((timeout, command))
            return await super().exec_in_vm(vm_id, command, timeout=timeout)

    asyncio.run(env_for(Recorder()).exec(["make"], timeout=requested))
    timeout, command = next(s for s in seen if "timeout -k" in s[1])
    assert timeout == guest + 10 and timeout <= 3600
    assert f"timeout -k 1s {guest}s " in command


def test_sdk_controller_maps_gateway_errors_and_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_create_vm(transport: Any, *, body: Any, request_timeout: Any = None) -> Any:
        del transport, request_timeout
        return SimpleNamespace(id="s", name=body.name)

    async def fake_exec_vm(
        transport: Any, *, id: str, body: Any, request_timeout: Any = None
    ) -> Any:
        del transport, id, request_timeout
        if body.command == "slow":
            raise HttpError(
                500, '{"code": "exec_timeout", "error": "IPC command timed out after 1s"}'
            )
        if "slow504" in body.command:
            raise HttpError(504, "Gateway Timeout")
        if "slow408" in body.command:
            raise HttpError(408, "Request Timeout")
        if "false_positive_500" in body.command:
            raise HttpError(
                500,
                '{"code": "internal_error", "error": "database connection timed out in worker"}',
            )
        if "hang" in body.command:
            await asyncio.sleep(10)
        raise HttpError(500, "other failure")

    monkeypatch.setattr(api, "create_vm", fake_create_vm)
    monkeypatch.setattr(api, "exec_vm", fake_exec_vm)

    async def run() -> None:
        ctrl = SdkCapsemController(hypervisor=capsem.Hypervisor("http://127.0.0.1:1", "t"))
        try:
            vid = await ctrl.start_vm(template="code", cpu_count=1, ram_gb=1)
            with pytest.raises(TimeoutError, match="timed out"):
                await ctrl.exec_in_vm(vid, "slow", timeout=1)
            with pytest.raises(TimeoutError, match="timed out"):
                await ctrl.exec_in_vm(vid, "slow504", timeout=1)
            with pytest.raises(TimeoutError, match="timed out"):
                await ctrl.exec_in_vm(vid, "slow408", timeout=1)
            with pytest.raises(HttpError, match="database connection timed out"):
                await ctrl.exec_in_vm(vid, "false_positive_500", timeout=1)
            with pytest.raises(HttpError):
                await ctrl.exec_in_vm(vid, "fast", timeout=1)

            monkeypatch.setattr(ctrl_mod, "GATEWAY_REQUEST_BUDGET_SECS", 0)
            with pytest.raises(TimeoutError, match="Capsem SDK call did not finish"):
                await ctrl.exec_in_vm(vid, "hang", timeout=0)

            async def hang_delete(transport: Any, *, id: str) -> Any:
                del transport, id
                await asyncio.sleep(10)

            async def hang_list(transport: Any) -> Any:
                del transport
                await asyncio.sleep(10)

            monkeypatch.setattr(api, "delete_vm", hang_delete)
            monkeypatch.setattr(api, "list_vms", hang_list)
            with pytest.raises(TimeoutError, match="Capsem SDK call did not finish"):
                await ctrl.stop_vm(vid, timeout=0.02)
            with pytest.raises(TimeoutError, match="Capsem SDK call did not finish"):
                await ctrl.list_vms(timeout=0.02)
        finally:
            await ctrl.close()

    asyncio.run(run())


@pytest.mark.asyncio
async def test_sdk_controller_with_async_capsem_hypervisor_and_vm() -> None:
    from capsem.models import ExecOutput, ExecOutputEncoding

    class FakeAsyncVM:
        id = "vm-async-123"
        name = "sdk-vm-test"
        deleted = False

        async def exec(self, command: str, *, timeout_secs: int | None = None) -> object:
            del timeout_secs
            return type(
                "ExecRes",
                (),
                {
                    "exit_code": 0,
                    "stdout": ExecOutput(data=f"ran:{command}\n", encoding=ExecOutputEncoding.UTF8),
                    "stderr": ExecOutput(data="", encoding=ExecOutputEncoding.UTF8),
                    "duration_ms": 1.0,
                    "truncated": False,
                },
            )()

        async def delete(self) -> None:
            self.deleted = True

    class FakeAsyncHypervisor:
        def __init__(self) -> None:
            self._vm = FakeAsyncVM()

        def vm(self, *, name: str | None = None, id: str | None = None) -> FakeAsyncVM:
            del name, id
            return self._vm

        async def create(
            self,
            *,
            name: str = "",
            cpus: int | None = None,
            memory: int | None = None,
            labels: Any = None,
        ) -> FakeAsyncVM:
            del name, cpus, memory, labels
            return self._vm

        async def close(self) -> None:
            return None

    hv = FakeAsyncHypervisor()
    ctrl = SdkCapsemController(hypervisor=hv)
    try:
        vid = await ctrl.start_vm(template="code", cpu_count=2, ram_gb=4)
        assert vid == "vm-async-123"
        res = await ctrl.exec_in_vm(vid, "uname -a", timeout=30)
        assert res.exit_code == 0 and res.stdout == "ran:uname -a\n"
        await ctrl.stop_vm(vid)
        assert hv._vm.deleted is True
    finally:
        await ctrl.close()


def test_exec_output_text_decodes_sdk_streams() -> None:
    from capsem.execution import decode_exec_output
    from capsem.models import ExecOutput, ExecOutputEncoding

    assert (
        decode_exec_output(ExecOutput(data="hé\n", encoding=ExecOutputEncoding.UTF8)).decode(
            "utf-8", errors="replace"
        )
        == "hé\n"
    )
    b64 = ExecOutput(data="aGk=", encoding=ExecOutputEncoding.BASE64)
    assert decode_exec_output(b64).decode("utf-8", errors="replace") == "hi"


@pytest.mark.asyncio
async def test_sdk_controller_bounds_every_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HangingVM:
        id = "vm-hang"

        async def exec(self, command: str, *, timeout_secs: int | None = None) -> object:
            del command, timeout_secs
            await asyncio.sleep(3600)
            raise AssertionError("unreachable")

        async def delete(self) -> None:
            return None

    class HangingHypervisor:
        def vm(self, *, name: str | None = None, id: str | None = None) -> HangingVM:
            del name, id
            return HangingVM()

        async def create(self, **_: Any) -> object:
            return HangingVM()

        async def close(self) -> None:
            return None

    ctrl = SdkCapsemController(hypervisor=HangingHypervisor())
    try:
        vid = await ctrl.start_vm(template="code", cpu_count=1, ram_gb=1)
        monkeypatch.setattr(ctrl_mod, "GATEWAY_REQUEST_BUDGET_SECS", 0.2)
        t0 = time.monotonic()
        with pytest.raises(TimeoutError):
            await ctrl.exec_in_vm(vid, "true", timeout=0)
        assert time.monotonic() - t0 < 5
    finally:
        await ctrl.close()
