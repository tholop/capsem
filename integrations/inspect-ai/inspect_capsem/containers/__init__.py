"""Inspect-free OCI container and Compose support for `inspect-capsem`."""

from __future__ import annotations

from .compose import extract_compose_fields, parse_compose_yaml_file
from .controller import CapsemController, CommandResult
from .runtime import (
    _OCI_WORKLOAD_CONTAINER_ID,
    prepare_oci_workload_container,
    resolve_container_working_dir,
)
from .spec import ContainerSpec

__all__ = [
    "_OCI_WORKLOAD_CONTAINER_ID",
    "CapsemController",
    "CommandResult",
    "ContainerSpec",
    "extract_compose_fields",
    "parse_compose_yaml_file",
    "prepare_oci_workload_container",
    "resolve_container_working_dir",
]
