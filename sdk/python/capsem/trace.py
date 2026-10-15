"""Session ledger (`session.db` / `session.bodies`) export and ATIF-v1.7 trajectory synthesis."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import __version__
from ._trace_atif import build_steps_from_session_tables
from ._trace_bodies import (
    BodiesMode,
    BodyReader,
    HttpBodyReader,
    candidate_body_event_ids,
    decode_security_payload,
    hydrate_tables_from_bodies,
    validate_event_id,
)
from ._trace_enrich import (
    attach_capsem_summary,
    enrich_steps_by_timestamp_window,
    enrich_steps_by_turn_id,
    validate_atif_trajectory,
)
from ._trace_ledger import (
    DROPPED_SECURITY_TABLES,
    EXPORT_PERSIST_PREFIX,
    HARBOR_EVENT_TABLES,
    export_session_ledger,
    find_session_db,
    has_session_events,
    is_export_persisted_vm,
    load_db_tables,
    parse_iso_datetime,
    quiesce_vm_for_export,
    resolve_include_security_events,
)

_ATIF_LOAD_TABLES = (
    "model_calls",
    "model_items",
    "tool_calls",
    "tool_responses",
    "event_body_blobs",
    *HARBOR_EVENT_TABLES,
)


def _build_exec_only_steps(exec_events: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Synthesize ATIF-v1.7 steps from `exec_events` when a VM run made zero `model_calls`."""
    if not exec_events:
        return []
    steps: list[dict[str, Any]] = [
        {
            "step_id": 1,
            "timestamp": exec_events[0].get("timestamp"),
            "source": "user",
            "message": "(Capsem VM session without intercepted model_calls)",
        }
    ]
    for idx, ex in enumerate(exec_events, start=2):
        cid = f"exec_{ex.get('id') or idx - 1}"
        cmd, args_str = str(ex.get("command") or ""), str(ex.get("args") or "")
        out = str(ex.get("stdout") or ex.get("stdout_preview") or "")
        err = str(ex.get("stderr") or ex.get("stderr_preview") or "")
        combined = f"{out}\n{err}".strip() if out and err else (out or err)
        res_obj: dict[str, Any] = {"source_call_id": cid, "content": combined}
        if int(ex.get("exit_code") or 0) != 0:
            res_obj["extra"] = {"is_error": True}
        tcall: dict[str, Any] = {
            "tool_call_id": cid,
            "function_name": "bash",
            "arguments": {"command": cmd, "args": args_str},
        }
        steps.append(
            {
                "step_id": idx,
                "timestamp": ex.get("timestamp"),
                "source": "agent",
                "message": f"{cmd} {args_str}".strip() if args_str else cmd,
                "llm_call_count": 0,
                "tool_calls": [tcall],
                "observation": {"results": [res_obj]},
                "extra": {"capsem_events": {"join_mode": "exec_only", "exec_events": [ex]}},
            }
        )
    return steps


def _prepare_atif_tables(
    db_path: Path,
    *,
    bodies: BodiesMode,
    include_security_events: bool | None,
    body_reader: BodyReader | None,
    preloaded_tables: dict[str, list[dict[str, Any]]] | None,
) -> tuple[bool, tuple[str, ...], dict[str, list[dict[str, Any]]]]:
    if bodies not in ("none", "previews", "http", "full"):
        raise ValueError(
            f"Invalid bodies mode {bodies!r}; expected 'none', 'previews', 'http', or 'full'"
        )
    keep_sec = resolve_include_security_events(include_security_events=include_security_events)
    sec_tbls = DROPPED_SECURITY_TABLES if keep_sec else ()
    ev_tables = (*HARBOR_EVENT_TABLES, *sec_tbls)
    tables = (
        preloaded_tables
        if preloaded_tables is not None
        else load_db_tables(db_path, (*_ATIF_LOAD_TABLES, *sec_tbls))
    )
    if body_reader is not None and bodies in ("previews", "http", "full"):
        hydrate_tables_from_bodies(tables, body_reader, full=(bodies == "full"))
    return not keep_sec, ev_tables, tables


def session_db_to_atif(
    db_path: str | Path,
    *,
    vm_id: str,
    session_id: str | None = None,
    agent_name: str | None = None,
    agent_version: str | None = None,
    bodies_path: str | Path | None = None,
    include_security_events: bool | None = None,
    bodies: BodiesMode = "previews",
    body_reader: BodyReader | None = None,
    output_path: str | Path | None = None,
    _preloaded_tables: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Synthesize a standalone `ATIF-v1.7` trajectory from `session.db`."""
    db_p = Path(db_path)
    bodies_p = Path(bodies_path) if bodies_path is not None else (db_p.parent / "session.bodies")
    eff_drop, ev_tables, tables = _prepare_atif_tables(
        db_p,
        bodies=bodies,
        include_security_events=include_security_events,
        body_reader=body_reader,
        preloaded_tables=_preloaded_tables,
    )
    mcalls = tables["model_calls"]
    events = {t: tables[t] for t in ev_tables}
    if mcalls:
        steps = build_steps_from_session_tables(
            mcalls,
            tables["model_items"],
            tables["tool_calls"],
            tables["tool_responses"],
            include_llm_call=(bodies == "full"),
        )
        enrich_steps_by_turn_id(steps, mcalls, events)
        last_ev = steps[-1].get("extra", {}).get("capsem_events", {}) if steps else {}
        join_mode = str(last_ev.get("join_mode", "turn_id"))
        first = mcalls[0]
        agent_obj: dict[str, Any] = {
            "name": agent_name or str(first.get("process_name") or "capsem-vm-agent"),
            "version": agent_version or __version__,
            "model_name": first.get("model"),
        }
        tot_in = sum(int(m.get("input_tokens") or 0) for m in mcalls)
        tot_out = sum(int(m.get("output_tokens") or 0) for m in mcalls)
        costs = [float(m["estimated_cost_usd"]) for m in mcalls if m.get("estimated_cost_usd")]
    elif tables["exec_events"]:
        steps = _build_exec_only_steps(tables["exec_events"])
        join_mode = "exec_only"
        first_ex = tables["exec_events"][0]
        agent_obj = {
            "name": agent_name or str(first_ex.get("process_name") or "capsem-vm-exec"),
            "version": agent_version or __version__,
        }
        tot_in, tot_out, costs = 0, 0, []
    else:
        raise ValueError(f"session.db at {db_p} contains no model_calls or exec_events")

    traj: dict[str, Any] = {
        "schema_version": "ATIF-v1.7",
        "session_id": session_id or vm_id,
        "agent": agent_obj,
        "steps": steps,
        "final_metrics": {
            "total_prompt_tokens": tot_in,
            "total_completion_tokens": tot_out,
            "total_cost_usd": round(sum(costs), 6) if costs else None,
            "total_steps": len(steps),
        },
    }
    attach_capsem_summary(
        traj,
        vm_id=vm_id,
        db_path=db_p,
        bodies_path=bodies_p if bodies_p.is_dir() else None,
        model_calls_count=len(mcalls),
        events=events,
        dropped_security_tables=eff_drop,
        join_mode=join_mode,
        bodies=bodies,
    )
    errs = validate_atif_trajectory(traj)
    if errs:
        raise ValueError(f"Synthesized ATIF trajectory failed validation: {errs}")
    if output_path is not None:
        out_p = Path(output_path)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(json.dumps(traj, indent=2) + "\n", encoding="utf-8")
    return traj


def enrich_atif_trajectory(
    trajectory_data: dict[str, Any],
    db_path: str | Path,
    *,
    vm_id: str,
    bodies_path: str | Path | None = None,
    include_security_events: bool | None = None,
    bodies: BodiesMode = "previews",
    body_reader: BodyReader | None = None,
    output_path: str | Path | None = None,
    _preloaded_tables: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Enrich an existing `ATIF-v1.7` trajectory dict with `session.db` events."""
    db_p = Path(db_path)
    bodies_p = Path(bodies_path) if bodies_path is not None else (db_p.parent / "session.bodies")
    eff_drop, ev_tables, tables = _prepare_atif_tables(
        db_p,
        bodies=bodies,
        include_security_events=include_security_events,
        body_reader=body_reader,
        preloaded_tables=_preloaded_tables,
    )
    mcalls = tables["model_calls"]
    events = {t: tables[t] for t in ev_tables}
    steps: list[dict[str, Any]] = trajectory_data.get("steps", [])
    if mcalls:
        enrich_steps_by_turn_id(steps, mcalls, events)
        agent_steps = [s for s in steps if isinstance(s, Mapping) and s.get("source") == "agent"]
        first_ev = agent_steps[0].get("extra", {}).get("capsem_events", {}) if agent_steps else {}
        join_mode = str(first_ev.get("join_mode", "turn_id"))
        if not isinstance(trajectory_data.get("final_metrics"), Mapping):
            costs = [float(m["estimated_cost_usd"]) for m in mcalls if m.get("estimated_cost_usd")]
            trajectory_data["final_metrics"] = {
                "total_prompt_tokens": sum(int(m.get("input_tokens") or 0) for m in mcalls),
                "total_completion_tokens": sum(int(m.get("output_tokens") or 0) for m in mcalls),
                "total_cost_usd": round(sum(costs), 6) if costs else None,
                "total_steps": len(steps),
            }
    else:
        enrich_steps_by_timestamp_window(steps, events)
        join_mode = "timestamp_window"
    attach_capsem_summary(
        trajectory_data,
        vm_id=vm_id,
        db_path=db_p,
        bodies_path=bodies_p if bodies_p.is_dir() else None,
        model_calls_count=len(mcalls),
        events=events,
        dropped_security_tables=eff_drop,
        join_mode=join_mode,
        bodies=bodies,
    )
    errs = validate_atif_trajectory(trajectory_data)
    if errs:
        raise ValueError(f"Enriched ATIF trajectory failed validation: {errs}")
    if output_path is not None:
        out_p = Path(output_path)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(json.dumps(trajectory_data, indent=2) + "\n", encoding="utf-8")
    return trajectory_data


__all__ = [
    "DROPPED_SECURITY_TABLES",
    "EXPORT_PERSIST_PREFIX",
    "HARBOR_EVENT_TABLES",
    "BodiesMode",
    "BodyReader",
    "HttpBodyReader",
    "attach_capsem_summary",
    "build_steps_from_session_tables",
    "candidate_body_event_ids",
    "decode_security_payload",
    "enrich_atif_trajectory",
    "enrich_steps_by_timestamp_window",
    "enrich_steps_by_turn_id",
    "export_session_ledger",
    "find_session_db",
    "has_session_events",
    "hydrate_tables_from_bodies",
    "is_export_persisted_vm",
    "load_db_tables",
    "parse_iso_datetime",
    "quiesce_vm_for_export",
    "resolve_include_security_events",
    "session_db_to_atif",
    "validate_atif_trajectory",
    "validate_event_id",
]
