"""Package imports, entry-point registration, and `CapsemSandboxConfig` tests."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, cast

import pytest
from inspect_ai.util._sandbox.registry import registry_find_sandboxenv
from inspect_capsem import (
    CapsemSandboxConfig,
    CapsemSandboxEnvironment,
    SdkCapsemController,
)
from inspect_capsem.config import coerce_config
from pydantic import ValidationError


def test_standalone_package_imports_and_registration() -> None:
    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[3] / "integrations/inspect-ai/pyproject.toml").read_text(
            encoding="utf-8"
        )
    )
    capsem_dep = next(d for d in pyproject["project"]["dependencies"] if d.startswith("capsem>="))
    floor = tuple(int(part) for part in capsem_dep.removeprefix("capsem>=").split("."))
    assert floor >= (0, 7, 0)
    sandbox_cls = registry_find_sandboxenv("capsem")
    assert sandbox_cls is CapsemSandboxEnvironment
    default_cfg = CapsemSandboxConfig()
    assert default_cfg.template == "code"
    assert CapsemSandboxEnvironment.config_deserialize(default_cfg.model_dump(mode="json")) == (
        default_cfg
    )
    cfg = CapsemSandboxConfig(
        template="default",
        cpu_count=2,
        ram_gb=4,
        working_dir="/tmp",
        environment={"FOO": "bar"},
        user="developer",
    )
    assert cfg.template == "default"
    assert hash(default_cfg) != hash(cfg)
    restored = CapsemSandboxEnvironment.config_deserialize(cfg.model_dump(mode="json"))
    assert isinstance(restored, CapsemSandboxConfig)
    assert restored == cfg
    assert SdkCapsemController is not None


def test_coerce_config_and_unsupported_knobs() -> None:
    assert coerce_config(None) == CapsemSandboxConfig()
    cfg = CapsemSandboxConfig(template="default")
    assert coerce_config(cfg) is cfg
    assert coerce_config({"template": "default", "cpu_count": 2}) == CapsemSandboxConfig(
        template="default", cpu_count=2
    )
    with pytest.raises(NotImplementedError, match="allow_domains"):
        coerce_config({"allow_domains": ["pypi.org"]})
    with pytest.raises(NotImplementedError, match="host_workspace_dir"):
        coerce_config({"host_workspace_dir": "/tmp/ws"})
    with pytest.raises(ValidationError):
        coerce_config({"pool_endpoint": "http://127.0.0.1:9999"})
    with pytest.raises(TypeError, match="Unsupported Capsem sandbox config type"):
        coerce_config(cast(Any, object()))


def test_user_spec_quoting_and_mixed_groups() -> None:
    from inspect_capsem._controller import is_root_user_spec
    from inspect_capsem._exec import _format_exec_command

    assert is_root_user_spec("0:0") and is_root_user_spec("root:root")
    assert not is_root_user_spec("0:1000") and not is_root_user_spec("root:nogroup")
    inj_cmd, _ = _format_exec_command(
        "id", effective_cwd="/", env=None, user='$(id); "pwn"', timeout=None
    )
    assert "echo 'capsem: unknown user $(id); \"pwn\"' >&2; exit 1;" in inj_cmd
    cmd_0_1000, _ = _format_exec_command(
        "id -g", effective_cwd="/", env=None, user="0:1000", timeout=None
    )
    assert "setpriv --reuid=0 --regid=1000 --clear-groups /bin/bash -c" in cmd_0_1000
    for spec, expected in (
        ("1000:users", ("_uid=1000;", "getent group users")),
        ("alice:1000", ("_uid=$(id -u alice 2>/dev/null)", "_gid=1000;")),
        ("alice:users", ("_uid=$(id -u alice 2>/dev/null)", "getent group users")),
    ):
        cmd, _ = _format_exec_command("id", effective_cwd="/", env=None, user=spec, timeout=None)
        assert all(part in cmd for part in expected)
        assert 'setpriv --reuid="$_uid" --regid="$_gid" --clear-groups /bin/bash -c' in cmd
