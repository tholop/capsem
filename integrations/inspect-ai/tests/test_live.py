"""Live Capsem VM conformance checks (`CAPSEM_LIVE_SELF_CHECK=1`)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from inspect_capsem import CapsemSandboxConfig, CapsemSandboxEnvironment

from .conftest import _run_inspect_self_check


@pytest.mark.skipif(
    not (os.environ.get("CAPSEM_LIVE_VM_ACCEPTANCE") or os.environ.get("CAPSEM_LIVE_SELF_CHECK")),
    reason="Set CAPSEM_LIVE_VM_ACCEPTANCE=1 or CAPSEM_LIVE_SELF_CHECK=1 with a running Capsem gateway/VM",
)
@pytest.mark.asyncio
async def test_live_vm_sandbox_acceptance() -> None:
    """One VM-backed acceptance test for the gate VM lane: init -> exec -> write/read_file -> cleanup."""
    envs = await CapsemSandboxEnvironment.sample_init(
        task_name="gate_vm_acceptance",
        config=CapsemSandboxConfig(
            template="code",
            cpu_count=2,
            ram_gb=2,
            working_dir="/workspace",
        ),
        metadata={},
    )
    try:
        sb = envs["default"]
        assert isinstance(sb, CapsemSandboxEnvironment)
        exec_res = await sb.exec(["echo", "INSPECT_CAPSEM_VM_ACCEPTANCE_OK"])
        assert exec_res.returncode == 0, exec_res.stderr
        assert exec_res.stdout.strip() == "INSPECT_CAPSEM_VM_ACCEPTANCE_OK"
        await sb.write_file("/workspace/roundtrip.txt", "hello-capsem\n")
        assert await sb.read_file("/workspace/roundtrip.txt") == "hello-capsem\n"
        payload = bytes(range(64)) + b"\x00CAPSEM_BIN\xff"
        await sb.write_file("/workspace/roundtrip.bin", payload)
        assert await sb.read_file("/workspace/roundtrip.bin", text=False) == payload
        print("INSPECT_CAPSEM_VM_ACCEPTANCE_OK")
    finally:
        await CapsemSandboxEnvironment.sample_cleanup(
            task_name="gate_vm_acceptance",
            config=None,
            environments=envs,
            interrupted=False,
        )
        await CapsemSandboxEnvironment.task_cleanup(
            task_name="gate_vm_acceptance",
            config=None,
            cleanup=True,
        )


@pytest.mark.skipif(
    not os.environ.get("CAPSEM_LIVE_SELF_CHECK"),
    reason="Set CAPSEM_LIVE_SELF_CHECK=1 with a running Capsem gateway/VM to run live self_check",
)
@pytest.mark.asyncio
async def test_live_capsem_vm_self_check() -> None:
    """Live conformance check against a real Capsem VM (vm mode)."""
    envs = await CapsemSandboxEnvironment.sample_init(
        task_name="live_self_check_vm",
        config=CapsemSandboxConfig(working_dir="/workspace"),
        metadata={},
    )
    try:
        sb = envs["default"]
        assert isinstance(sb, CapsemSandboxEnvironment)
        results = await _run_inspect_self_check(sb)
        failures = {k: v for k, v in results.items() if v is not True}
        print(f"live self_check vm: {len(results) - len(failures)}/{len(results)} passed")
        assert failures == {}, f"Live self_check (vm) failures: {failures}"
    finally:
        await CapsemSandboxEnvironment.sample_cleanup(
            task_name="live_self_check_vm",
            config=None,
            environments=envs,
            interrupted=False,
        )


@pytest.mark.skipif(
    not os.environ.get("CAPSEM_LIVE_SELF_CHECK"),
    reason="Set CAPSEM_LIVE_SELF_CHECK=1 with a running Capsem VM to run live container check",
)
@pytest.mark.asyncio
async def test_live_container_nonroot_and_numeric_user(tmp_path: Path) -> None:
    """Live container with non-root USER or numeric '1000:1000' succeeds on exec and write_file."""
    cfg = CapsemSandboxConfig(
        execution_mode="container",
        image="python:3.11-slim",
        user="developer",
        working_dir="/workspace",
    )
    envs = await CapsemSandboxEnvironment.sample_init(
        task_name="live_nonroot_write_file", config=cfg, metadata={}
    )
    try:
        sb = envs["default"]
        assert isinstance(sb, CapsemSandboxEnvironment)
        setup_cmd = (
            "(id -u developer >/dev/null 2>&1 || useradd -m -u 1000 -s /bin/bash developer) "
            "&& chown developer:developer /workspace"
        )
        setup_res = await sb.exec(["sh", "-c", setup_cmd], user="root")
        assert setup_res.returncode == 0, setup_res.stderr
        await sb.write_file("/workspace/staged.txt", "initial\n")
        stat_res = await sb.exec(["stat", "-c", "%u:%g", "/workspace/staged.txt"], user="root")
        assert stat_res.stdout.strip() == "1000:1000"
        env_res = await sb.exec(["sh", "-c", 'printf "%s:%s:%s" "$USER" "$LOGNAME" "$HOME"'])
        assert env_res.stdout == "developer:developer:/home/developer"
    finally:
        await CapsemSandboxEnvironment.sample_cleanup(
            task_name="live_nonroot_write_file", config=None, environments=envs, interrupted=False
        )

    compose = tmp_path / "compose.yaml"
    compose.write_text(
        "services:\n  default:\n    image: python:3.11-slim\n    user: '1000:1000'\n"
    )
    envs_num = await CapsemSandboxEnvironment.sample_init(
        task_name="live_numeric_user", config=str(compose), metadata={}
    )
    try:
        sb_num = envs_num["default"]
        assert isinstance(sb_num, CapsemSandboxEnvironment)
        assert (await sb_num.exec(["id", "-u"])).stdout.strip() == "1000"
        assert (await sb_num.exec(["id", "-g"])).stdout.strip() == "1000"
        bad_res = await sb_num.exec(["id", "-u"], user="nonexistent")
        assert bad_res.returncode != 0 and "capsem: unknown user nonexistent" in bad_res.stderr
    finally:
        await CapsemSandboxEnvironment.sample_cleanup(
            task_name="live_numeric_user", config=None, environments=envs_num, interrupted=False
        )
