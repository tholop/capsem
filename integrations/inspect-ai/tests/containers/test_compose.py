"""Compose field extraction, resource mapping, and fail-fast rejection tests."""

from __future__ import annotations

import logging
from pathlib import Path

import inspect_capsem.containers.compose as c_compose_mod
import inspect_capsem.containers.compose_fields as compose_fields_mod
import pytest
import yaml
from inspect_capsem._compose import coerce_config


def test_compose_field_extraction_and_operator_allowlists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extract = c_compose_mod.extract_compose_fields
    for bad in ({"services": {}}, {"execution_mode": "vm"}):
        with pytest.raises(ValueError, match="non-empty 'services' mapping"):
            extract(bad)
    with pytest.raises(ValueError, match="Multi-service Compose files are not supported"):
        extract({"services": {"a": {"image": "a"}, "b": {"image": "b"}}}, base_dir=tmp_path)

    ctx, outside = tmp_path / "ctx", tmp_path / "outside"
    ctx.mkdir()
    outside.mkdir()
    monkeypatch.setenv("INHERITED_VAR", "from_os")
    monkeypatch.setenv("CAPSEM_INSPECT_ALLOWED_HOST_ENV", "INHERITED_*")
    monkeypatch.setenv("CAPSEM_INSPECT_ALLOWED_HOST_PATHS", str(outside))
    rich = extract(
        {
            "services": {
                "app": {
                    "image": "ubuntu:24.04",
                    "environment": ["K=V", "INHERITED_VAR"],
                    "command": "echo hi",
                    "entrypoint": "/ep",
                    "volumes": ["./ctx:/mnt:ro", f"{outside}:/abs"],
                    "expose": [9000],
                    "init": True,
                    "deploy": {"resources": {"limits": {"memory": "256m", "cpus": "1.5"}}},
                    "network_mode": "bridge",
                    "user": "nobody",
                    "healthcheck": {"test": ("CMD", "true"), "retries": 5},
                }
            }
        },
        base_dir=tmp_path,
    )
    assert rich["environment"] == {"K": "V", "INHERITED_VAR": "from_os"}
    assert rich["command"] == ("/ep", "echo", "hi")
    assert rich["volumes"] == (f"{ctx.resolve()}:/mnt:ro", f"{outside.resolve()}:/abs")
    assert (rich["mem_limit"], rich["ram_gb"], rich["cpu_count"]) == ("256m", 1, 2)
    assert (rich["user"], rich["healthcheck"]) == (
        "nobody",
        {"test": ["CMD", "true"], "retries": 5},
    )


def test_compose_networks_and_resource_limits() -> None:
    nets = {"isolated": {"internal": True}, "ext": {"internal": False}}
    for internal_compose in (
        {"networks": nets, "services": {"a": {"image": "alpine:3.20", "networks": ["isolated"]}}},
        {
            "networks": nets,
            "services": {
                "a": {"image": "a", "networks": {"isolated": {}, "local": {"internal": True}}}
            },
        },
        {"networks": {"default": {"internal": True}}, "services": {"a": {"image": "alpine:3.20"}}},
    ):
        with pytest.raises(ValueError, match="internal: true"):
            c_compose_mod.extract_compose_fields(internal_compose)
    online = {
        "networks": nets,
        "services": {"o": {"image": "a:1", "networks": ["isolated", "ext"]}},
    }
    assert c_compose_mod.extract_compose_fields(online)["image"] == "a:1"
    assert compose_fields_mod.parse_memory_to_ram_gb("512m") == 1
    assert compose_fields_mod.parse_memory_to_ram_gb("2500MiB") == 3
    assert compose_fields_mod.parse_memory_to_ram_gb(4 * 1024**3) == 4
    with pytest.raises(ValueError, match="Invalid Compose memory limit"):
        compose_fields_mod.parse_memory_to_ram_gb("invalid")
    assert compose_fields_mod.parse_cpus_to_cpu_count("0.5") == 1
    assert compose_fields_mod.parse_cpus_to_cpu_count(2.25) == 3
    with pytest.raises(ValueError, match="Invalid Compose cpus limit"):
        compose_fields_mod.parse_cpus_to_cpu_count("0")


def test_compose_long_form_env_and_volumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bind_dir = tmp_path / "src"
    bind_dir.mkdir()
    monkeypatch.setenv("INHERITED_DICT_KEY", "inherited_val")
    monkeypatch.setenv("BLOCKED_DICT_KEY", "secret_val")
    monkeypatch.setenv("CAPSEM_INSPECT_ALLOWED_HOST_ENV", "INHERITED_DICT_KEY")
    monkeypatch.delenv("UNSET_DICT_KEY", raising=False)
    long_compose = {
        "services": {
            "web": {
                "image": "nginx:alpine",
                "environment": {
                    "INHERITED_DICT_KEY": None,
                    "BLOCKED_DICT_KEY": None,
                    "UNSET_DICT_KEY": None,
                    "BOOL_T": True,
                    "BOOL_F": False,
                },
                "volumes": [
                    {"type": "bind", "source": "./src", "target": "/app/src", "read_only": True}
                ],
            }
        }
    }
    with caplog.at_level(logging.WARNING):
        ext_long = c_compose_mod.extract_compose_fields(long_compose, base_dir=tmp_path)
    assert "Ignoring non-allowlisted host environment variable 'BLOCKED_DICT_KEY'" in caplog.text
    assert ext_long["environment"] == {
        "INHERITED_DICT_KEY": "inherited_val",
        "BLOCKED_DICT_KEY": "",
        "UNSET_DICT_KEY": "",
        "BOOL_T": "true",
        "BOOL_F": "false",
    }
    assert ext_long["volumes"] == (f"{bind_dir.resolve()}:/app/src:ro",)
    for bad_vols, match in (
        ([{"type": "tmpfs", "target": "/tmp"}], "Unsupported Compose volume type 'tmpfs'"),
        (["named-vol:/var/lib/data"], "Named or non-bind Compose volume"),
        ([{"type": "bind", "source": "./src"}], "requires both 'source' and 'target'"),
    ):
        with pytest.raises(ValueError, match=match):
            compose_fields_mod._normalize_volumes(bad_vols, tmp_path)


def test_unsupported_compose_features_rejected_at_coerce_config(tmp_path: Path) -> None:
    compose_path = tmp_path / "compose.yaml"
    cases: tuple[tuple[dict[str, object], str], ...] = (
        ({"build": "."}, "build"),
        ({"build": {"context": ".", "dockerfile": "Dockerfile"}}, "build"),
        ({"build": {"dockerfile_inline": "FROM alpine\n"}}, "build"),
        ({"dockerfile": "Dockerfile"}, "dockerfile"),
        ({"privileged": True}, "privileged"),
        *(
            ({k: ["x"]}, k)
            for k in (
                "cap_add",
                "cap_drop",
                "devices",
                "security_opt",
                "ports",
                "env_file",
                "depends_on",
            )
        ),
        *(({k: "host"}, k) for k in ("pid", "ipc", "uts", "cgroup")),
        ({"sysctls": {"net.ipv4.ip_forward": "1"}}, "sysctls"),
        *(
            ({"network_mode": m}, "network_mode")
            for m in ("none", "host", "service:db", "container:db")
        ),
    )
    for override, expected_match in cases:
        compose_path.write_text(
            yaml.safe_dump({"services": {"app": {"image": "alpine:3.20", **override}}})
        )
        with pytest.raises(ValueError, match=expected_match):
            coerce_config(str(compose_path))


def test_unknown_benign_compose_keys_warn_and_x_extensions_ignored(
    caplog: pytest.LogCaptureFixture,
) -> None:
    svc = {
        "image": "alpine:3.20",
        "shm_size": "64m",
        "tmpfs": ["/run"],
        "platform": "linux/amd64",
        "x-inspect": {"k": "v"},
    }
    with caplog.at_level(logging.WARNING):
        c_compose_mod.extract_compose_fields({"services": {"app": svc}})
    assert "shm_size, tmpfs" in caplog.text
    assert "platform" not in caplog.text and "x-inspect" not in caplog.text
