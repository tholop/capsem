"""Inspect-facing `CapsemSandboxConfig` coercion and Compose file resolution."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from inspect_ai.util import SandboxEnvironmentConfigType
from pydantic import BaseModel

from .config import CapsemSandboxConfig


def _is_dockerfile_string(s: str) -> bool:
    name = Path(s).name.lower()
    return (
        name in ("dockerfile", "containerfile")
        or name.startswith(("dockerfile.", "containerfile."))
        or name.endswith((".dockerfile", ".containerfile"))
    )


def _validate_direct_volumes(cfg: CapsemSandboxConfig) -> CapsemSandboxConfig:
    if cfg.volumes:
        from .containers.compose_fields import _normalize_volumes

        _normalize_volumes(cfg.volumes, None, cfg.allowed_host_paths)
    return cfg


def resolve_compose_file(
    cfg: CapsemSandboxConfig, *, sample_metadata: Mapping[str, Any] | None = None
) -> CapsemSandboxConfig:
    """Resolve `cfg.compose_file` and return an updated `CapsemSandboxConfig`."""
    if not cfg.compose_file or not cfg.compose_file.strip():
        return _validate_direct_volumes(cfg)
    from .containers.compose import extract_compose_fields, parse_compose_yaml_file

    compose_path = Path(cfg.compose_file)
    if not compose_path.is_file():
        raise FileNotFoundError(f"Compose file not found: {compose_path}")
    parsed = parse_compose_yaml_file(
        compose_path, allowed_host_env=cfg.allowed_host_env, sample_metadata=sample_metadata
    )
    overrides = extract_compose_fields(
        parsed,
        base_dir=compose_path.parent,
        allowed_host_env=cfg.allowed_host_env,
        allowed_host_paths=cfg.allowed_host_paths,
        sample_metadata=sample_metadata,
    )
    explicit = set(cfg.model_fields_set)
    data = {**cfg.model_dump(), **{k: v for k, v in overrides.items() if k not in explicit}}
    resolved = CapsemSandboxConfig.model_construct(
        _fields_set=explicit, **CapsemSandboxConfig(**data).__dict__
    )
    resolved._compose_working_dir = "working_dir" in overrides
    return resolved


def coerce_config(
    config: SandboxEnvironmentConfigType | Mapping[str, Any] | None,
    *,
    resolve_compose: bool = True,
    sample_metadata: Mapping[str, Any] | None = None,
) -> CapsemSandboxConfig:
    """Coerce an Inspect sandbox config argument into a `CapsemSandboxConfig`."""
    if config is None:
        return CapsemSandboxConfig()
    if isinstance(config, (CapsemSandboxConfig, dict)):
        cfg = config if isinstance(config, CapsemSandboxConfig) else CapsemSandboxConfig(**config)
        return (
            resolve_compose_file(cfg, sample_metadata=sample_metadata)
            if (resolve_compose and cfg.compose_file)
            else _validate_direct_volumes(cfg)
        )
    if isinstance(config, str):
        s = config.strip()
        if s.lower().endswith((".yaml", ".yml")):
            cfg = CapsemSandboxConfig(execution_mode="container", compose_file=s)
            return (
                resolve_compose_file(cfg, sample_metadata=sample_metadata)
                if resolve_compose
                else cfg
            )
        if _is_dockerfile_string(s):
            raise ValueError(
                f"Dockerfile / Containerfile builds ({s!r}) are not supported in Capsem "
                "OCI-workload mode; specify a pre-built OCI image reference instead."
            )
        return CapsemSandboxConfig(execution_mode="container", image=s)
    if isinstance(config, BaseModel) and hasattr(config, "services"):
        from .containers.compose import extract_compose_fields

        dumped = config.model_dump(exclude_none=True)
        if isinstance(dumped.get("services"), Mapping):
            overrides = extract_compose_fields(
                dumped, base_dir=None, sample_metadata=sample_metadata
            )
            cfg = CapsemSandboxConfig(**overrides)
            cfg._compose_working_dir = "working_dir" in overrides
            return cfg
    raise TypeError(f"Unsupported Capsem sandbox config type: {type(config)!r}")
