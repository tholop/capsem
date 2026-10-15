"""Hermetic unit tests for `harbor_capsem._credentials` and `CapsemEnvironment.exec()` brokering."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths
from harbor_capsem import CapsemEnvironment
from harbor_capsem._credentials import (
    broker_env_credentials,
    coerce_bool_flag,
    should_broker_credential,
)
from inspect_capsem._controller import CommandResult, _managed_vm_labels


@dataclass(frozen=True)
class _FakeInjectResponse:
    credential_ref: str


class _FakeCredentialsClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def inject(
        self, provider: str, value: str, *, storage: str = "file"
    ) -> _FakeInjectResponse:
        self.calls.append((provider, value, storage))
        digest = f"{len(self.calls):064x}"
        return _FakeInjectResponse(credential_ref=f"credential:blake3:{digest}")


class _FakeHypervisor:
    def __init__(self) -> None:
        self.credentials = _FakeCredentialsClient()


class _FakeBrokerController:
    def __init__(self) -> None:
        self._hypervisor = _FakeHypervisor()
        self.commands: list[tuple[str, int]] = []

    async def start_vm(
        self, *, cpu_count: int, ram_gb: int, image: str | None = None, **kwargs: Any
    ) -> str:
        del cpu_count, ram_gb, image
        _managed_vm_labels(kwargs.get("labels"))
        return "vm-cred-0"

    async def stop_vm(self, vm_id: str) -> None:
        del vm_id

    async def list_vms(self) -> list[Any]:
        return []

    async def close(self) -> None:
        pass

    async def upload_to_vm(self, vm_id: str, guest_path: str, data: bytes) -> None:
        del vm_id, guest_path, data

    async def download_from_vm(
        self, vm_id: str, guest_path: str, *, max_bytes: int | None = None
    ) -> bytes:
        del vm_id, guest_path, max_bytes
        return b""

    async def exec_in_vm(self, vm_id: str, command: str, *, timeout: int = 120) -> CommandResult:
        del vm_id
        self.commands.append((command, timeout))
        if command == "pwd":
            return CommandResult(exit_code=0, stdout="/workspace\n", stderr="")
        return CommandResult(exit_code=0, stdout="ok\n", stderr="")


def _make_trial_paths(tmp_path: Path) -> TrialPaths:
    trial_dir = tmp_path / "trial"
    trial_dir.mkdir(parents=True, exist_ok=True)
    return TrialPaths(trial_dir=trial_dir)


def test_coerce_bool_and_should_broker_rules() -> None:
    assert coerce_bool_flag(None, default=True) is True
    assert coerce_bool_flag("false", default=True) is False
    assert coerce_bool_flag("TRUE", default=False) is True
    assert coerce_bool_flag(1, default=False) is True
    with pytest.raises(ValueError, match="Invalid boolean option"):
        coerce_bool_flag("maybe", default=False)

    already_ref = f"credential:blake3:{'a' * 64}"
    assert should_broker_credential("openai", already_ref, {}) is False
    assert should_broker_credential("openai", "   ", {}) is False
    assert should_broker_credential("openai", "local-proxy", {}) is True
    assert (
        should_broker_credential(
            "openai",
            "sk-live-secret",
            {"OPENAI_BASE_URL": "http://127.0.0.1:11434/v1"},
            ("OPENAI_BASE_URL",),
        )
        is True
    )
    assert (
        should_broker_credential(
            "openai",
            "sk-live-secret",
            {"OPENAI_BASE_URL": "https://api.openai.com/v1"},
            ("OPENAI_BASE_URL",),
        )
        is True
    )


async def test_capsem_environment_exec_brokers_and_caches_credentials(tmp_path: Path) -> None:
    env_dir = tmp_path / "env"
    env_dir.mkdir()
    ctrl = _FakeBrokerController()
    env = CapsemEnvironment(
        environment_dir=env_dir,
        environment_name="cred-task",
        session_id="cred-task__1",
        trial_paths=_make_trial_paths(tmp_path),
        task_env_config=EnvironmentConfig(docker_image="ubuntu:24.04"),
        controller=ctrl,
    )
    await env.start(force_build=False)

    already_ref = f"credential:blake3:{'f' * 64}"
    await env.exec(
        "claude --version",
        env={
            "ANTHROPIC_API_KEY": "sk-ant-real-key",
            "OPENAI_API_KEY": already_ref,
            "GEMINI_API_KEY": "AIzaRealGeminiKey",
        },
    )
    calls = ctrl._hypervisor.credentials.calls
    assert calls == [
        ("anthropic", "sk-ant-real-key", "memory"),
        ("google", "AIzaRealGeminiKey", "memory"),
    ]
    cmd_str = ctrl.commands[-1][0]
    assert "sk-ant-real-key" not in cmd_str
    assert "AIzaRealGeminiKey" not in cmd_str
    assert f"ANTHROPIC_API_KEY=credential:blake3:{1:064x}" in cmd_str
    assert f"GEMINI_API_KEY=credential:blake3:{2:064x}" in cmd_str
    assert f"OPENAI_API_KEY={already_ref}" in cmd_str
    assert "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt" in cmd_str

    # Second exec with same key hits the per-environment cache without re-injecting.
    await env.exec("claude -p hi", env={"ANTHROPIC_API_KEY": "sk-ant-real-key"})
    assert len(ctrl._hypervisor.credentials.calls) == 2
    await env.stop(delete=True)


async def test_broker_credentials_opt_out_and_error_paths(tmp_path: Path) -> None:
    env_dir = tmp_path / "env"
    env_dir.mkdir()
    ctrl = _FakeBrokerController()
    env = CapsemEnvironment(
        environment_dir=env_dir,
        environment_name="opt-out",
        session_id="opt-out__1",
        trial_paths=_make_trial_paths(tmp_path),
        task_env_config=EnvironmentConfig(docker_image="ubuntu:24.04"),
        broker_credentials="false",
        controller=ctrl,
    )
    await env.start(force_build=False)
    await env.exec("env", env={"ANTHROPIC_API_KEY": "sk-ant-raw"})
    assert ctrl._hypervisor.credentials.calls == []
    assert "ANTHROPIC_API_KEY=sk-ant-raw" in ctrl.commands[-1][0]
    await env.stop(delete=True)

    with pytest.raises(RuntimeError, match=r"does not expose hypervisor\.credentials\.inject"):
        await broker_env_credentials(object(), {"OPENAI_API_KEY": "sk-test"}, enabled=True)


async def test_environment_error_paths_output_callbacks_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from capsem.trace import load_db_tables as _load_db_tables
    from capsem.trace import parse_iso_datetime as _parse_ts
    from harbor_capsem._trace import _validate_and_write

    assert _parse_ts(None) is None
    assert _parse_ts("invalid-iso-date") is None
    with pytest.raises(ValueError, match="ATIF trajectory failed validation"):
        _validate_and_write({})
    empty_db = tmp_path / "empty.db"
    import sqlite3

    sqlite3.connect(empty_db).close()
    assert _load_db_tables(empty_db, ("security_rule_events",)) == {"security_rule_events": []}
    with pytest.raises(sqlite3.OperationalError, match="missing required ledger table"):
        _load_db_tables(empty_db, ("model_calls",))

    env_dir = tmp_path / "err_env"
    env_dir.mkdir()
    ctrl = _FakeBrokerController()
    bad_mode: Any = "invalid"
    with pytest.raises(ValueError, match="Invalid execution_mode"):
        CapsemEnvironment(
            environment_dir=env_dir,
            environment_name="bad-mode",
            session_id="bad-mode__1",
            trial_paths=_make_trial_paths(tmp_path),
            task_env_config=EnvironmentConfig(docker_image="ubuntu:24.04"),
            execution_mode=bad_mode,
            controller=ctrl,
        )

    env = CapsemEnvironment(
        environment_dir=env_dir,
        environment_name="lifecycle-edge",
        session_id="lifecycle-edge__1",
        trial_paths=_make_trial_paths(tmp_path),
        task_env_config=EnvironmentConfig(docker_image="ubuntu:24.04"),
        controller=ctrl,
    )
    await env.stop(delete=True)
    with pytest.raises(RuntimeError, match="CapsemEnvironment is not started"):
        await env.exec("pwd")

    await env.start(force_build=False)
    chunks: list[tuple[str, str]] = []

    async def _cb(text: str, stream: str) -> None:
        chunks.append((text, stream))

    monkeypatch.setattr(env, "_output_callback", lambda: _cb)
    await env.exec("echo hi")
    assert chunks == [("ok\n", "stdout")]
    env._owns_controller = True
    await env.stop(delete=True)

    async def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("prepare failed")

    monkeypatch.setattr(
        "harbor_capsem.environment.containers.prepare_oci_workload_container", _boom
    )
    with pytest.raises(RuntimeError, match="prepare failed"):
        await env.start(force_build=False)
    assert env.vm_id == ""

    monkeypatch.setattr(
        "harbor_capsem.environment._controller.SdkCapsemController", _FakeBrokerController
    )

    async def _fake_leftover(_ctrl: Any) -> list[str]:
        return ["vm-leftover-1"]

    async def _fake_untracked(
        _cls: Any, _vm_id: str | None, stopped: set[str], _unreg: Any
    ) -> None:
        stopped.add("vm-untracked-2")

    monkeypatch.setattr("harbor_capsem.environment._lifecycle.sweep_leftover_vms", _fake_leftover)
    monkeypatch.setattr(
        "harbor_capsem.environment._cleanup._sweep_untracked_managed_vms", _fake_untracked
    )
    cleaned = await CapsemEnvironment.cleanup_untracked_vms()
    assert cleaned == ["vm-leftover-1", "vm-untracked-2"]
