"""Low-level `session.db` / `session.bodies` discovery, generation-pinned ledger export, and table queries."""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import shutil
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ._trace_atif import HARBOR_EVENT_TABLES, parse_iso_datetime
from .discovery import capsem_run_dir

CAPSEM_EVENT_TABLES: tuple[str, ...] = (*HARBOR_EVENT_TABLES, "tool_calls", "tool_responses", "model_calls")
DROPPED_SECURITY_TABLES: tuple[str, ...] = ("security_rule_events", "security_decision_events", "security_ask_events")


def parse_iso_epoch(ts: Any) -> float | None:
    """Convert an ISO-8601 string, datetime, or numeric epoch into a UTC POSIX timestamp."""
    if ts is None or isinstance(ts, (int, float)):
        return float(ts) if ts is not None else None
    return dt.timestamp() if (dt := parse_iso_datetime(ts)) is not None else None


def format_iso_utc(ts: Any) -> str | None:
    """Format an ISO-8601 string, datetime, or numeric epoch as `YYYY-MM-DDTHH:MM:SSZ`."""
    if (epoch := parse_iso_epoch(ts)) is None:
        return None
    return datetime.datetime.fromtimestamp(epoch, tz=datetime.UTC).isoformat().replace("+00:00", "Z")


def should_drop_security_tables(
    drop_security_tables: bool | None = None, compact: bool | None = None
) -> bool:
    """Determine whether `security_*_events` tables should be dropped when exporting `session.db`."""
    if drop_security_tables is not None or compact is not None:
        return bool(drop_security_tables if drop_security_tables is not None else compact)
    keep_env = os.environ.get("CAPSEM_TRACE_KEEP_SECURITY_TABLES", "") or os.environ.get("CAPSEM_TRACE_KEEP_SECURITY", "")
    if keep_env.strip().lower() in ("1", "true", "yes"):
        return False
    if os.environ.get("CAPSEM_TRACE_DROP_SECURITY_TABLES", "").strip().lower() in ("0", "false", "no"):
        return False
    return os.environ.get("CAPSEM_TRACE_COMPACT", "").strip().lower() not in ("0", "false", "no")


def find_session_db(
    vm_id: str, *, capsem_home: str | Path | None = None, run_dir: str | Path | None = None
) -> Path | None:
    """Locate `<run_dir>/{sessions,persistent}/<vm_id>/session.db` if present."""
    if not vm_id:
        return None
    base = Path(capsem_home).expanduser() if capsem_home is not None else capsem_run_dir()
    roots = (
        (Path(run_dir).expanduser(),)
        if run_dir is not None
        else ((base / "run", base) if capsem_home is not None else (base, base.parent))
    )
    for root in roots:
        for subdir in ("sessions", "persistent"):
            if (candidate := root / subdir / vm_id / "session.db").is_file():
                return candidate
    return None


def load_db_tables(
    db_path: str | Path,
    tables: Sequence[str],
    *,
    allow_missing: Sequence[str] = DROPPED_SECURITY_TABLES,
) -> dict[str, list[dict[str, Any]]]:
    """Read rows from `tables` in `db_path`, raising `sqlite3.OperationalError` if required tables are missing."""
    path, out = Path(db_path), {t: [] for t in tables}
    if not path.is_file():
        return out
    with contextlib.closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        existing = {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if missing := [t for t in tables if t not in existing and t not in allow_missing]:
            raise sqlite3.OperationalError(f"missing required ledger table(s) in {path}: {', '.join(missing)}")
        for tbl in (t for t in tables if t in existing):
            try:
                rows = conn.execute(f"SELECT * FROM {tbl} ORDER BY timestamp ASC, id ASC").fetchall()
            except sqlite3.OperationalError:
                rows = conn.execute(f"SELECT * FROM {tbl} ORDER BY rowid ASC").fetchall()
            out[tbl] = [dict(r) for r in rows]
    return out


def export_session_ledger(
    vm_id: str, dest_dir: str | Path, *, labels: Mapping[str, str] | None = None,
    drop_security_tables: bool | None = None, compact: bool | None = None,
    capsem_home: str | Path | None = None, run_dir: str | Path | None = None, write_meta: bool = False,
) -> tuple[Path | None, Path | None]:
    """Backup `session.db` (and generation-pinned `session.bodies`) for `vm_id` into `dest_dir`."""
    if (src_db := find_session_db(vm_id, capsem_home=capsem_home, run_dir=run_dir)) is None:
        return None, None
    eff_drop, target_dir = should_drop_security_tables(drop_security_tables, compact), Path(dest_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    dst_db = target_dir / f"{vm_id}.session.db"
    raw_sz = sum(p.stat().st_size for p in (src_db, Path(f"{src_db}-wal"), Path(f"{src_db}-shm")) if p.exists())
    dst_db.unlink(missing_ok=True)
    with contextlib.closing(sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)) as sc, contextlib.closing(sqlite3.connect(dst_db)) as dc:
        sc.backup(dc)
    with contextlib.closing(sqlite3.connect(dst_db, isolation_level=None)) as conn:
        conn.execute("PRAGMA journal_mode=DELETE")
        if eff_drop:
            for tbl in DROPPED_SECURITY_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {tbl}")
            conn.execute("VACUUM")
    src_bodies = src_db.parent / "session.bodies"
    dst_bodies = target_dir / f"{vm_id}.session.bodies" if src_bodies.exists() else None
    if dst_bodies is not None:
        if src_bodies.is_dir():
            st = load_db_tables(dst_db, ("archive_state",), allow_missing=("archive_state",))["archive_state"]
            gen_raw = st[0].get("generation_id") if st else None
            gen_hex = gen_raw.hex() if isinstance(gen_raw, (bytes, bytearray)) else None
            pinned = src_bodies / f"g-{gen_hex}.cbl" if gen_hex else None
            if pinned is not None and pinned.is_file():
                shutil.rmtree(dst_bodies, ignore_errors=True)
                dst_bodies.mkdir(parents=True, exist_ok=True)
                shutil.copy2(pinned, dst_bodies / pinned.name)
            else:
                shutil.copytree(src_bodies, dst_bodies, dirs_exist_ok=True)
        else:
            shutil.copy2(src_bodies, dst_bodies)
    if write_meta or labels is not None:
        meta = {"vm_id": vm_id, "labels": dict(labels or {}), "raw_size_bytes": raw_sz, "compacted_size_bytes": dst_db.stat().st_size, "dropped_security_tables": eff_drop}
        (target_dir / f"{vm_id}.meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return dst_db, dst_bodies


def load_db_events(
    db_path: str | Path, *, include_security_events: bool = False, tables: Sequence[str] | None = None
) -> dict[str, list[tuple[float, dict[str, Any]]]]:
    """Load `(epoch, row_dict)` pairs from `session.db` event tables for window slicing."""
    sec = DROPPED_SECURITY_TABLES if include_security_events else ()
    eff_tables = tuple(tables) if tables is not None else CAPSEM_EVENT_TABLES + sec
    loaded: dict[str, list[tuple[float, dict[str, Any]]]] = {t: [] for t in eff_tables}
    for tbl, rows in load_db_tables(db_path, eff_tables).items():
        for row in rows:
            ep = parse_iso_epoch(row.get("timestamp"))
            if ep is None and row.get("timestamp_unix_ms") is not None:
                ep = float(row["timestamp_unix_ms"]) / 1000.0
            if ep is not None:
                loaded[tbl].append((ep, row))
    return loaded


def slice_events(
    db_events: Mapping[str, Sequence[tuple[float, dict[str, Any]]]],
    start_epoch: float,
    end_epoch: float,
    *,
    pad_secs: float = 0.25,
) -> dict[str, list[dict[str, Any]]]:
    """Filter `db_events` to rows whose timestamp falls within `[start - pad, end + pad]`."""
    lo, hi = start_epoch - pad_secs, max(end_epoch, start_epoch) + pad_secs
    return {t: m for t, rows in db_events.items() if (m := [r for ep, r in rows if lo <= ep <= hi])}


def match_sample_db(
    db_cache: Mapping[Path, Mapping[str, Sequence[tuple[float, dict[str, Any]]]]],
    sample_start: float | None,
    sample_end: float | None,
    sample_cmds: set[str],
) -> Path | None:
    """Select the `.session.db` in `db_cache` that best matches `[sample_start, sample_end]`."""
    if not db_cache:
        return None
    if sample_start is None or sample_end is None:
        return next(iter(db_cache)) if len(db_cache) == 1 else None
    best_db, best_score = None, 0
    for db_path, db_events in db_cache.items():
        sliced = slice_events(db_events, sample_start, sample_end, pad_secs=5.0)
        cmd_bonus = sum(
            1000 for row in sliced.get("exec_events", []) if any(c in str(row.get("command") or "") for c in sample_cmds if c)
        )
        if (score := sum(len(v) for v in sliced.values()) + cmd_bonus) > best_score:
            best_score, best_db = score, db_path
    return best_db or (next(iter(db_cache)) if len(db_cache) == 1 else None)


def read_archived_body_index(
    db_path: str | Path, *, bodies_path: str | Path | None = None
) -> dict[str, Any]:
    """Inspect `archive_state` and `event_body_blobs` in `session.db` and companion `session.bodies`."""
    path = Path(db_path)
    stem = path.name.removesuffix(".session.db")
    bname = f"{stem}.session.bodies" if path.name.endswith(".session.db") else "session.bodies"
    bpath = Path(bodies_path) if bodies_path is not None else path.with_name(bname)
    idx_tbls = load_db_tables(path, ("archive_state", "event_body_blobs"), allow_missing=("archive_state", "event_body_blobs"))
    state, blobs = idx_tbls["archive_state"], idx_tbls["event_body_blobs"]
    st = state[0] if state else {}
    gen_raw = st.get("generation_id")
    gen_hex = gen_raw.hex() if isinstance(gen_raw, (bytes, bytearray)) else None
    if gen_hex is None and bpath.is_dir():
        gen_hex = next((f.stem[2:] for f in sorted(bpath.glob("g-*.cbl"))), None)
    fmt_ver = st.get("format_version") if st.get("format_version") is not None else st.get("version")
    bodies_size = (
        sum(f.stat().st_size for f in bpath.rglob("*") if f.is_file())
        if bpath.is_dir()
        else (bpath.stat().st_size if bpath.is_file() else 0)
    )
    return {
        "session_bodies": bpath.name if bpath.exists() else None,
        "generation_id": gen_hex,
        "bodies_size_bytes": bodies_size,
        "format_version": fmt_ver,
        "blob_count": len(blobs),
        "blobs": blobs,
    }
