"""Pydantic configuration model for `inspect-capsem` sandboxes."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, PrivateAttr, model_validator

if TYPE_CHECKING:
    from .containers.spec import ContainerSpec

_REJECTED_DOCKERFILE_KEYS = frozenset(
    {"dockerfile", "build", "build_context", "build_args", "build_target", "dockerfile_inline"}
)


def _json_hashable(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((str(k), _json_hashable(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_json_hashable(item) for item in value)
    return value


class CapsemSandboxConfig(BaseModel, frozen=True, extra="forbid"):
    """Configuration for the Capsem Inspect Sandbox Environment."""

    _compose_working_dir: bool = PrivateAttr(default=False)

    execution_mode: Literal["vm", "container"] = "vm"
    template: str = "code"
    image: str = "python:3.11-slim"
    cpu_count: int = 4
    ram_gb: int = 8
    working_dir: str = "/workspace"
    compose_file: str | None = None
    environment: dict[str, str] = Field(default_factory=dict)
    command: tuple[str, ...] | str | None = None
    volumes: tuple[str, ...] = ()
    healthcheck: dict[str, Any] | None = None
    mem_limit: str | None = None
    user: str | None = None
    allowed_host_env: tuple[str, ...] = ()
    allowed_host_paths: tuple[str, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _auto_select_container_mode(cls, data: Any) -> Any:
        if not isinstance(data, Mapping):
            return data
        if data.get("allow_domains") is not None:
            raise NotImplementedError(
                "CapsemSandboxConfig does not support per-VM allow_domains; "
                "configure domain policy in the Capsem profile"
            )
        if data.get("host_workspace_dir") is not None:
            raise NotImplementedError(
                "host_workspace_dir is not supported: Capsem does not expose a host VirtioFS mount"
            )
        for bad_key in _REJECTED_DOCKERFILE_KEYS:
            if data.get(bad_key) is not None:
                raise ValueError(
                    f"Config field {bad_key!r} is not supported in Capsem OCI-workload mode; "
                    "specify a pre-built 'image' reference instead."
                )
        out = dict(data)
        if out.get("allowed_host_env"):
            from .containers.compose_yaml import _validate_host_env_patterns

            _validate_host_env_patterns(out["allowed_host_env"], source="allowed_host_env")
        if "execution_mode" not in out:
            for key in ("compose_file", "image"):
                if isinstance(out.get(key), str) and out[key].strip():
                    out["execution_mode"] = "container"
                    break
        return out

    def __hash__(self) -> int:
        return hash(tuple(_json_hashable(getattr(self, k)) for k in type(self).model_fields))

    def to_container_spec(self) -> ContainerSpec:
        """Convert this sandbox config into an Inspect-free `ContainerSpec`."""
        from .containers.compose_fields import resolve_effective_allowed_host_paths
        from .containers.spec import ContainerSpec

        eff_paths = list(resolve_effective_allowed_host_paths(self.allowed_host_paths))
        if self.compose_file and self.compose_file.strip():
            compose_dir = str(Path(self.compose_file).expanduser().resolve().parent)
            if compose_dir not in eff_paths:
                eff_paths.append(compose_dir)
        return ContainerSpec(
            image=self.image,
            working_dir=self.working_dir,
            working_dir_explicit="working_dir" in self.model_fields_set
            or self._compose_working_dir,
            environment=dict(self.environment),
            command=self.command,
            volumes=self.volumes,
            healthcheck=dict(self.healthcheck) if self.healthcheck is not None else None,
            mem_limit=self.mem_limit,
            user=self.user,
            allowed_host_paths=tuple(eff_paths),
        )
