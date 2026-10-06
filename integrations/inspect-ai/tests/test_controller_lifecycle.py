"""SDK controller session binding, list_vms, stop_vm, and persistent event loop tests."""

from __future__ import annotations

import asyncio
import http.server
import threading
from typing import Any

import capsem
import capsem._operations as api
import capsem.vm as vm_mod
import inspect_capsem.sandbox as sb
import pytest
from capsem import models
from capsem._transport import HttpError, MediaType, Method, Transport
from capsem.models import ExecOutput, ExecOutputEncoding
from inspect_capsem import SdkCapsemController


def _sandbox_info(
    vid: str,
    name: str | None = None,
    *,
    status: models.VmLifecycleState = models.VmLifecycleState.RUNNING,
    persistent: bool = False,
    labels: dict[str, str] | None = None,
) -> models.SandboxInfo:
    return models.SandboxInfo(
        id=vid,
        name=name,
        status=status,
        persistent=persistent,
        available_actions=[],
        pid=1,
        labels=labels,
    )


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


def test_sdk_controller_sessions_stop_list_and_exec_variants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exec_calls: list[tuple[str, str]] = []
    deleted_ids: list[str] = []
    written_files: dict[tuple[str, str], bytes] = {}

    async def fake_list_vms(transport: Any) -> Any:
        del transport
        return models.ListResponse(
            sandboxes=[
                _sandbox_info("d1", "d1", status=models.VmLifecycleState.RUNNING, persistent=False),
                _sandbox_info("o1", "obj", status=models.VmLifecycleState.STOPPED, persistent=True),
            ]
        )

    async def fake_exec_vm(
        transport: Any, *, id: str, body: Any, request_timeout: Any = None
    ) -> Any:
        del transport, request_timeout
        exec_calls.append((id, body.command))
        if id == "known":
            return _exec_response(
                4,
                f"bash {body.command}",
                "ZQ==",
                truncated=True,
                stderr_encoding=models.ExecOutputEncoding.BASE64,
            )
        return _exec_response(0, stdout="foreign-ok\n")

    async def fake_delete_vm(transport: Any, *, id: str) -> Any:
        del transport
        deleted_ids.append(id)
        return models.VmActionResponse(success=True)

    async def fake_write_file(self: Any, path: str, content: bytes | str) -> None:
        vm_id = await self._vm._resolve()
        written_files[(vm_id, path)] = (
            content.encode("utf-8") if isinstance(content, str) else bytes(content)
        )

    async def fake_read_bytes(self: Any, path: str) -> bytes:
        vm_id = await self._vm._resolve()
        return written_files[(vm_id, path)]

    monkeypatch.setattr(api, "list_vms", fake_list_vms)
    monkeypatch.setattr(api, "exec_vm", fake_exec_vm)
    monkeypatch.setattr(api, "delete_vm", fake_delete_vm)
    monkeypatch.setattr(vm_mod.Files, "write", fake_write_file)
    monkeypatch.setattr(vm_mod.Files, "read", fake_read_bytes)

    async def run() -> None:
        hv = capsem.Hypervisor("http://127.0.0.1:1", "t")
        ctrl = SdkCapsemController(hypervisor=hv)
        try:
            ctrl._sessions["known"] = hv.vm(id="known")
            res = await ctrl.exec_in_vm("known", "echo x", timeout=5)
            assert (res.exit_code, res.stdout, res.stderr, res.truncated) == (
                4,
                "bash echo x",
                "e",
                True,
            )
            assert [vm["id"] for vm in await ctrl.list_vms()] == ["d1", "o1"]

            assert "foreign-vm" not in ctrl._sessions
            res_f = await ctrl.exec_in_vm("foreign-vm", "uname -a", timeout=10)
            assert res_f.exit_code == 0 and res_f.stdout == "foreign-ok\n"
            assert "foreign-vm" in ctrl._sessions and isinstance(
                ctrl._sessions["foreign-vm"], capsem.VM
            )
            await ctrl.upload_to_vm("foreign-vm", "/root/hello.txt", b"world")
            assert await ctrl.download_from_vm("foreign-vm", "/root/hello.txt") == b"world"

            await ctrl.stop_vm("known")
            await ctrl.stop_vm("missing")
            assert "known" in deleted_ids and "missing" in deleted_ids
            await ctrl.stop_vm("foreign-vm")
        finally:
            await ctrl.close()

    asyncio.run(run())


def test_sdk_list_and_stop_leftover_vms(monkeypatch: pytest.MonkeyPatch) -> None:
    """`Hypervisor.list` returns a ListResponse; leftovers are deleted by id via the SDK."""
    deleted_ids: list[str] = []
    managed = {"managed-by": "inspect-capsem"}

    async def fake_list_vms(transport: Any) -> Any:
        del transport
        return models.ListResponse(
            sandboxes=[
                _sandbox_info("r1", "r1", status=models.VmLifecycleState.RUNNING, persistent=False),
                _sandbox_info("x1", None, status=models.VmLifecycleState.STOPPED, labels=managed),
                _sandbox_info("x2", None, status=models.VmLifecycleState.DEFUNCT, labels=managed),
            ]
        )

    async def fake_delete_vm(transport: Any, *, id: str) -> Any:
        del transport
        deleted_ids.append(id)
        return models.VmActionResponse(success=True)

    monkeypatch.setattr(api, "list_vms", fake_list_vms)
    monkeypatch.setattr(api, "delete_vm", fake_delete_vm)

    async def run() -> None:
        ctrl = SdkCapsemController(hypervisor=capsem.Hypervisor("http://127.0.0.1:1", "t"))
        try:
            x1_row = {
                "id": "x1",
                "name": "x1",
                "status": "Stopped",
                "persistent": False,
                "labels": managed,
            }
            x2_row = {
                "id": "x2",
                "name": "x2",
                "status": "Defunct",
                "persistent": False,
                "labels": managed,
            }
            assert await ctrl.list_vms() == [
                {"id": "r1", "name": "r1", "status": "Running", "persistent": False, "labels": {}},
                x1_row,
                x2_row,
            ]
            assert await sb.sweep_leftover_vms(ctrl) == ["x1", "x2"]
            assert deleted_ids == ["x1", "x2"]
            vm = ctrl._session_for("x1")
            assert isinstance(vm, capsem.VM) and vm.id == "x1"
        finally:
            await ctrl.close()

    asyncio.run(run())


def test_sdk_stop_vm_suppresses_404_and_reraises_other_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_delete_vm(transport: Any, *, id: str) -> Any:
        del transport
        if id == "vm-gone":
            raise HttpError(404, "VM not found")
        raise HttpError(500, "internal gateway error")

    monkeypatch.setattr(api, "delete_vm", fake_delete_vm)

    async def run() -> None:
        ctrl = SdkCapsemController(hypervisor=capsem.Hypervisor("http://127.0.0.1:1", "t"))
        try:
            await ctrl.stop_vm("vm-gone")
            with pytest.raises(HttpError, match="internal gateway error"):
                await ctrl.stop_vm("vm-boom")
        finally:
            await ctrl.close()

    asyncio.run(run())


@pytest.mark.asyncio
async def test_sdk_controller_reuses_persistent_event_loop_with_aiohttp() -> None:
    """SdkCapsemController reuses a persistent event loop across real Transport / aiohttp calls."""

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    transport = Transport(url, "tok")

    class TransportBackedVM:
        id = "sb-1"
        name = "test"

        async def exec(self, command: str, *, timeout_secs: int | None = None) -> object:
            del timeout_secs
            await transport.request(Method.POST, "/exec", accept=MediaType.JSON)
            return type(
                "ExecRes",
                (),
                {
                    "exit_code": 0,
                    "stdout": ExecOutput(data=f"ok:{command}\n", encoding=ExecOutputEncoding.UTF8),
                    "stderr": ExecOutput(data="", encoding=ExecOutputEncoding.UTF8),
                    "duration_ms": 1.0,
                    "truncated": False,
                },
            )()

        async def delete(self) -> None:
            await transport.request(Method.POST, "/delete", accept=MediaType.JSON)

    class TransportBackedHypervisor:
        def vm(self, *, name: str | None = None, id: str | None = None) -> TransportBackedVM:
            del name, id
            return TransportBackedVM()

        async def create(
            self,
            *,
            name: str = "",
            cpus: int | None = None,
            memory: int | None = None,
            labels: Any = None,
        ) -> TransportBackedVM:
            del name, cpus, memory, labels
            await transport.request(Method.POST, "/create", accept=MediaType.JSON)
            return TransportBackedVM()

        async def close(self) -> None:
            await transport.close()

    ctrl = SdkCapsemController(hypervisor=TransportBackedHypervisor())
    try:
        vid = await ctrl.start_vm(template="code", cpu_count=2, ram_gb=4)
        assert vid == "sb-1"
        res1 = await ctrl.exec_in_vm(vid, "echo hi", timeout=10)
        assert res1.exit_code == 0 and res1.stdout == "ok:echo hi\n"
        res2 = await ctrl.exec_in_vm(vid, "echo second", timeout=10)
        assert res2.exit_code == 0 and res2.stdout == "ok:echo second\n"
        await ctrl.stop_vm(vid)
    finally:
        await ctrl.close()
        srv.shutdown()
        srv.server_close()
