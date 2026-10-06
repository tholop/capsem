"""OCI workload container staging, healthcheck, and working_dir probing tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import inspect_capsem.containers.runtime as rt_mod
import pytest
from inspect_capsem import CapsemSandboxConfig, CommandResult
from inspect_capsem.containers.runtime import (
    prepare_oci_workload_container,
    resolve_container_working_dir,
)

from ..conftest import Scripted, fail, ok


@pytest.mark.asyncio
async def test_prepare_oci_workload_container_and_working_dir_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class CaptureController:
        def __init__(self, *, exit_code: int = 0, stdout: str = "/app\n", stderr: str = "") -> None:
            self.exit_code, self.stdout, self.stderr = exit_code, stdout, stderr
            self.captured: list[str] = []
            self.uploads: dict[str, bytes] = {}

        async def exec_in_vm(
            self, vm_id: str, command: str, *, timeout: int = 120
        ) -> CommandResult:
            del vm_id, timeout
            self.captured.append(command)
            return CommandResult(exit_code=self.exit_code, stdout=self.stdout, stderr=self.stderr)

        async def upload_to_vm(self, vm_id: str, guest_path: str, data: bytes) -> None:
            del vm_id
            self.uploads[guest_path] = data

    ctrl = CaptureController()
    spec_default = CapsemSandboxConfig(image="alpine:3.19").to_container_spec()
    assert await prepare_oci_workload_container(cast(Any, ctrl), "vm-1", spec_default) == "workload"
    assert ctrl.captured == []
    assert await resolve_container_working_dir(cast(Any, ctrl), "vm-1", "workload") == "/app"
    assert (
        await resolve_container_working_dir(
            cast(Any, CaptureController(exit_code=1, stdout="")), "vm-1", "workload"
        )
        == "/"
    )

    spec_explicit = CapsemSandboxConfig(
        image="alpine:3.19", working_dir="/custom/work"
    ).to_container_spec()
    assert (
        await prepare_oci_workload_container(cast(Any, ctrl), "vm-1", spec_explicit) == "workload"
    )
    assert ctrl.captured == [
        "pwd",
        "mkdir -p /custom/work && (chmod a+rx /custom/work 2>/dev/null || true)",
    ]

    monkeypatch.setenv("CAPSEM_INSPECT_ALLOWED_HOST_PATHS", str(tmp_path))
    host_dir, host_file = tmp_path / "data", tmp_path / "single.txt"
    host_dir.mkdir()
    (host_dir / "hello.txt").write_text("hi\n")
    host_file.write_text("single\n")

    ctrl_vols = Scripted([("", ok())])
    cfg_vols = CapsemSandboxConfig(
        image="alpine:3.19",
        volumes=(f"{host_dir}:/mnt/data:ro", f"{host_file}:/etc/single.txt:ro"),
        allowed_host_paths=(str(tmp_path),),
    )
    assert (
        await prepare_oci_workload_container(
            cast(Any, ctrl_vols), "vm-1", cfg_vols.to_container_spec()
        )
        == "workload"
    )
    assert ctrl_vols.uploads["/etc/single.txt"] == b"single\n"
    assert any(p.startswith("/tmp/.capsem_bind_") for p in ctrl_vols.uploads)
    assert any("tar -xzf /tmp/.capsem_bind_0.tar.gz -C /mnt/data" in c for c in ctrl_vols.commands)

    monkeypatch.setattr(rt_mod, "_MAX_BIND_MOUNT_BYTES", 2)
    for vol in (f"{host_file}:/etc/single.txt:ro", f"{host_dir}:/mnt/data:ro"):
        spec = CapsemSandboxConfig(
            image="alpine:3.19", volumes=(vol,), allowed_host_paths=(str(tmp_path),)
        ).to_container_spec()
        with pytest.raises(ValueError, match="exceeds maximum size"):
            await prepare_oci_workload_container(cast(Any, ctrl_vols), "vm-1", spec)


@pytest.mark.asyncio
async def test_oci_healthcheck_polling() -> None:
    ctrl_ok = Scripted([("check_ok", ok())])
    spec_ok = CapsemSandboxConfig(
        image="ubuntu:24.04",
        healthcheck={"test": ["CMD-SHELL", "check_ok"], "retries": 2, "interval": "10ms"},
    ).to_container_spec()
    assert await prepare_oci_workload_container(ctrl_ok, "vm-1", spec_ok) == "workload"
    assert any("sh -c check_ok" in c for c in ctrl_ok.commands)

    ctrl_fail = Scripted([("check_hc", fail(1, stderr="hc failed"))])
    spec_fail = CapsemSandboxConfig(
        image="ubuntu:24.04",
        healthcheck={"test": ["CMD", "check_hc"], "retries": 1, "interval": 0.01},
    ).to_container_spec()
    with pytest.raises(RuntimeError, match="failed healthcheck"):
        await prepare_oci_workload_container(ctrl_fail, "vm-1", spec_fail)
