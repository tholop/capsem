"""VM file transfer path sanitization and SDK transfer self_check tests."""

from __future__ import annotations

import asyncio
import base64 as b64
import os
import subprocess as sp
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import inspect_capsem._tools as tools_mod
import inspect_capsem._transfer as xfer_mod
import pytest
from capsem.models import ExecOutput, ExecOutputEncoding
from inspect_capsem import CapsemSandboxEnvironment, SdkCapsemController
from inspect_capsem._tools import _chown_to_container_user_snippet

from .conftest import Scripted, _host_timeout_skips, _run_inspect_self_check, env_for, fail, ok


def test_rel_to_stage_dir_path_validation() -> None:
    for unsafe_rel in (
        "/workspace/a:b.txt",
        "/workspace/a@b.txt",
        "/workspace/a+b.txt",
        "/workspace/../etc/passwd",
        "/workspace/a..b",
    ):
        assert xfer_mod._rel_to_stage_dir(unsafe_rel, "/workspace") is None
    assert xfer_mod._rel_to_stage_dir("/root/a..b", "/root") is None
    assert xfer_mod._rel_to_stage_dir("/root", "/root") is None
    assert (
        xfer_mod._rel_to_stage_dir("/workspace/sub/ok_file-1.txt", "/workspace")
        == "sub/ok_file-1.txt"
    )
    assert xfer_mod._rel_to_stage_dir("/root/.bashrc", "/root") == ".bashrc"
    assert xfer_mod._rel_to_stage_dir("/root/.config/app.toml", "/root") == ".config/app.toml"


def test_oci_container_write_file_chowns_to_nonroot_user(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """OCI container write_file chowns to cfg.user or /proc/1 and warns on failure."""
    bin_dir, chown_log = tmp_path / "bin", tmp_path / "chown.log"
    bin_dir.mkdir()
    fake_chown = bin_dir / "chown"
    fake_chown.write_text(f'#!/bin/sh\necho "$@" >> "{chown_log}"\n')
    fake_chown.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}"}

    for cfg_user, arg, expected in (
        ("developer", "imageuser", "developer: /home/dev/file.py"),
        ("", "1000:1000", "1000:1000 /home/dev/file.py"),
    ):
        sp.run(
            [
                "/bin/sh",
                "-c",
                _chown_to_container_user_snippet("'/home/dev/file.py'", cfg_user),
                "sh",
                arg,
            ],
            env=env,
            check=True,
        )
        assert chown_log.read_text().strip() == expected
        chown_log.unlink()

    sp.run(
        [
            "/bin/sh",
            "-c",
            _chown_to_container_user_snippet("'/root/file.py'", "root"),
            "sh",
            "1000:1000",
        ],
        env=env,
        check=True,
    )
    assert not chown_log.exists()

    ctrl_oci = Scripted([("", ok())])
    sb_oci = env_for(
        ctrl_oci,
        container_id=tools_mod._OCI_WORKLOAD_CONTAINER_ID,
        execution_mode="container",
        user="1000:1000",
    )
    asyncio.run(sb_oci.write_file("/workspace/fix.py", "x = 1\n"))
    assert all(s in "\n".join(ctrl_oci.commands) for s in ("/proc/1", "chown ", "1000:1000"))

    caplog.clear()
    ctrl_warn = Scripted([("chown", fail(1, stderr="chown: invalid group"))])
    sb_warn = env_for(
        ctrl_warn,
        container_id=tools_mod._OCI_WORKLOAD_CONTAINER_ID,
        execution_mode="container",
        user="bad:group",
    )
    asyncio.run(sb_warn.write_file("/workspace/b.py", "1"))
    assert "Failed to chown /workspace/b.py in container workload" in caplog.text


class _LocalFiles:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.writes: list[int] = []

    async def write(self, path: str, data: bytes) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        self.writes.append(len(data))

    async def read(self, path: str) -> bytes:
        return (self.root / path).read_bytes()


class _LocalSdkVM:
    def __init__(self, root: Path) -> None:
        self.id = self.name = "vm-local-sdk"
        self.files = _LocalFiles(root)
        self.exec_count = 0

    async def exec(self, command: str, *, timeout_secs: int | None = None) -> object:
        self.exec_count += 1
        proc = sp.run(
            ["bash", "-c", command], capture_output=True, timeout=timeout_secs, check=False
        )

        def _stream(raw: bytes) -> ExecOutput:
            return ExecOutput(data=b64.b64encode(raw).decode(), encoding=ExecOutputEncoding.BASE64)

        return SimpleNamespace(
            exit_code=proc.returncode,
            stdout=_stream(proc.stdout),
            stderr=_stream(proc.stderr),
            truncated=False,
        )

    async def delete(self) -> None:
        return None


class _LocalSdkHypervisor:
    def __init__(self, root: Path) -> None:
        self._vm = _LocalSdkVM(root)

    def vm(self, *, name: str | None = None, id: str | None = None) -> _LocalSdkVM:
        del name, id
        return self._vm

    async def create(self, **_: Any) -> object:
        return self._vm

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_sdk_controller_file_transfer_self_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writes and reads go through Files API parts, not base64 exec chunks."""
    stage_root = tmp_path / "root"
    stage_root.mkdir()
    monkeypatch.setattr(xfer_mod, "_XFER_STAGE_DIR", str(stage_root))
    monkeypatch.setattr(xfer_mod, "_XFER_PART_BYTES", 1024)
    hv = _LocalSdkHypervisor(stage_root)
    ctrl = SdkCapsemController(hypervisor=hv)
    work = tmp_path / "work"
    work.mkdir()
    try:
        vid = await ctrl.start_vm(template="code", cpu_count=1, ram_gb=1)
        data = os.urandom(5000)
        await ctrl.upload_to_vm(vid, str(work / "sub" / "blob.bin"), data)
        assert (work / "sub" / "blob.bin").read_bytes() == data
        assert hv._vm.files.writes == [1024, 1024, 1024, 1024, 904]
        assert await ctrl.download_from_vm(vid, str(work / "sub" / "blob.bin")) == data
        await ctrl.upload_to_vm(vid, str(work / "empty.bin"), b"")
        assert await ctrl.download_from_vm(vid, str(work / "empty.bin")) == b""
        assert list(stage_root.iterdir()) == []

        env = CapsemSandboxEnvironment(vm_id=vid, controller=ctrl, working_dir=str(work))
        before = hv._vm.exec_count
        big = os.urandom(200_000)
        await env.write_file("big.bin", big)
        assert await env.read_file("big.bin", text=False) == big
        assert hv._vm.exec_count - before < 12
        results = await _run_inspect_self_check(
            env,
            skip={
                "test_read_and_write_large_file_binary",
                "test_exec_input_large",
                "test_exec_as_user",
            }
            | _host_timeout_skips(),
        )
        failures = {k: v for k, v in results.items() if v is not True}
        assert failures == {}, f"self_check via SDK transfer failed: {failures}"
    finally:
        await ctrl.close()
