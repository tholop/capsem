"""Live Capsem VM conformance checks (`CAPSEM_LIVE_SELF_CHECK=1`)."""

from __future__ import annotations

import os

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
