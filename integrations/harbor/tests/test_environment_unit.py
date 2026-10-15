"""Hermetic unit tests for `CapsemEnvironment` and `harbor_capsem._transfer`."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths
from harbor_capsem import CapsemEnvironment
from harbor_capsem import _transfer as _xfer
from inspect_capsem._controller import CommandResult, _managed_vm_labels


class FakeCapsemController:
    """Host-backed fake controller executing guest commands in a local temp directory."""

    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir
        self.started_vms: list[dict[str, Any]] = []
        self.stopped_vms: list[str] = []
        self.commands: list[tuple[str, int]] = []
        self.uploads: dict[str, bytes] = {}
        self.files: dict[str, bytes] = {}
        self.closed = False

    async def start_vm(
        self, *, cpu_count: int, ram_gb: int, image: str | None = None, **kwargs: Any
    ) -> str:
        vm_id = f"vm-harbor-{len(self.started_vms)}"
        merged_labels = _managed_vm_labels(kwargs.get("labels"))
        cmd = kwargs.get("command")
        env = kwargs.get("env")
        self.started_vms.append(
            {
                "vm_id": vm_id,
                "cpu_count": cpu_count,
                "ram_gb": ram_gb,
                "image": image,
                "command": tuple(cmd) if cmd else None,
                "env": dict(env) if env else None,
                "labels": merged_labels,
                "registry_ca_pem": kwargs.get("registry_ca_pem"),
            }
        )
        return vm_id

    async def stop_vm(self, vm_id: str) -> None:
        self.stopped_vms.append(vm_id)

    async def list_vms(self) -> list[Any]:
        return []

    async def close(self) -> None:
        self.closed = True

    def _host_path(self, guest_path: str) -> Path:
        rel = guest_path.lstrip("/")
        return self.root_dir / rel

    async def upload_to_vm(self, vm_id: str, guest_path: str, data: bytes) -> None:
        del vm_id
        self.uploads[guest_path] = data
        self.files[guest_path] = data
        target = self._host_path(guest_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    async def download_from_vm(
        self, vm_id: str, guest_path: str, *, max_bytes: int | None = None
    ) -> bytes:
        del vm_id
        if guest_path in self.files:
            data = self.files[guest_path]
        else:
            data = self._host_path(guest_path).read_bytes()
        return data[:max_bytes] if max_bytes is not None else data

    async def exec_in_vm(self, vm_id: str, command: str, *, timeout: int = 120) -> CommandResult:
        del vm_id
        self.commands.append((command, timeout))
        if command == "pwd":
            return CommandResult(exit_code=0, stdout="/workspace\n", stderr="")
        if ".hb-upload-" in command or ".hb-download-" in command:
            rewritten = command.replace("/var/tmp/", f"{self.root_dir}/var/tmp/").replace(
                "/workspace", f"{self.root_dir}/workspace"
            )
            (self.root_dir / "var/tmp").mkdir(parents=True, exist_ok=True)
            proc = subprocess.run(
                ["bash", "-c", rewritten], capture_output=True, text=True, check=False
            )
            return CommandResult(exit_code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)
        if command.startswith("mkdir -p "):
            return CommandResult(exit_code=0, stdout="", stderr="")
        return CommandResult(exit_code=0, stdout="exec-ok\n", stderr="")


def _make_trial_paths(tmp_path: Path) -> TrialPaths:
    trial_dir = tmp_path / "trial"
    trial_dir.mkdir(parents=True, exist_ok=True)
    return TrialPaths(trial_dir=trial_dir)


def test_capabilities_and_resource_contract(tmp_path: Path) -> None:
    env_dir = tmp_path / "env"
    env_dir.mkdir()
    ctrl = FakeCapsemController(tmp_path / "guest")
    env = CapsemEnvironment(
        environment_dir=env_dir,
        environment_name="hello-world",
        session_id="hello-world__trial1__env",
        trial_paths=_make_trial_paths(tmp_path),
        task_env_config=EnvironmentConfig(docker_image="ubuntu:24.04"),
        controller=ctrl,
    )
    assert env.type() == "capsem"
    assert env.execution_mode == "container"
    caps = env.capabilities
    assert caps.disable_internet is True
    assert caps.gpus is False
    assert caps.docker_compose is False
    assert caps.mounted is False
    rcaps = CapsemEnvironment.resource_capabilities()
    assert rcaps.cpu_limit is True
    assert rcaps.cpu_request is True
    assert rcaps.memory_limit is True
    assert rcaps.memory_request is True
    assert _xfer._OCI_STAGE_DIR == "/workspace"
    assert _xfer._DIRECT_REL_RE.match("sub/file_1.txt")
    assert not _xfer._DIRECT_REL_RE.match("sub/a:b.txt")
    assert _xfer._rel_to_stage_dir("/workspace/../etc/passwd", "/workspace") is None
    assert _xfer._resolve_guest_path("rel/a.txt", "/workspace") == "/workspace/rel/a.txt"


async def test_container_and_vm_modes_lifecycle_and_exec_clamping(tmp_path: Path) -> None:
    env_dir = tmp_path / "env"
    env_dir.mkdir()
    ctrl = FakeCapsemController(tmp_path / "guest")
    env = CapsemEnvironment(
        environment_dir=env_dir,
        environment_name="task-a",
        session_id="task-a__1__env",
        trial_paths=_make_trial_paths(tmp_path),
        task_env_config=EnvironmentConfig(
            docker_image="debian:bookworm-slim",
            cpus=2,
            memory_mb=4096,
            workdir="/app",
            env={"TASK_VAR": "from-task"},
        ),
        controller=ctrl,
    )
    await env.start(force_build=False)
    assert env.vm_id == "vm-harbor-0"
    started = ctrl.started_vms[0]
    assert started["cpu_count"] == 2
    assert started["ram_gb"] == 4
    assert started["image"] == "debian:bookworm-slim"
    assert started["labels"]["harbor-session-id"] == "task-a__1__env"
    assert started["labels"]["harbor-env"] == "task-a"
    assert "capsem-vm-prefix" in started["labels"]
    assert started["env"]["TASK_VAR"] == "from-task"
    assert started["env"]["SSL_CERT_FILE"] == "/etc/ssl/certs/ca-certificates.crt"

    res = await env.exec("echo hi", env={"EXTRA": "1"}, timeout_sec=9999, user="root")
    assert res.return_code == 0
    assert res.stdout == "exec-ok\n"
    last_cmd, last_timeout = ctrl.commands[-1]
    assert last_timeout == 3600
    assert "cd /app" in last_cmd
    assert "EXTRA=1" in last_cmd
    assert "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt" in last_cmd

    await env.stop(delete=False)
    assert ctrl.stopped_vms == []
    await env.stop(delete=True)
    assert ctrl.stopped_vms == ["vm-harbor-0"]

    vm_env_dir = tmp_path / "vm_env"
    vm_env_dir.mkdir()
    vm_env = CapsemEnvironment(
        vm_env_dir,
        "vm-task",
        "vm-task__1__env",
        _make_trial_paths(tmp_path),
        EnvironmentConfig(),
        execution_mode="vm",
        controller=ctrl,
    )
    await vm_env.start(force_build=False)
    assert ctrl.started_vms[1]["image"] is None
    await vm_env.stop(delete=True)
    assert ctrl.stopped_vms == ["vm-harbor-0", "vm-harbor-1"]


async def test_single_service_compose_and_multi_service_rejection(tmp_path: Path) -> None:
    single_dir = tmp_path / "single"
    single_dir.mkdir()
    (single_dir / "docker-compose.yaml").write_text(
        "services:\n"
        "  main:\n"
        "    image: python:3.12-slim\n"
        "    working_dir: /work\n"
        "    user: '1000:1000'\n"
        "    environment:\n"
        "      COMPOSE_FOO: bar\n"
        "    command: ['sleep', 'infinity']\n",
        encoding="utf-8",
    )
    ctrl = FakeCapsemController(tmp_path / "guest")
    env = CapsemEnvironment(
        environment_dir=single_dir,
        environment_name="compose-ok",
        session_id="compose-ok__1__env",
        trial_paths=_make_trial_paths(tmp_path),
        task_env_config=EnvironmentConfig(),
        controller=ctrl,
    )
    await env.start(force_build=False)
    started = ctrl.started_vms[0]
    assert started["image"] == "python:3.12-slim"
    assert started["command"] == ("sleep", "infinity")
    assert started["env"]["COMPOSE_FOO"] == "bar"
    await env.exec("id")
    assert "1000:1000" in ctrl.commands[-1][0]
    await env.stop(delete=True)

    multi_dir = tmp_path / "multi"
    multi_dir.mkdir()
    (multi_dir / "docker-compose.yaml").write_text(
        "services:\n  main:\n    image: alpine:3.19\n  db:\n    image: redis:7\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Multi-service Compose files are not supported"):
        CapsemEnvironment(
            environment_dir=multi_dir,
            environment_name="compose-multi",
            session_id="compose-multi__1__env",
            trial_paths=_make_trial_paths(tmp_path),
            task_env_config=EnvironmentConfig(),
            controller=ctrl,
        )


async def test_dockerfile_build_and_file_dir_tar_transfers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    df_dir = tmp_path / "df_task"
    df_dir.mkdir()
    df_text = "FROM busybox:latest\nWORKDIR /workspace\n"
    (df_dir / "Dockerfile").write_text(df_text, encoding="utf-8")

    async def _fake_build(spec: Mapping[str, Any]) -> tuple[str, str | None]:
        assert Path(spec["dockerfile"]).name == "Dockerfile"
        return "127.0.0.1:5055/capsem-inspect-build:test", "-----BEGIN CERTIFICATE-----\n"

    monkeypatch.setattr("harbor_capsem.environment.build_and_stage_oci_image", _fake_build)
    ctrl = FakeCapsemController(tmp_path / "guest")
    env = CapsemEnvironment(
        environment_dir=df_dir,
        environment_name="df-task",
        session_id="df-task__1__env",
        trial_paths=_make_trial_paths(tmp_path),
        task_env_config=EnvironmentConfig(),
        controller=ctrl,
    )
    await env.start(force_build=True)
    assert ctrl.started_vms[0]["image"] == "127.0.0.1:5055/capsem-inspect-build:test"
    assert ctrl.started_vms[0]["registry_ca_pem"] == "-----BEGIN CERTIFICATE-----\n"

    src_file = tmp_path / "solve.sh"
    src_file.write_text("#!/bin/sh\necho solved\n", encoding="utf-8")
    os.chmod(src_file, 0o755)
    await env.upload_file(src_file, "/workspace/solve.sh")
    assert ctrl.uploads["/workspace/solve.sh"] == b"#!/bin/sh\necho solved\n"
    assert any("chmod 755 /workspace/solve.sh" in cmd for cmd, _ in ctrl.commands)

    dl_file = tmp_path / "downloaded_solve.sh"
    await env.download_file("/workspace/solve.sh", dl_file)
    assert dl_file.read_text(encoding="utf-8") == "#!/bin/sh\necho solved\n"

    src_tree = tmp_path / "src_tree"
    (src_tree / "nested").mkdir(parents=True)
    (src_tree / "nested" / "hello.txt").write_text("tar-fidelity-ok\n", encoding="utf-8")
    (src_tree / "link.txt").symlink_to("nested/hello.txt")
    await env.upload_dir(src_tree, "/workspace/uploaded_tree")

    dst_tree = tmp_path / "dst_tree"
    await env.download_dir("/workspace/uploaded_tree", dst_tree)
    assert (dst_tree / "nested" / "hello.txt").read_text(encoding="utf-8") == "tar-fidelity-ok\n"
    assert (dst_tree / "link.txt").is_symlink()
    await env.stop(delete=True)
