"""Shared Capsem `session.db` / `session.bodies` reader and ATIF-v1.7 trace helpers."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ._trace_atif import (
    HARBOR_EVENT_TABLES,
    attach_capsem_summary,
    build_steps_from_session_tables,
    enrich_steps_by_timestamp_window,
    enrich_steps_by_turn_id,
    parse_iso_datetime,
)
from ._trace_bodies import (
    VALID_BODIES_MODES,
    BodiesMode,
    BodyReader,
    HttpBodyReader,
    candidate_body_event_ids,
    decode_security_payload,
    hydrate_tables_from_bodies,
)
from ._trace_ledger import (
    CAPSEM_EVENT_TABLES,
    DROPPED_SECURITY_TABLES,
    export_session_ledger,
    find_session_db,
    format_iso_utc,
    load_db_events,
    load_db_tables,
    match_sample_db,
    parse_iso_epoch,
    read_archived_body_index,
    should_drop_security_tables,
    slice_events,
)
from .errors import ExportUnavailableError


def build_exec_only_steps(
    exec_events: Sequence[Mapping[str, Any]], *, vm_id: str, bodies: str = "previews"
) -> list[dict[str, Any]]:
    """Construct ATIF-v1.7 `steps` from `exec_events` when `model_calls` is empty."""
    first_ts = exec_events[0].get("timestamp") if exec_events else None
    steps: list[dict[str, Any]] = [
        {"step_id": 1, "timestamp": first_ts, "source": "user", "message": f"Execute commands in Capsem VM {vm_id}"}
    ]
    for ex in exec_events:
        cmd = str(ex.get("command") or "")
        s_ex = {
            "exit_code": ex.get("exit_code"),
            "duration_ms": ex.get("duration_ms"),
            "exec_id": ex.get("exec_id"),
            "event_id": ex.get("event_id"),
            "turn_id": ex.get("turn_id"),
        }
        step: dict[str, Any] = {
            "step_id": len(steps) + 1,
            "timestamp": ex.get("timestamp"),
            "source": "agent",
            "llm_call_count": 0,
            "message": cmd,
            "extra": s_ex,
        }
        if bodies != "none":
            out = str(ex.get("stdout") if ex.get("stdout") is not None else (ex.get("stdout_preview") or ""))
            err = str(ex.get("stderr") if ex.get("stderr") is not None else (ex.get("stderr_preview") or ""))
            content = f"{out}\n{err}".strip("\n") if (out and err) else (out or err)
            obs_ex = {"exit_code": ex.get("exit_code"), "duration_ms": ex.get("duration_ms")}
            step["observation"] = {"results": [{"content": content, "extra": obs_ex}]}
        steps.append(step)
    return steps


def session_db_to_atif(
    db_path: str | Path, *, vm_id: str, session_id: str | None = None,
    bodies_path: str | Path | None = None, drop_security_tables: bool = True,
    include_security_events: bool | None = None, bodies: BodiesMode = "previews",
    body_reader: BodyReader | None = None, output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Synthesize an `ATIF-v1.7` trajectory dict from `session.db` (`model_calls` or `exec_events`-only)."""
    if bodies not in VALID_BODIES_MODES:
        raise ValueError(f"Invalid bodies mode {bodies!r}; expected one of {VALID_BODIES_MODES}")
    inc_sec = (not drop_security_tables) if include_security_events is None else bool(include_security_events)
    eff_drop = (not inc_sec) if include_security_events is not None else drop_security_tables
    ev_tbls = (*HARBOR_EVENT_TABLES, *(DROPPED_SECURITY_TABLES if inc_sec else ()))
    all_tbls = ("model_calls", "model_items", "tool_calls", "tool_responses", "event_body_blobs", *ev_tbls)
    tables = load_db_tables(db_path, all_tbls, allow_missing=(*DROPPED_SECURITY_TABLES, "event_body_blobs"))
    hydrate_tables_from_bodies(
        tables, tables["event_body_blobs"], bodies=bodies, body_reader=body_reader, include_security_events=inc_sec
    )
    model_calls, exec_rows = tables["model_calls"], tables["exec_events"]
    if not model_calls and not exec_rows:
        raise ValueError(f"Cannot synthesize ATIF trajectory: no model_calls or exec_events in {db_path}")
    events = {tbl: tables[tbl] for tbl in ev_tbls}
    if model_calls:
        steps = build_steps_from_session_tables(
            model_calls, tables["model_items"], tables["tool_calls"], tables["tool_responses"]
        )
        enrich_steps_by_turn_id(steps, model_calls, events, event_tables=ev_tbls)
        first_mc = model_calls[0]
        agent_name = str(first_mc.get("process_name") or first_mc.get("provider") or "capsem-agent")
        model_name = str(first_mc["model"]) if first_mc.get("model") else None
        jmode = "turn_id"
    else:
        steps = build_exec_only_steps(exec_rows, vm_id=vm_id, bodies=bodies)
        enrich_steps_by_timestamp_window(steps, events, event_tables=ev_tbls)
        agent_name = str(exec_rows[0].get("process_name") or "capsem-exec")
        model_name, jmode = None, "exec_only"
    cost_usd = round(sum(float(m.get("estimated_cost_usd") or 0.0) for m in model_calls), 8)
    traj: dict[str, Any] = {
        "schema_version": "ATIF-v1.7",
        "session_id": session_id or vm_id,
        "trajectory_id": f"capsem-{vm_id}",
        "agent": {"name": agent_name, "version": "0.7", **({"model_name": model_name} if model_name else {})},
        "steps": steps,
        "final_metrics": {
            "total_prompt_tokens": sum(int(m.get("input_tokens") or 0) for m in model_calls),
            "total_completion_tokens": sum(int(m.get("output_tokens") or 0) for m in model_calls),
            "total_cost_usd": cost_usd,
            "total_steps": len(steps),
        },
    }
    return attach_capsem_summary(
        traj, vm_id, db_path, bodies_path, eff_drop, model_calls, events, output_path, bodies=bodies, join_mode=jmode
    )


def enrich_atif_trajectory(
    trajectory_data: dict[str, Any], db_path: str | Path, *, vm_id: str,
    bodies_path: str | Path | None = None, drop_security_tables: bool = True,
    include_security_events: bool | None = None, bodies: BodiesMode = "previews",
    body_reader: BodyReader | None = None, output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Enrich an ATIF-v1.7 trajectory dict in-place with Capsem `session.db` events and model calls."""
    if bodies not in VALID_BODIES_MODES:
        raise ValueError(f"Invalid bodies mode {bodies!r}; expected one of {VALID_BODIES_MODES}")
    inc_sec = (not drop_security_tables) if include_security_events is None else bool(include_security_events)
    eff_drop = (not inc_sec) if include_security_events is not None else drop_security_tables
    ev_tbls = (*HARBOR_EVENT_TABLES, *(DROPPED_SECURITY_TABLES if inc_sec else ()))
    tables = load_db_tables(
        db_path, ("model_calls", "tool_responses", "event_body_blobs", *ev_tbls),
        allow_missing=(*DROPPED_SECURITY_TABLES, "tool_responses", "event_body_blobs"),
    )
    hydrate_tables_from_bodies(
        tables, tables["event_body_blobs"], bodies=bodies, body_reader=body_reader, include_security_events=inc_sec
    )
    model_calls, events = tables["model_calls"], {t: tables[t] for t in ev_tbls}
    if isinstance(steps := trajectory_data.get("steps"), list):
        if model_calls:
            enrich_steps_by_turn_id(steps, model_calls, events, event_tables=ev_tbls)
        else:
            enrich_steps_by_timestamp_window(steps, events, event_tables=ev_tbls)
    return attach_capsem_summary(
        trajectory_data, vm_id, db_path, bodies_path, eff_drop, model_calls, events, output_path, bodies=bodies
    )


def validate_atif_trajectory(trajectory_data: Mapping[str, Any], *, label: str = "<trajectory>") -> list[str]:
    """Validate structural ATIF-v1.7 invariants using stdlib only."""
    errors: list[str] = []
    if (ver := trajectory_data.get("schema_version")) != "ATIF-v1.7":
        errors.append(f"{label}: expected schema_version 'ATIF-v1.7', got {ver!r}")
    if not str(trajectory_data.get("session_id") or "").strip():
        errors.append(f"{label}: missing non-empty session_id")
    agent = trajectory_data.get("agent")
    if not isinstance(agent, Mapping) or not agent.get("name") or not agent.get("version"):
        errors.append(f"{label}: agent must have 'name' and 'version'")
    steps = trajectory_data.get("steps")
    if not isinstance(steps, list) or not steps:
        return [*errors, f"{label}: steps must be a non-empty list"]
    for idx, step in enumerate(steps, 1):
        if not isinstance(step, Mapping):
            errors.append(f"{label}: step[{idx}] must be an object")
            continue
        if step.get("step_id") != idx:
            errors.append(f"{label}: step[{idx}] has step_id={step.get('step_id')!r}")
        if step.get("source") not in ("system", "user", "agent"):
            errors.append(f"{label}: step[{idx}] invalid source={step.get('source')!r}")
        if not isinstance(step.get("message"), str):
            errors.append(f"{label}: step[{idx}] missing string 'message'")
        extra = step.get("extra")
        if isinstance(extra, Mapping) and "llm_call" in extra and "attachment://" in json.dumps(extra["llm_call"]):
            errors.append(f"{label}: step[{idx}] has unresolved attachment:// pointer")
    return errors


__all__ = [
    "CAPSEM_EVENT_TABLES",
    "DROPPED_SECURITY_TABLES",
    "HARBOR_EVENT_TABLES",
    "VALID_BODIES_MODES",
    "BodiesMode",
    "BodyReader",
    "ExportUnavailableError",
    "HttpBodyReader",
    "attach_capsem_summary",
    "build_exec_only_steps",
    "build_steps_from_session_tables",
    "candidate_body_event_ids",
    "decode_security_payload",
    "enrich_atif_trajectory",
    "enrich_steps_by_timestamp_window",
    "enrich_steps_by_turn_id",
    "export_session_ledger",
    "find_session_db",
    "format_iso_utc",
    "load_db_events",
    "load_db_tables",
    "match_sample_db",
    "parse_iso_datetime",
    "parse_iso_epoch",
    "read_archived_body_index",
    "session_db_to_atif",
    "should_drop_security_tables",
    "slice_events",
    "validate_atif_trajectory",
]
