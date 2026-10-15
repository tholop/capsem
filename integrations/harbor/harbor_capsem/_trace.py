"""Export Capsem `session.db` / `session.bodies` and enrich or synthesize ATIF `trajectory.json`."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
from typing import Any

from capsem.errors import ExportUnavailableError
from capsem.trace import (
    HARBOR_EVENT_TABLES as _EVENT_TABLES,
)
from capsem.trace import (
    BodiesMode,
    BodyReader,
    build_steps_from_session_tables,
    export_session_ledger,
    find_session_db,
    resolve_include_security_events,
)
from capsem.trace import (
    enrich_atif_trajectory as _sdk_enrich_atif_trajectory,
)
from capsem.trace import (
    load_db_tables as _load_db_tables,
)
from capsem.trace import (
    session_db_to_atif as _sdk_session_db_to_atif,
)
from harbor.models.trial.paths import TrialPaths
from harbor.utils.trajectory_validator import TrajectoryValidator

from harbor_capsem._credentials import coerce_bool_flag


def _validate_and_write(
    trajectory_data: dict[str, Any], trajectory_path: Path | None = None
) -> dict[str, Any]:
    validator = TrajectoryValidator()
    if not validator.validate(trajectory_data, validate_images=False):
        raise ValueError(f"ATIF trajectory failed validation: {validator.get_errors()}")
    if trajectory_path is not None:
        trajectory_path.parent.mkdir(parents=True, exist_ok=True)
        trajectory_path.write_text(json.dumps(trajectory_data, indent=2) + "\n", encoding="utf-8")
    return trajectory_data


def resolve_trace_options(
    *, export_trace: Any = None, include_security_events: Any = None
) -> tuple[bool, bool]:
    """Resolve `export_trace` and `include_security_events` from kwargs or `CAPSEM_*` env."""
    eff_export = (
        coerce_bool_flag(export_trace, default=False)
        if export_trace is not None
        else coerce_bool_flag(os.environ.get("CAPSEM_TRACE_EXPORT", ""), default=False)
    )
    eff_inc_sec = (
        coerce_bool_flag(include_security_events, default=False)
        if include_security_events is not None
        else resolve_include_security_events(None)
    )
    return eff_export, eff_inc_sec


def session_db_to_atif(
    db_path: Path,
    *,
    vm_id: str,
    session_id: str | None = None,
    bodies_path: Path | None = None,
    include_security_events: bool = False,
    bodies: BodiesMode = "previews",
    body_reader: BodyReader | None = None,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Synthesize a Harbor-validated `ATIF-v1.7` trajectory directly from `session.db`."""
    traj = _sdk_session_db_to_atif(
        db_path,
        vm_id=vm_id,
        session_id=session_id,
        bodies_path=bodies_path,
        include_security_events=include_security_events,
        bodies=bodies,
        body_reader=body_reader,
    )
    return _validate_and_write(traj, output_path)


def enrich_trajectory_file(
    trajectory_path: Path,
    db_path: Path,
    *,
    vm_id: str,
    bodies_path: Path | None = None,
    include_security_events: bool = False,
    bodies: BodiesMode = "previews",
    body_reader: BodyReader | None = None,
) -> dict[str, Any]:
    """Enrich an ATIF `trajectory.json` file in place from `db_path` and validate it."""
    trajectory_data: dict[str, Any] = json.loads(trajectory_path.read_text(encoding="utf-8"))
    _sdk_enrich_atif_trajectory(
        trajectory_data,
        db_path,
        vm_id=vm_id,
        bodies_path=bodies_path,
        include_security_events=include_security_events,
        bodies=bodies,
        body_reader=body_reader,
    )
    return _validate_and_write(trajectory_data, trajectory_path)


async def export_and_enrich_trial_trace(
    *,
    vm_id: str,
    trial_paths: TrialPaths,
    session_id: str | None = None,
    include_security_events: bool = False,
    capsem_home: Path | None = None,
    vm: Any = None,
    bodies: BodiesMode | None = None,
) -> tuple[Path | None, Path | None]:
    """Export `<vm_id>.session.db` and enrich or synthesize `trajectory.json`."""
    vm_export = getattr(vm, "export", None)
    if vm_export is not None and callable(getattr(vm_export, "quiesce", None)):
        await vm_export.quiesce()
    traj_path = trial_paths.agent_dir / "trajectory.json"
    if vm is not None and hasattr(vm, "export") and capsem_home is None:
        with contextlib.suppress(ExportUnavailableError):
            dst_db, dst_bodies = await vm.export.ledger(
                trial_paths.trial_dir, include_security_events=include_security_events
            )
            has_traj = traj_path.is_file()
            if not has_traj:
                counts = await asyncio.to_thread(
                    _load_db_tables, dst_db, ("model_calls", "exec_events")
                )
                should_export = bool(counts["model_calls"] or counts["exec_events"])
            else:
                should_export = True
            if should_export:
                traj = await vm.export.atif(
                    traj_path if has_traj else None,
                    bodies=bodies or "http",
                    include_security_events=include_security_events,
                    session_id=session_id,
                )
                if isinstance(s := traj.get("extra", {}).get("capsem_summary"), dict):
                    s["session_db"] = dst_db.name
                    s["session_bodies"] = (
                        dst_bodies.name if dst_bodies and dst_bodies.exists() else None
                    )
                await asyncio.to_thread(_validate_and_write, traj, traj_path)
            return dst_db, dst_bodies

    def _run_sync() -> tuple[Path | None, Path | None]:
        dst_db, dst_bodies = export_session_ledger(
            vm_id,
            trial_paths.trial_dir,
            include_security_events=include_security_events,
            capsem_home=capsem_home,
        )
        if dst_db is None:
            return None, None
        eff_bodies: BodiesMode = bodies or "previews"
        if traj_path.is_file():
            enrich_trajectory_file(
                traj_path,
                dst_db,
                vm_id=vm_id,
                bodies_path=dst_bodies,
                include_security_events=include_security_events,
                bodies=eff_bodies,
            )
        else:
            counts = _load_db_tables(dst_db, ("model_calls", "exec_events"))
            if counts["model_calls"] or counts["exec_events"]:
                session_db_to_atif(
                    dst_db,
                    vm_id=vm_id,
                    session_id=session_id,
                    bodies_path=dst_bodies,
                    include_security_events=include_security_events,
                    bodies=eff_bodies,
                    output_path=traj_path,
                )
        return dst_db, dst_bodies

    return await asyncio.to_thread(_run_sync)


__all__ = [
    "_EVENT_TABLES",
    "_validate_and_write",
    "build_steps_from_session_tables",
    "enrich_trajectory_file",
    "export_and_enrich_trial_trace",
    "export_session_ledger",
    "find_session_db",
    "resolve_trace_options",
    "session_db_to_atif",
]
