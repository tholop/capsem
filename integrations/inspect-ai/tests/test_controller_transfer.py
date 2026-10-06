"""SDK controller staged file transfer and Files API part chunking tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import capsem
import capsem._operations as api
import capsem.models as models
import capsem.vm as vm_mod
import inspect_capsem._transfer as xfer_mod
import pytest
from capsem._transport import HttpError
from inspect_capsem import CommandResult, SdkCapsemController

from .conftest import (
    Scripted,
    fail,
    ok,
)


def _exec_response(
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> models.ExecResponse:
    return models.ExecResponse(
        exit_code=exit_code,
        stdout=models.ExecOutput(data=stdout, encoding=models.ExecOutputEncoding.UTF8),
        stderr=models.ExecOutput(data=stderr, encoding=models.ExecOutputEncoding.UTF8),
        truncated=False,
    )


def test_sdk_direct_stage_dir_transfers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stage_dir = tmp_path / "root"
    stage_dir.mkdir()
    monkeypatch.setattr(xfer_mod, "_XFER_STAGE_DIR", str(stage_dir))
    monkeypatch.setattr(xfer_mod, "_XFER_PART_BYTES", 8)

    writes: list[tuple[str, bytes]] = []
    execs: list[str] = []

    async def fake_create_vm(transport: Any, *, body: Any, request_timeout: Any = None) -> Any:
        del transport, request_timeout
        return SimpleNamespace(id="vm-direct", name=body.name)

    async def fake_write_file(self: Any, path: str, data: bytes) -> None:
        del self
        writes.append((path, data))

    async def fake_read_file(self: Any, path: str) -> bytes:
        del self
        assert path == "direct/file.bin"
        return b"direct-bytes"

    async def fake_exec_vm(
        transport: Any, *, id: str, body: Any, request_timeout: Any = None
    ) -> Any:
        del transport, id, request_timeout
        execs.append(body.command)
        return _exec_response(0)

    monkeypatch.setattr(api, "create_vm", fake_create_vm)
    monkeypatch.setattr(vm_mod.Files, "write", fake_write_file)
    monkeypatch.setattr(vm_mod.Files, "read", fake_read_file)
    monkeypatch.setattr(api, "exec_vm", fake_exec_vm)

    async def run() -> None:
        sdk_ctrl = SdkCapsemController(hypervisor=capsem.Hypervisor("http://127.0.0.1:1", "t"))
        try:
            vid = await sdk_ctrl.start_vm(template="code", cpu_count=1, ram_gb=1)
            await sdk_ctrl.upload_to_vm(vid, str(stage_dir / "direct" / "file.bin"), b"abc")
            assert writes == [("direct/file.bin", b"abc")]
            assert (
                await sdk_ctrl.download_from_vm(vid, str(stage_dir / "direct" / "file.bin"))
                == b"direct-bytes"
            )
            assert (
                await sdk_ctrl.download_from_vm(
                    vid, str(stage_dir / "direct" / "file.bin"), max_bytes=5
                )
                == b"direct"
            )
            assert execs == []
        finally:
            await sdk_ctrl.close()

    asyncio.run(run())


def test_sdk_transfer_reports_assembly_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_create_vm(transport: Any, *, body: Any, request_timeout: Any = None) -> Any:
        del transport, request_timeout
        return SimpleNamespace(id="s", name=body.name)

    async def fake_write_file(self: Any, path: str, content: bytes | str) -> None:
        del self, path, content

    async def fake_exec_vm(
        transport: Any, *, id: str, body: Any, request_timeout: Any = None
    ) -> Any:
        del transport, id, request_timeout
        code = 1 if "cat " in body.command else 0
        return _exec_response(code, stdout="", stderr="disk full")

    monkeypatch.setattr(api, "create_vm", fake_create_vm)
    monkeypatch.setattr(vm_mod.Files, "write", fake_write_file)
    monkeypatch.setattr(api, "exec_vm", fake_exec_vm)
    monkeypatch.setattr(xfer_mod, "_XFER_STAGE_DIR", str(tmp_path))

    async def run() -> None:
        ctrl = SdkCapsemController(hypervisor=capsem.Hypervisor("http://127.0.0.1:1", "t"))
        try:
            vid = await ctrl.start_vm(template="code", cpu_count=1, ram_gb=1)
            with pytest.raises(RuntimeError, match="disk full"):
                await ctrl.upload_to_vm(vid, "/dest", b"abc")
        finally:
            await ctrl.close()

    asyncio.run(run())


def test_staged_transfers_suppress_finally_cleanup_error_and_catch_403_413(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(xfer_mod, "_XFER_STAGE_DIR", "/root")
    monkeypatch.setattr(xfer_mod, "_XFER_PART_BYTES", 4)

    class FailingCleanupController(Scripted):
        async def exec_in_vm(
            self, vm_id: str, command: str, *, timeout: int = 120
        ) -> CommandResult:
            if command.startswith(("rm -rf ", "rm -f ")):
                raise RuntimeError("VM gone during finally cleanup")
            return await super().exec_in_vm(vm_id, command, timeout=timeout)

    async def _noop_write(_p: str, _b: bytes) -> None:
        return None

    async def _empty_read(_p: str) -> bytes:
        return b""

    async def _fail_part(_p: str, _b: bytes) -> None:
        raise RuntimeError("part write failed")

    async def _read_404(rel: str) -> bytes:
        raise HttpError(404, f"Not found: /root/run-413/{rel}")

    async def _read_plain_413_in_path(rel: str) -> bytes:
        raise RuntimeError(f"permission denied reading /root/413/{rel}")

    async def _read_413(rel: str) -> bytes:
        if rel == "large.bin":
            raise HttpError(413, "Payload Too Large")
        return b"chunk-ok"

    async def _read_403(rel: str) -> bytes:
        if rel == "symlink.bin":
            raise HttpError(403, "Forbidden: symlink")
        return b"sym-ok"

    written_parts: list[tuple[str, bytes]] = []

    async def _write_403(rel: str, data: bytes) -> None:
        if rel == "symlink_dest.bin":
            raise HttpError(403, "Forbidden: symlink parent")
        written_parts.append((rel, data))

    async def run() -> None:
        ctrl_ok_up = Scripted()
        await xfer_mod._staged_upload(
            ctrl_ok_up, "vm-1", "/opt/out.bin", b"0123456789", _noop_write
        )
        assert len(ctrl_ok_up.commands) == 1
        assert "cat /root/.capsem-xfer-" in ctrl_ok_up.commands[0]
        assert "__ec=$?; rm -rf /root/.capsem-xfer-" in ctrl_ok_up.commands[0]

        await ctrl_ok_up.upload_to_vm("vm-1", "/opt/out.bin", b"data")
        assert ctrl_ok_up.uploads["/opt/out.bin"] == b"data"
        assert await ctrl_ok_up.download_from_vm("vm-1", "/opt/out.bin") == (b"downloaded")

        ctrl_403_up = Scripted()
        await xfer_mod._staged_upload(
            ctrl_403_up, "vm-1", "/root/symlink_dest.bin", b"hi", _write_403
        )
        assert len(written_parts) == 1
        assert any("cat /root/.capsem-xfer-" in c for c in ctrl_403_up.commands)

        ctrl_up = FailingCleanupController([("cat ", fail(stderr="assembly failed"))])
        with pytest.raises(RuntimeError, match="assembly failed"):
            await xfer_mod._staged_upload(
                ctrl_up, "vm-1", "/opt/out.bin", b"0123456789", _noop_write
            )
        with pytest.raises(RuntimeError, match="part write failed"):
            await xfer_mod._staged_upload(
                ctrl_up, "vm-1", "/opt/out.bin", b"0123456789", _fail_part
            )

        ctrl_down = FailingCleanupController([("split ", fail(stderr="split failed"))])
        with pytest.raises(RuntimeError, match="split failed"):
            await xfer_mod._staged_download(ctrl_down, "vm-1", "/opt/in.bin", _empty_read)

        ctrl_split = Scripted([("split -b", ok("part.000000\n"))])
        with pytest.raises(HttpError, match="Not found"):
            await xfer_mod._staged_download(ctrl_split, "vm-1", "/root/missing.bin", _read_404)
        assert ctrl_split.commands == []

        with pytest.raises(RuntimeError, match="permission denied"):
            await xfer_mod._staged_download(
                ctrl_split, "vm-1", "/root/missing.bin", _read_plain_413_in_path
            )
        assert ctrl_split.commands == []

        res_bytes = await xfer_mod._staged_download(
            ctrl_split, "vm-1", "/root/large.bin", _read_413
        )
        assert res_bytes == b"chunk-ok" and any("split -b" in c for c in ctrl_split.commands)

        ctrl_split_403 = Scripted([("split -b", ok("part.000000\n"))])
        res_403 = await xfer_mod._staged_download(
            ctrl_split_403, "vm-1", "/root/symlink.bin", _read_403
        )
        assert res_403 == b"sym-ok" and any("split -b" in c for c in ctrl_split_403.commands)

        read_parts: list[str] = []

        async def _read_multi(rel: str) -> bytes:
            read_parts.append(rel)
            return b"abcd"

        ctrl_bounded = Scripted([("split -b", ok("part.000000\npart.000001\npart.000002\n"))])
        res_bounded = await xfer_mod._staged_download(
            ctrl_bounded, "vm-1", "/opt/growing.bin", _read_multi, max_bytes=5
        )
        assert res_bounded == b"abcdab"
        assert len(read_parts) == 2
        assert any(
            "set -o pipefail; [ -f /opt/growing.bin ]" in c
            and "head -c 6 -- /opt/growing.bin | split -b" in c
            for c in ctrl_bounded.commands
        )

    asyncio.run(run())
