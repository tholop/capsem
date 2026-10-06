"""Inspect-free Compose field extraction and fail-fast feature validation."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .compose_fields import (
    _combine_entrypoint_and_command,
    _normalize_command_field,
    _normalize_environment,
    _normalize_healthcheck,
    _normalize_volumes,
    parse_cpus_to_cpu_count,
    parse_memory_to_ram_gb,
)
from .compose_yaml import parse_compose_yaml_file

logger = logging.getLogger(__name__)
_UNSUPPORTED_KEYS_RAW = (
    "cap_add cap_drop devices security_opt sysctls pid ipc uts cgroup cgroup_parent "
    "userns_mode ports"
)
_SILENT_KEYS_RAW = (
    "image working_dir environment command entrypoint volumes expose init mem_limit cpus deploy "
    "network_mode networks user healthcheck restart tty stdin_open container_name hostname labels "
    "logging platform pull_policy stop_grace_period stop_signal"
)
_REJECTED_SERVICE_KEYS: dict[str, str] = {
    **{
        k: f"Compose {k!r} is not supported in Capsem OCI-workload mode."
        for k in _UNSUPPORTED_KEYS_RAW.split()
    },
    "build": (
        "Compose 'build' / Dockerfile builds are not supported in Capsem OCI-workload mode; "
        "specify a pre-built 'image' reference instead."
    ),
    "dockerfile": (
        "Compose 'dockerfile' builds are not supported in Capsem OCI-workload mode; "
        "specify a pre-built 'image' reference instead."
    ),
    "privileged": (
        "Compose 'privileged' is not supported in Capsem OCI-workload mode; "
        "Capsem isolates workloads via micro-VM hardware virtualization."
    ),
    "env_file": (
        "Compose 'env_file' is not supported by Capsem; inline variables under "
        "'environment' or place them in a project '.env' file."
    ),
    "depends_on": "Compose 'depends_on' is not supported by Capsem (single-service sandbox only).",
}
_SILENT_SERVICE_KEYS = frozenset(_SILENT_KEYS_RAW.split())


def _check_k8s_allow_domains(mapping: Mapping[str, Any]) -> None:
    k8s_ext = mapping.get("x-inspect_k8s_sandbox")
    if isinstance(k8s_ext, Mapping) and k8s_ext.get("allow_domains") is not None:
        raise NotImplementedError(
            "x-inspect_k8s_sandbox.allow_domains is not supported by Capsem; "
            "configure network domain policy in the Capsem VM profile instead."
        )


def _select_primary_service(services: Mapping[str, Any]) -> Mapping[str, Any]:
    if len(services) > 1:
        names = ", ".join(str(k) for k in services)
        raise ValueError(
            f"Multi-service Compose files are not supported by Capsem "
            f"(found {len(services)} services: {names})."
        )
    svc = next(iter(services.values()), {})
    return svc if isinstance(svc, Mapping) else {}


def _is_internal_only_service_networks(svc_networks: Any, top_networks: Mapping[str, Any]) -> bool:
    if isinstance(svc_networks, (list, tuple)):
        names = [str(n) for n in svc_networks if n]
        return bool(names) and all(
            isinstance(top_networks.get(n), Mapping) and bool(top_networks[n].get("internal"))
            for n in names
        )
    if isinstance(svc_networks, Mapping) and svc_networks:
        return all(
            (isinstance(cfg, Mapping) and cfg.get("internal") is True)
            or (
                isinstance(top_networks.get(str(n)), Mapping)
                and top_networks[str(n)].get("internal") is True
            )
            for n, cfg in svc_networks.items()
        )
    return False


def _validate_service_keys(svc: Mapping[str, Any]) -> None:
    for key, msg in _REJECTED_SERVICE_KEYS.items():
        if svc.get(key) not in (None, False, [], (), {}):
            raise ValueError(msg)
    unknown = sorted(
        str(k)
        for k in svc
        if str(k) not in _SILENT_SERVICE_KEYS
        and str(k) not in _REJECTED_SERVICE_KEYS
        and not str(k).startswith("x-")
    )
    if unknown:
        logger.warning(
            "Compose service specifies unsupported key(s) ignored by Capsem: %s",
            ", ".join(unknown),
        )


def _apply_resource_and_network_fields(
    svc: Mapping[str, Any], top_networks: Any, out: dict[str, Any]
) -> None:
    d: Any = svc
    for k in ("deploy", "resources", "limits"):
        d = d.get(k) if isinstance(d, Mapping) else None
    limits = d if isinstance(d, Mapping) else {}
    mem_limit = svc.get("mem_limit") if svc.get("mem_limit") is not None else limits.get("memory")
    if mem_limit is not None:
        out["mem_limit"] = str(mem_limit)
        out["ram_gb"] = parse_memory_to_ram_gb(mem_limit)
    cpus_raw = svc.get("cpus") if svc.get("cpus") is not None else limits.get("cpus")
    if cpus_raw is not None:
        out["cpu_count"] = parse_cpus_to_cpu_count(cpus_raw)

    net_mode = svc.get("network_mode")
    if isinstance(net_mode, str) and (cleaned_mode := net_mode.strip()):
        if cleaned_mode not in ("bridge", "default"):
            raise ValueError(
                f"Compose network_mode {cleaned_mode!r} is not supported by inspect-capsem: "
                "per-VM network mode / air-gapped egress is not yet wired in the Capsem 0.7 "
                "ProvisionRequest contract."
            )
    elif isinstance(top_networks, Mapping):
        svc_networks = svc.get("networks")
        default_net = top_networks.get("default")
        is_internal = (
            _is_internal_only_service_networks(svc_networks, top_networks)
            if svc_networks is not None
            else (isinstance(default_net, Mapping) and default_net.get("internal") is True)
        )
        if is_internal:
            raise ValueError(
                "Compose 'internal: true' networks are not supported by inspect-capsem: "
                "per-VM air-gapped egress is not yet wired in the Capsem 0.7 ProvisionRequest "
                "contract."
            )


def extract_compose_fields(
    parsed: Mapping[str, Any],
    base_dir: Path | None = None,
    *,
    allowed_host_env: Sequence[str] = (),
    allowed_host_paths: Sequence[str] = (),
    sample_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract primary service fields from a parsed Compose dictionary."""
    _check_k8s_allow_domains(parsed)
    services = parsed.get("services")
    if not isinstance(services, Mapping) or not services:
        raise ValueError("Compose file must contain a non-empty 'services' mapping")
    svc = _select_primary_service(services)
    _check_k8s_allow_domains(svc)
    _validate_service_keys(svc)

    out: dict[str, Any] = {"execution_mode": "container"}
    for field in ("image", "working_dir"):
        val = svc.get(field)
        if isinstance(val, str) and val.strip():
            out[field] = val.strip()
    if env := _normalize_environment(
        svc.get("environment"),
        base_dir,
        allowed_host_env=allowed_host_env,
        sample_metadata=sample_metadata,
    ):
        out["environment"] = env
    command = _combine_entrypoint_and_command(
        _normalize_command_field(svc.get("entrypoint")),
        _normalize_command_field(svc.get("command")),
    )
    if command is not None:
        out["command"] = command
    if volumes := _normalize_volumes(svc.get("volumes"), base_dir, allowed_host_paths):
        out["volumes"] = volumes
    _apply_resource_and_network_fields(svc, parsed.get("networks"), out)
    if (user := svc.get("user")) is not None and str(user).strip():
        out["user"] = str(user).strip()
    if (hc := _normalize_healthcheck(svc.get("healthcheck"))) is not None:
        out["healthcheck"] = hc
    return out


__all__ = ["extract_compose_fields", "parse_compose_yaml_file"]
