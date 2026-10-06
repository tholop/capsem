"""Compose service field normalizers and host-path containment helpers."""

from __future__ import annotations

import logging
import math
import os
import re
import shlex
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .compose_yaml import (
    _is_host_env_allowed,
    _load_dotenv,
    _warn_blocked_host_env,
    build_sample_metadata_env,
    resolve_effective_allowed_host_env,
)

logger = logging.getLogger(__name__)
CAPSEM_INSPECT_ALLOWED_HOST_PATHS_VAR = "CAPSEM_INSPECT_ALLOWED_HOST_PATHS"
_MEM_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([bkmg]i?b?)?\s*$", re.IGNORECASE)
_MEM_FACTORS = {
    u: 1024**p
    for p, us in enumerate((" b", "k kb kib", "m mb mib", "g gb gib"))
    for u in us.split(" ")
}


def _realpath(raw: str | Path) -> Path:
    return Path(os.path.realpath(Path(raw).expanduser()))


def _is_under_root(target: Path, root: Path) -> bool:
    return target == root or target.is_relative_to(root)


def resolve_effective_allowed_host_paths(
    task_allowed_host_paths: Sequence[str] = (),
) -> tuple[str, ...]:
    """Resolve effective host-path allowlist from operator env narrowed by task config."""
    raw_op = os.environ.get(CAPSEM_INSPECT_ALLOWED_HOST_PATHS_VAR, "")
    op_roots = tuple(
        s.strip() for chunk in raw_op.split(",") for s in chunk.split(os.pathsep) if s.strip()
    )
    if not op_roots:
        return ()
    task_roots = tuple(r.strip() for r in task_allowed_host_paths if r and r.strip())
    if not task_roots:
        return op_roots
    resolved_op = [_realpath(r) for r in op_roots]
    resolved_task = [_realpath(r) for r in task_roots]
    narrowed = [str(tr) for tr in resolved_task if any(_is_under_root(tr, o) for o in resolved_op)]
    for opr in resolved_op:
        if any(_is_under_root(opr, tr) for tr in resolved_task) and str(opr) not in narrowed:
            narrowed.append(str(opr))
    return tuple(narrowed)


def _is_host_path_allowed(
    candidate: Path,
    allowed_host_paths: Sequence[str] = (),
    *,
    base_dir: Path | None = None,
) -> bool:
    """Return True if `candidate`'s `realpath` stays inside `base_dir` or allowed roots."""
    resolved = _realpath(candidate)
    if base_dir is not None and _is_under_root(resolved, _realpath(base_dir)):
        return True
    return any(_is_under_root(resolved, _realpath(r)) for r in allowed_host_paths if r)


def _looks_like_host_bind_source(src: str) -> bool:
    return (
        src in (".", "..")
        or src.startswith(("/", "./", "../", "~"))
        or "/" in src
        or "\\" in src
        or (len(src) >= 2 and src[1] == ":" and src[0].isalpha())
    )


def _resolve_bind_source(
    src: str, base_dir: Path | None, allowed_host_paths: Sequence[str] = ()
) -> str:
    expanded = Path(src).expanduser()
    target_path = expanded
    if base_dir is not None and not expanded.is_absolute() and not src.startswith("~"):
        candidate = base_dir / expanded
        if src.startswith(".") or "/" in src or candidate.exists() or candidate.is_symlink():
            target_path = candidate
    resolved = target_path.resolve()
    eff_roots = resolve_effective_allowed_host_paths(allowed_host_paths)
    if not _is_host_path_allowed(resolved, eff_roots, base_dir=base_dir):
        raise ValueError(
            f"Host bind mount source {src!r} (resolved to {str(resolved)!r}) is outside the "
            f"Compose directory and not allowlisted via {CAPSEM_INSPECT_ALLOWED_HOST_PATHS_VAR} "
            "/ allowed_host_paths."
        )
    return str(resolved)


def _normalize_environment(
    raw_env: Any,
    base_dir: Path | None = None,
    *,
    allowed_host_env: Sequence[str] = (),
    sample_metadata: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    dotenv = _load_dotenv(base_dir / ".env") if base_dir is not None else {}
    sample_env = build_sample_metadata_env(sample_metadata)
    eff_env = resolve_effective_allowed_host_env(allowed_host_env)
    warned_blocked: set[str] = set()

    def _lookup_inherited(k: str) -> str:
        if k.startswith("SAMPLE_METADATA_"):
            return sample_env.get(k, "")
        if k in os.environ and _is_host_env_allowed(k, eff_env):
            return os.environ[k]
        if k in dotenv:
            return dotenv[k]
        if k in os.environ:
            _warn_blocked_host_env(k, warned_blocked)
        return ""

    if isinstance(raw_env, Mapping):
        return {
            str(k): (
                _lookup_inherited(str(k))
                if v is None
                else ("true" if v else "false")
                if isinstance(v, bool)
                else str(v)
            )
            for k, v in raw_env.items()
        }
    if isinstance(raw_env, list):
        return {
            (item.partition("=")[0] if "=" in item else item): (
                item.partition("=")[2] if "=" in item else _lookup_inherited(item)
            )
            for item in raw_env
            if isinstance(item, str)
        }
    return {}


def _normalize_command_field(raw: Any) -> tuple[str, ...] | str | None:
    if raw is None or isinstance(raw, str):
        return raw
    return tuple(str(x) for x in raw) if isinstance(raw, (list, tuple)) else str(raw)


def _combine_entrypoint_and_command(
    entrypoint: tuple[str, ...] | str | None,
    command: tuple[str, ...] | str | None,
) -> tuple[str, ...] | str | None:
    if entrypoint is None:
        return command
    ep_parts = tuple(shlex.split(entrypoint)) if isinstance(entrypoint, str) else entrypoint
    if not ep_parts or command is None:
        return ep_parts if ep_parts else command
    cmd_parts = tuple(shlex.split(command)) if isinstance(command, str) else command
    return (*ep_parts, *cmd_parts)


def _normalize_volumes(
    raw_vols: Any, base_dir: Path | None, allowed_host_paths: Sequence[str] = ()
) -> tuple[str, ...]:
    if not isinstance(raw_vols, (list, tuple)):
        return ()
    out: list[str] = []
    for entry in raw_vols:
        if isinstance(entry, str):
            parts = entry.split(":")
            if len(parts) >= 2 and _looks_like_host_bind_source(parts[0]):
                parts[0] = _resolve_bind_source(parts[0], base_dir, allowed_host_paths)
                out.append(":".join(parts))
            elif len(parts) >= 2:
                raise ValueError(
                    f"Named or non-bind Compose volume {entry!r} is not supported in Capsem "
                    "OCI-workload mode; use a relative or allowlisted host bind mount."
                )
            else:
                out.append(entry)
        elif isinstance(entry, Mapping):
            vol_type = str(entry.get("type") or "volume")
            if vol_type != "bind":
                raise ValueError(
                    f"Unsupported Compose volume type {vol_type!r} in {dict(entry)!r}; "
                    "only 'bind' volumes are supported in Capsem OCI-workload mode."
                )
            src = entry.get("source") or entry.get("src")
            target = entry.get("target") or entry.get("dst") or entry.get("destination")
            if not src or not target:
                raise ValueError(
                    f"Compose volume entry {dict(entry)!r} requires both 'source' and 'target'."
                )
            src_str = _resolve_bind_source(str(src), base_dir, allowed_host_paths)
            out.append(
                f"{src_str}:{target}:ro" if entry.get("read_only") else f"{src_str}:{target}"
            )
    return tuple(out)


def parse_memory_to_ram_gb(raw_mem: Any) -> int:
    """Convert Compose memory limit (`512m`, `2g`, integer bytes) up to integer GiB (`>=1`)."""
    if isinstance(raw_mem, (int, float)) and not isinstance(raw_mem, bool):
        if raw_mem <= 0:
            raise ValueError(f"Invalid Compose memory limit: {raw_mem!r}")
        return max(1, math.ceil(float(raw_mem) / (1024**3)))
    m = _MEM_RE.match(str(raw_mem).strip())
    if not m:
        raise ValueError(f"Invalid Compose memory limit: {raw_mem!r}")
    amount, unit = float(m.group(1)), (m.group(2) or "").lower()
    if amount <= 0 or unit not in _MEM_FACTORS:
        raise ValueError(f"Invalid Compose memory limit: {raw_mem!r}")
    return max(1, math.ceil((amount * _MEM_FACTORS[unit]) / (1024**3)))


def parse_cpus_to_cpu_count(raw_cpus: Any) -> int:
    """Convert Compose `cpus` (`"1.5"`, `2`) up to integer vCPU count (`>=1`)."""
    try:
        val = float(raw_cpus)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid Compose cpus limit: {raw_cpus!r}") from exc
    if val <= 0 or math.isnan(val) or math.isinf(val):
        raise ValueError(f"Invalid Compose cpus limit: {raw_cpus!r}")
    return max(1, math.ceil(val))


def _normalize_healthcheck(raw_hc: Any) -> dict[str, Any] | None:
    if not isinstance(raw_hc, Mapping) or raw_hc.get("disable") is True:
        return None
    test = raw_hc.get("test")
    if test is None:
        return None
    if isinstance(test, (list, tuple)) and len(test) > 0 and str(test[0]).upper() == "NONE":
        return None
    if isinstance(test, str) and test.strip().upper() == "NONE":
        return None
    out: dict[str, Any] = {
        "test": [str(x) for x in test] if isinstance(test, (list, tuple)) else str(test)
    }
    for key in ("interval", "timeout", "start_period", "start_interval", "retries"):
        if raw_hc.get(key) is not None:
            out[key] = raw_hc[key]
    return out
