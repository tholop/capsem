"""`vm.export` sub-resource (`warc`, `ledger`, `atif`) for exporting Capsem session traces."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from . import _operations as api
from ._resources import Resource
from ._trace_atif import HARBOR_EVENT_TABLES
from ._trace_bodies import (
    VALID_BODIES_MODES,
    BodiesMode,
    HttpBodyReader,
    candidate_body_event_ids,
)
from ._trace_ledger import (
    DROPPED_SECURITY_TABLES,
    export_session_ledger,
    find_session_db,
    load_db_tables,
)
from .errors import CapsemError, ExportUnavailableError
from .trace import enrich_atif_trajectory, session_db_to_atif

if TYPE_CHECKING:
    from .vm import VM

_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")

# TODO(Option C - Rust GET /vms/{id}/export/atif streaming route):
# Today `vm.export.atif()` reads rows from the local `<run_dir>/{sessions,persistent}/<id>/session.db`
# and hydrates truncated bodies over loopback HTTP via `GET /vms/{id}/bodies/{event_id}` (`bodies="http"`).
# A future Rust `GET /vms/{id}/export/atif` endpoint in `capsem-logger` + `capsem-service` (modeled on
# `DbHandle::export_warc` in `crates/capsem-logger/src/db/warc_export.rs`) would provide:
#   1. Single-pass `BodyLogReader` streaming in archive block order without per-event loopback HTTP calls.
#   2. Standalone ATIF synthesis over remote gateways where `session.db` is not on the local filesystem.
# Note: enrichment of host-collected trajectories (`trajectory=<dict|path>`, e.g. Inspect `.eval` or
# Harbor `terminus-2`) requires host-side trajectory state and will remain in Python even with Option C.


def _resolve_local_session_db(vm: VM, vm_id: str) -> Path:
    explicit_run_dir: Path | None = getattr(vm._transport, "_explicit_run_dir", None)
    run_dir: Path | None = getattr(vm._transport, "_run_dir", None)
    host = urlsplit(vm._transport._url).hostname
    if explicit_run_dir is None and host not in _LOCAL_HOSTS:
        raise ExportUnavailableError(
            f"VM {vm_id!r} uses non-local gateway {vm._transport._url!r}; local session.db is unavailable",
            vm_id=vm_id,
        )
    db_path = find_session_db(vm_id, run_dir=run_dir)
    if db_path is None:
        raise ExportUnavailableError(
            f"local session.db not found for VM {vm_id!r} under {run_dir or 'default capsem run_dir'}",
            vm_id=vm_id,
        )
    return db_path


async def _quiesce_vm_for_export(session: Any, vm_id: str) -> None:
    """Quiesce a VM via `#346`'s `persist` + `stop` (`ShutdownMode::Retain`) drain barrier before export."""
    if session is None or not vm_id or getattr(session, "_quiesced_for_export", False):
        return
    if not hasattr(session, "stop"):
        return
    can_stop = not hasattr(session, "persist")
    if not can_stop:
        try:
            await session.persist(f"export-{vm_id[:12]}")
            can_stop = True
        except (CapsemError, RuntimeError, OSError) as exc:
            can_stop = "already persistent" in str(exc).lower()
    if can_stop:
        with contextlib.suppress(CapsemError, RuntimeError, OSError):
            await session.stop()
            session._quiesced_for_export = True


class Export(Resource):
    """Export a VM's archived HTTP/exec/security bodies (WARC), SQLite session ledger, or ATIF-v1.7 trajectory.

    `ledger-worker` & teardown drain barrier (`#346`):
    - Each session's dedicated `ledger-worker` (`LedgerServer`) owns `session.db` and
      `session.bodies/g-<gen>.cbl`, receiving events from `capsem-process` (`FsMonitor`/exec/audit),
      the MITM proxy worker, and `capsem-service`, and serving `GET /vms/{id}/bodies/*` over IPC.
    - While a VM is running, `ledger-worker` flushes accepted rows to `session.db` (WAL) every
      `<= 5 s` (`DISK_FLUSH_INTERVAL`), and finalizes `session.bodies/g-<gen>.cbl` on shutdown.
    - Before tearing down an ephemeral VM, quiesce it through the `ShutdownMode::Retain` stop
      barrier (`await vm.persist(f"export-{vm_id[:12]}")` then `await vm.stop()`) so `FsMonitor`,
      the proxy worker, and `ledger-worker` drain and finalize `session.bodies/g-<gen>.cbl` before
      calling `vm.export.ledger()` / `vm.export.atif()`, then call `await vm.delete()`. Calling
      `await vm.stop()` on an unpersisted ephemeral VM deletes the session directory immediately.
    """

    async def warc(self, dest: str | Path | None = None) -> bytes | Path:
        """Export archived event bodies as a gzipped WARC 1.1 stream (`GET /vms/{id}/bodies/export.warc.gz`)."""
        vm_id = await self._vm._resolve()
        payload = await self._vm._call(
            vm_id, api.export_vm_bodies(self._vm._transport, id=vm_id)
        )
        if dest is None:
            return payload
        target = Path(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_bytes, payload)
        return target

    async def ledger(
        self,
        dest_dir: str | Path,
        *,
        drop_security_tables: bool = True,
        labels: Mapping[str, str] | None = None,
        write_meta: bool = False,
    ) -> tuple[Path, Path | None]:
        """Copy `<vm_id>.session.db` and generation-pinned `<vm_id>.session.bodies/g-<gen>.cbl` into `dest_dir`."""
        vm_id = await self._vm._resolve()
        db_path = _resolve_local_session_db(self._vm, vm_id)
        run_dir = db_path.parent.parent.parent
        dst_db, dst_bodies = await asyncio.to_thread(
            export_session_ledger,
            vm_id,
            dest_dir,
            labels=labels,
            drop_security_tables=drop_security_tables,
            run_dir=run_dir,
            write_meta=write_meta,
        )
        if dst_db is None:
            raise ExportUnavailableError(
                f"failed to export session ledger for VM {vm_id!r}", vm_id=vm_id
            )
        return dst_db, dst_bodies

    async def atif(
        self,
        trajectory: Mapping[str, Any] | str | Path | None = None,
        *,
        output_path: str | Path | None = None,
        bodies: BodiesMode = "http",
        include_security_events: bool = False,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Synthesize (`trajectory=None`) or enrich (`trajectory=<dict|path>`) an `ATIF-v1.7` trajectory."""
        if bodies not in VALID_BODIES_MODES:
            raise ValueError(f"Invalid bodies mode {bodies!r}; expected one of {VALID_BODIES_MODES}")
        vm_id = await self._vm._resolve()
        db_path = _resolve_local_session_db(self._vm, vm_id)
        bodies_dir = db_path.parent / "session.bodies"
        bodies_path = bodies_dir if bodies_dir.exists() else None

        reader: HttpBodyReader | None = None
        if bodies in ("http", "full"):
            reader = HttpBodyReader(self._vm._transport, vm_id)
            ev_tbls = (*HARBOR_EVENT_TABLES, *(DROPPED_SECURITY_TABLES if include_security_events else ()))
            scan_tbls = ("model_calls", "tool_calls", "tool_responses", "event_body_blobs", *ev_tbls)
            loaded = await asyncio.to_thread(
                load_db_tables,
                db_path,
                scan_tbls,
                allow_missing=(*DROPPED_SECURITY_TABLES, "event_body_blobs"),
            )
            eids = candidate_body_event_ids(
                loaded,
                loaded["event_body_blobs"],
                bodies=bodies,
                include_security_events=include_security_events,
            )
            await reader.prefetch(eids)

        def _build_sync() -> dict[str, Any]:
            if trajectory is None:
                return session_db_to_atif(
                    db_path,
                    vm_id=vm_id,
                    session_id=session_id,
                    bodies_path=bodies_path,
                    drop_security_tables=not include_security_events,
                    include_security_events=include_security_events,
                    bodies=bodies,
                    body_reader=reader,
                    output_path=output_path,
                )
            if isinstance(trajectory, (str, Path)):
                traj_path = Path(trajectory)
                traj_dict: dict[str, Any] = json.loads(traj_path.read_text(encoding="utf-8"))
                eff_out = output_path if output_path is not None else traj_path
            else:
                traj_dict = copy.deepcopy(dict(trajectory))
                eff_out = output_path
            return enrich_atif_trajectory(
                traj_dict,
                db_path,
                vm_id=vm_id,
                bodies_path=bodies_path,
                drop_security_tables=not include_security_events,
                include_security_events=include_security_events,
                bodies=bodies,
                body_reader=reader,
                output_path=eff_out,
            )

        return await asyncio.to_thread(_build_sync)
