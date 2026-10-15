"""Local `session.db` / `session.bodies` discovery, SQLite snapshot export, and table loading."""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import os
import shutil
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ._transport import CapsemError, HttpError
from .discovery import capsem_run_dir

logger = logging.getLogger(__name__)
EXPORT_PERSIST_PREFIX = "export-"

HARBOR_EVENT_TABLES = (
    "exec_events",
    "audit_events",
    "net_events",
    "dns_events",
    "fs_events",
    "substitution_events",
)
DROPPED_SECURITY_TABLES = (
    "security_rule_events",
    "security_decision_events",
    "security_ask_events",
)
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def parse_iso_datetime(value: Any) -> dt.datetime | None:
    """Parse an ISO-8601 timestamp or `datetime` into a UTC `datetime`."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)
    s = str(value).strip()
    if not s:
        return None
    try:
        parsed = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return parsed.replace(tzinfo=dt.UTC) if parsed.tzinfo is None else parsed.astimezone(dt.UTC)


def resolve_include_security_events(
    include_security_events: bool | None = None,
) -> bool:
    """Return whether security-engine tables should be retained in exported traces."""
    if include_security_events is not None:
        return bool(include_security_events)
    return os.environ.get("CAPSEM_TRACE_INCLUDE_SECURITY", "").strip().lower() in _TRUTHY


def find_session_db(
    vm_id: str,
    *,
    capsem_home: str | Path | None = None,
    run_dir: str | Path | None = None,
) -> Path | None:
    """Locate `<run_dir>/sessions/<vm_id>/session.db` on the local host."""
    if not vm_id:
        return None
    candidates: list[Path] = []
    if run_dir is not None:
        candidates.append(Path(run_dir).expanduser())
    if capsem_home is not None:
        home_p = Path(capsem_home).expanduser()
        candidates.extend([home_p / "run", home_p])
    candidates.append(capsem_run_dir())
    for base in candidates:
        p = base / "sessions" / vm_id / "session.db"
        if p.is_file():
            return p
    return None


def _pinned_generation_filename(db_path: Path) -> str | None:
    try:
        with contextlib.closing(
            sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        ) as conn:
            row = conn.execute("SELECT generation_id FROM archive_state LIMIT 1").fetchone()
            if row and row[0]:
                raw = row[0]
                gen_hex = raw.hex() if isinstance(raw, (bytes, bytearray)) else str(raw).strip()
                return f"g-{gen_hex}.cbl" if gen_hex else None
    except sqlite3.Error:
        return None
    return None


def export_session_ledger(
    vm_id: str,
    out_dir: str | Path,
    *,
    labels: Mapping[str, str] | None = None,
    include_security_events: bool | None = None,
    capsem_home: str | Path | None = None,
    run_dir: str | Path | None = None,
    write_meta: bool = True,
) -> tuple[Path | None, Path | None]:
    """Snapshot `<vm_id>/session.db` (`VACUUM INTO`) and pinned `session.bodies/g-*.cbl`."""
    src_db = find_session_db(vm_id, capsem_home=capsem_home, run_dir=run_dir)
    if src_db is None or not src_db.is_file():
        return None, None
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    dst_db = out / f"{vm_id}.session.db"
    dst_db.unlink(missing_ok=True)
    with contextlib.closing(
        sqlite3.connect(f"file:{src_db}?mode=ro", uri=True, timeout=15.0)
    ) as conn:
        conn.execute("VACUUM INTO ?", (str(dst_db),))
    if not resolve_include_security_events(include_security_events=include_security_events):
        with contextlib.closing(sqlite3.connect(str(dst_db), timeout=15.0)) as conn:
            for tbl in DROPPED_SECURITY_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {tbl}")
            conn.execute(
                "DELETE FROM event_body_blobs WHERE source_table IN (?, ?, ?)",
                DROPPED_SECURITY_TABLES,
            )
            conn.commit()
        with contextlib.closing(sqlite3.connect(str(dst_db), timeout=15.0)) as conn:
            conn.execute("VACUUM")
    src_bodies = src_db.parent / "session.bodies"
    dst_bodies: Path | None = None
    if src_bodies.is_dir():
        dst_bodies = out / f"{vm_id}.session.bodies"
        shutil.rmtree(dst_bodies, ignore_errors=True)
        pinned_cbl = _pinned_generation_filename(dst_db)
        pinned_src = src_bodies / pinned_cbl if pinned_cbl else None
        if pinned_src is not None and pinned_src.is_file():
            dst_bodies.mkdir(parents=True, exist_ok=True)
            shutil.copy2(pinned_src, dst_bodies / pinned_src.name)
        else:
            shutil.copytree(src_bodies, dst_bodies)
    if write_meta:
        meta = {
            "vm_id": vm_id,
            "labels": dict(labels or {}),
            "session_db": dst_db.name,
            "session_bodies": dst_bodies.name if dst_bodies else None,
        }
        (out / f"{vm_id}.meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return dst_db, dst_bodies


def is_export_persisted_vm(vm_info: Any) -> bool:
    """Return whether `vm_info` is a temporary persistent VM created by `quiesce()`."""
    if not bool(getattr(vm_info, "persistent", False)):
        return False
    name = str(getattr(vm_info, "name", "") or "")
    return name.startswith(EXPORT_PERSIST_PREFIX)


async def quiesce_vm_for_export(session: Any, vm_id: str | None = None) -> bool:
    """Persist (if ephemeral) and stop `session` so `#346` drains the session ledger.

    Leaves the VM in the `stopped` state so `capsem-process` `FsMonitor`, the proxy worker,
    and `ledger-worker` flush and seal `session.bodies/g-*.cbl` before `session.db` is read.
    Returns `True` when the VM is confirmed stopped or drained, `False` on error.
    """
    if session is None:
        return False
    vid = str(vm_id or getattr(session, "id", "") or getattr(session, "_id", "") or "")
    already_persistent = False
    info_fn = getattr(session, "info", None)
    if callable(info_fn):
        try:
            info = await info_fn()
            raw_status = getattr(info, "status", None)
            status_str = str(getattr(raw_status, "value", raw_status) or "").lower()
            if status_str == "stopped":
                return True
            already_persistent = bool(getattr(info, "persistent", False))
        except HttpError as exc:
            if exc.status == 404:
                logger.warning("VM %s not found before export drain barrier: %s", vid, exc)
                return False
            logger.warning("Failed to query VM %s info before export drain: %s", vid, exc)
        except (CapsemError, OSError, RuntimeError, ValueError, TypeError) as exc:
            logger.warning("Failed to query VM %s info before export drain: %s", vid, exc)

    persist_fn = getattr(session, "persist", None)
    stop_fn = getattr(session, "stop", None)
    if not callable(stop_fn):
        return False
    if not already_persistent and callable(persist_fn):
        slug = "".join(c for c in vid.lower() if c.isalnum() or c == "-").strip("-")[:12] or "vm"
        try:
            await persist_fn(f"{EXPORT_PERSIST_PREFIX}{slug}")
        except HttpError as exc:
            if exc.status != 400:
                logger.warning("Failed to persist VM %s for export drain barrier: %s", vid, exc)
                return False
        except (CapsemError, OSError, RuntimeError, ValueError, TypeError) as exc:
            logger.warning("Failed to persist VM %s for export drain barrier: %s", vid, exc)
            return False
    try:
        await stop_fn()
    except (CapsemError, OSError, RuntimeError, ValueError, TypeError) as exc:
        logger.warning("Failed to stop VM %s for export drain barrier: %s", vid, exc)
        return False
    return True


def has_session_events(db_path: str | Path) -> bool:
    """Return whether `db_path` contains at least one `model_calls` or `exec_events` row."""
    db_p = Path(db_path)
    if not db_p.is_file():
        return False
    with contextlib.closing(sqlite3.connect(f"file:{db_p}?mode=ro", uri=True)) as conn:
        for table in ("model_calls", "exec_events"):
            try:
                if conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None:
                    return True
            except sqlite3.OperationalError:
                continue
    return False


def load_db_tables(
    db_path: str | Path, tables: Sequence[str], *, order_by_id: bool = True
) -> dict[str, list[dict[str, Any]]]:
    """Load rows from the requested SQLite tables in `db_path`."""
    db_p = Path(db_path)
    if not db_p.is_file():
        return {t: [] for t in tables}
    out: dict[str, list[dict[str, Any]]] = {}
    with contextlib.closing(sqlite3.connect(f"file:{db_p}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        existing = {
            str(r[0])
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        for t in tables:
            if t not in existing:
                if t in DROPPED_SECURITY_TABLES:
                    out[t] = []
                    continue
                raise sqlite3.OperationalError(f"missing required ledger table: {t}")
            sql = f"SELECT * FROM {t} ORDER BY id ASC" if order_by_id else f"SELECT * FROM {t}"
            rows = conn.execute(sql).fetchall()
            out[t] = [dict(row) for row in rows]
    return out
