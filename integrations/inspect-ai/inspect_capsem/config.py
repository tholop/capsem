"""Configuration model for the Capsem Inspect AI SandboxEnvironment."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from inspect_ai.util import SandboxEnvironmentConfigType


class CapsemSandboxConfig(BaseModel, frozen=True, extra="forbid"):
    """Configuration for `CapsemSandboxEnvironment` (VM-level execution)."""

    cpu_count: int = Field(default=4)
    ram_gb: int = Field(default=8)
    working_dir: str = Field(default="/workspace")
    environment: dict[str, str] = Field(default_factory=dict)
    user: str | None = Field(default=None)

    def __hash__(self) -> int:
        return hash(self.model_dump_json())


def coerce_config(
    config: SandboxEnvironmentConfigType | dict[str, Any] | str | None,
) -> CapsemSandboxConfig:
    """Coerce an Inspect sandbox config into a `CapsemSandboxConfig`."""
    if config is None:
        return CapsemSandboxConfig()
    if isinstance(config, CapsemSandboxConfig):
        return config
    if isinstance(config, dict):
        return CapsemSandboxConfig.model_validate(config)
    msg = f"Unsupported Capsem sandbox config type: {type(config)!r}"
    raise TypeError(msg)
