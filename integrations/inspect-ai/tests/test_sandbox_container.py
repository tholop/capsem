"""OCI container mode initialization, working_dir resolution, and exec tests."""

from __future__ import annotations

from pathlib import Path

import inspect_capsem._controller as ctrl_mod
import inspect_capsem._exec as exec_mod
import inspect_capsem._lifecycle as lc_mod
import inspect_capsem.sandbox as sb_mod
import pytest
from inspect_capsem import CapsemSandboxConfig, CapsemSandboxEnvironment

from .conftest import (
    LocalFakeCapsemController,
    Scripted,
    _host_timeout_skips,
    _run_inspect_self_check,
    ok,
)


def test_oci_create_command_and_image_normalization() -> None:
    assert ctrl_mod._normalize_image_ref(None) is None
    assert ctrl_mod._normalize_image_ref("  ") is None
    assert ctrl_mod._normalize_image_ref("python:3.12-slim") == "docker://python:3.12-slim"
    assert ctrl_mod._normalize_image_ref("docker://alpine:3.19") == "docker://alpine:3.19"
    assert ctrl_mod._normalize_image_ref("/tmp/rootfs.sqfs") == "/tmp/rootfs.sqfs"
    assert lc_mod._oci_create_command(CapsemSandboxConfig(image="alpine:3.19")) is None
    assert (
        lc_mod._oci_create_command(CapsemSandboxConfig(image="alpine:3.19", command="   ")) is None
    )
    assert lc_mod._oci_create_command(
        CapsemSandboxConfig(image="alpine:3.19", command="python3 -m http.server 8080")
    ) == ("python3", "-m", "http.server", "8080")
    assert lc_mod._oci_create_command(
        CapsemSandboxConfig(image="alpine:3.19", command=("sleep", "infinity"))
    ) == ("sleep", "infinity")


@pytest.mark.asyncio
async def test_container_mode_uses_oci_create_and_resolves_workdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Container mode passes image/command to start_vm and probes WORKDIR when omitted."""
    fake = Scripted([("pwd", ok("/app/src\n"))])
    monkeypatch.setattr(sb_mod, "SdkCapsemController", lambda: fake)
    compose_path = tmp_path / "compose.yaml"
    compose_path.write_text("services:\n  default:\n    image: swe-bench/eval:1.0\n")
    envs = await CapsemSandboxEnvironment.sample_init(
        task_name="oci_default_wd", config=str(compose_path), metadata={}
    )
    try:
        sb = envs["default"]
        assert isinstance(sb, CapsemSandboxEnvironment)
        assert (sb.container_id, sb.execution_mode) == ("workload", "container")
        assert fake.started[-1]["image"] == "swe-bench/eval:1.0"
        assert not any(c.startswith("docker ") for c in fake.commands)
        fake.commands.clear()
        await sb.exec(["pytest", "-q"])
        assert len(fake.commands) == 1 and fake.commands[0].startswith("bash -c ")
        assert "cd /app/src && pytest -q" in fake.commands[0]
        assert exec_mod._CONTAINER_HOME_FIX in fake.commands[0]
    finally:
        await CapsemSandboxEnvironment.sample_cleanup(
            task_name="oci_default_wd", config=None, environments=envs, interrupted=False
        )


@pytest.mark.asyncio
async def test_container_mode_passes_self_check_with_local_fake_controller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`execution_mode='container'` passes Inspect's `self_check` via `LocalFakeCapsemController`."""
    controller = LocalFakeCapsemController(tmp_path, skip_bake=False)
    monkeypatch.setattr(sb_mod, "SdkCapsemController", lambda: controller)
    guest_work = tmp_path / "container_work"
    guest_work.mkdir()
    cfg = CapsemSandboxConfig(
        execution_mode="container", image="ubuntu:24.04", working_dir=str(guest_work)
    )
    envs = await CapsemSandboxEnvironment.sample_init(
        task_name="self_check_container_mode", config=cfg, metadata={}
    )
    env = envs["default"]
    assert isinstance(env, CapsemSandboxEnvironment)
    assert (env.execution_mode, env.container_id) == ("container", "workload")
    try:
        skip = {
            "test_read_and_write_large_file_binary",
            "test_exec_input_large",
            "test_read_file_limit",
            "test_exec_as_user",
        } | _host_timeout_skips()
        results = await _run_inspect_self_check(env, skip=skip)
        failures = {k: v for k, v in results.items() if v is not True}
        assert failures == {}, f"Inspect container self_check failures: {failures}"
    finally:
        await CapsemSandboxEnvironment.sample_cleanup(
            task_name="self_check_container_mode", config=None, environments=envs, interrupted=False
        )
