"""VM-scoped session export (`vm.export.quiesce`, `warc`, `ledger`, and `atif`)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, overload
from urllib.parse import urlsplit

from . import _operations as api
from ._resources import Resource
from ._trace_bodies import BodiesMode, HttpBodyReader, candidate_body_event_ids
from ._trace_ledger import (
    DROPPED_SECURITY_TABLES,
    EXPORT_PERSIST_PREFIX,
    HARBOR_EVENT_TABLES,
    export_session_ledger,
    find_session_db,
    is_export_persisted_vm,
    load_db_tables,
    quiesce_vm_for_export,
    resolve_include_security_events,
)
from .discovery import capsem_run_dir
from .errors import ExportUnavailableError
from .trace import enrich_atif_trajectory, session_db_to_atif

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_ATIF_CORE_TABLES = (
    "model_calls",
    "model_items",
    "tool_calls",
    "tool_responses",
    "event_body_blobs",
    *HARBOR_EVENT_TABLES,
)


class Export(Resource):
    """Export session artifacts (`quiesce`, `warc`, `ledger`, `atif`) from a `VM`."""

    def _is_loopback_gateway(self) -> bool:
        host = (urlsplit(self._vm._transport._url).hostname or "").lower()
        return host in _LOOPBACK_HOSTS

    def _local_run_dir(self) -> Path:
        t = self._vm._transport
        return t._explicit_run_dir or t._run_dir or capsem_run_dir()

    def _resolve_local_session_db(self, vm_id: str) -> Path:
        if not self._is_loopback_gateway() and self._vm._transport._explicit_run_dir is None:
            raise ExportUnavailableError(
                f"VM {vm_id!r} is on remote gateway {self._vm._transport._url!r} and no "
                "local run_dir was configured; local session.db is unavailable. Use "
                "await vm.export.warc() for HTTP WARC export over the gateway.",
                vm_id=vm_id,
            )
        rdir = self._local_run_dir()
        db_path = find_session_db(vm_id, run_dir=rdir)
        if db_path is None or not db_path.is_file():
            raise ExportUnavailableError(
                f"local session.db not found for VM {vm_id!r} under {rdir}/sessions/{vm_id}. "
                "Wait for session creation or use await vm.export.warc().",
                vm_id=vm_id,
            )
        return db_path

    async def quiesce(self) -> bool:
        """Persist (if ephemeral) and stop this VM to drain its session ledger before export.

        Transitions a running VM to the `stopped` state under `#346`'s `ShutdownMode::Retain`
        so `FsMonitor`, the proxy worker, and `ledger-worker` flush and seal `session.bodies`.
        """
        resolve_fn = getattr(self._vm, "_resolve", None)
        vm_id = await resolve_fn() if callable(resolve_fn) else str(getattr(self._vm, "id", ""))
        return await quiesce_vm_for_export(self._vm, vm_id)

    @overload
    async def warc(self, output_path: None = None) -> bytes: ...

    @overload
    async def warc(self, output_path: str | Path) -> Path: ...

    async def warc(self, output_path: str | Path | None = None) -> bytes | Path:
        """Download `GET /vms/{id}/bodies/export.warc.gz` as gzip-compressed WARC 1.1 bytes."""
        vm_id = await self._vm._resolve()
        data = await self._vm._call(
            vm_id, api.export_vm_bodies(self._vm._transport, id=vm_id)
        )
        if output_path is None:
            return data
        out = Path(output_path)
        await asyncio.to_thread(out.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(out.write_bytes, data)
        return out

    async def ledger(
        self,
        out_dir: str | Path,
        *,
        include_security_events: bool | None = None,
        labels: Mapping[str, str] | None = None,
        write_meta: bool = False,
        quiesce: bool = False,
    ) -> tuple[Path, Path | None]:
        """Snapshot `<vm_id>.session.db` (`VACUUM INTO`) and `<vm_id>.session.bodies/g-*.cbl`."""
        vm_id = await self._vm._resolve()
        if quiesce:
            await quiesce_vm_for_export(self._vm, vm_id)
        self._resolve_local_session_db(vm_id)
        rdir = self._local_run_dir()
        dst_db, dst_bodies = await asyncio.to_thread(
            export_session_ledger,
            vm_id,
            out_dir,
            labels=labels,
            include_security_events=include_security_events,
            run_dir=rdir,
            write_meta=write_meta,
        )
        if dst_db is None:
            raise ExportUnavailableError(
                f"Failed to snapshot session.db for VM {vm_id!r}", vm_id=vm_id
            )
        return dst_db, dst_bodies

    async def atif(
        self,
        trajectory: str | Path | dict[str, Any] | None = None,
        *,
        output_path: str | Path | None = None,
        bodies: BodiesMode = "http",
        include_security_events: bool | None = None,
        session_id: str | None = None,
        agent_name: str | None = None,
        agent_version: str | None = None,
        max_body_bytes: int | None = None,
        quiesce: bool = False,
    ) -> dict[str, Any]:
        """Synthesize or enrich an `ATIF-v1.7` trajectory for this VM."""
        vm_id = await self._vm._resolve()
        if quiesce:
            await quiesce_vm_for_export(self._vm, vm_id)
        db_path = self._resolve_local_session_db(vm_id)
        bodies_dir = db_path.parent / "session.bodies"
        bodies_path = bodies_dir if bodies_dir.is_dir() else None
        keep_sec = resolve_include_security_events(include_security_events=include_security_events)
        t_names = (*_ATIF_CORE_TABLES, *(DROPPED_SECURITY_TABLES if keep_sec else ()))
        tables = await asyncio.to_thread(load_db_tables, db_path, t_names)
        reader: HttpBodyReader | None = None
        if bodies in ("previews", "http", "full"):
            reader = HttpBodyReader(self._vm._transport, vm_id, max_bytes=max_body_bytes)
            eids = candidate_body_event_ids(tables, full=(bodies == "full"))
            if eids:
                await reader.prefetch(eids)

        out_p = Path(output_path) if output_path is not None else None

        def _build() -> dict[str, Any]:
            if trajectory is None:
                return session_db_to_atif(
                    db_path,
                    vm_id=vm_id,
                    session_id=session_id,
                    agent_name=agent_name,
                    agent_version=agent_version,
                    bodies_path=bodies_path,
                    include_security_events=keep_sec,
                    bodies=bodies,
                    body_reader=reader,
                    output_path=out_p,
                    _preloaded_tables=tables,
                )
            if isinstance(trajectory, (str, Path)):
                traj_p = Path(trajectory)
                traj_data: dict[str, Any] = json.loads(traj_p.read_text(encoding="utf-8"))
                eff_out = out_p or traj_p
            else:
                traj_data = dict(trajectory)
                eff_out = out_p
            return enrich_atif_trajectory(
                traj_data,
                db_path,
                vm_id=vm_id,
                bodies_path=bodies_path,
                include_security_events=keep_sec,
                bodies=bodies,
                body_reader=reader,
                output_path=eff_out,
                _preloaded_tables=tables,
            )

        return await asyncio.to_thread(_build)


__all__ = [
    "EXPORT_PERSIST_PREFIX",
    "Export",
    "is_export_persisted_vm",
    "quiesce_vm_for_export",
]
