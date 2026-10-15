"""Shared ATIF-v1.7 step builder, turn/window enricher, and structural validator."""

from __future__ import annotations

import datetime
import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_PREVIEW_TEXT_RE = re.compile(r'"(?:text|content)"\s*:\s*"((?:\\.|[^"\\])*)')

HARBOR_EVENT_TABLES: tuple[str, ...] = (
    "exec_events", "audit_events", "net_events", "dns_events", "fs_events", "substitution_events"
)


def parse_iso_datetime(raw: Any) -> datetime.datetime | None:
    """Parse an ISO-8601 timestamp string or `datetime` into an aware UTC `datetime`."""
    if isinstance(raw, datetime.datetime):
        return raw if raw.tzinfo is not None else raw.replace(tzinfo=datetime.UTC)
    if not isinstance(raw, str) or not (s := raw.strip()):
        return None
    try:
        dt = datetime.datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith("Z") else s)
    except ValueError:
        return None
    return dt.astimezone(datetime.UTC) if dt.tzinfo is not None else dt.replace(tzinfo=datetime.UTC)


def _row_datetime(row: Mapping[str, Any]) -> datetime.datetime | None:
    if (ts := parse_iso_datetime(row.get("timestamp"))) is not None:
        return ts
    return datetime.datetime.fromtimestamp(float(ms) / 1000.0, tz=datetime.UTC) if (ms := row.get("timestamp_unix_ms")) is not None else None


def _in_window(row: Mapping[str, Any], start: datetime.datetime | None, end: datetime.datetime | None) -> bool:
    return bool((ts := _row_datetime(row)) is not None and (start is None or ts >= start) and (end is None or ts < end))


def _step_extra(step: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(step.get("extra"), dict):
        step["extra"] = {}
    return step["extra"]


def _parse_tool_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        return parsed if isinstance(parsed := json.loads(raw), dict) else {"raw": raw}
    except ValueError:
        return {"raw": raw}


def _extract_text(val: Any) -> str | None:
    if isinstance(val, str):
        return val if val.strip() else None
    if isinstance(val, Mapping):
        return _extract_text(val.get("parts") or val.get("content") or val.get("text"))
    parts = [t for i in val if isinstance(i, Mapping) and isinstance(t := i.get("text"), str) and t] if isinstance(val, Sequence) else []
    return "\n".join(parts) if parts else None


def _extract_prompts_from_request(req_body: Any) -> tuple[str | None, str | None]:
    if isinstance(req_body, Mapping):
        parsed: dict[str, Any] = dict(req_body)
    elif isinstance(req_body, str) and (s := req_body.strip()):
        try:
            loaded = json.loads(s)
            parsed = dict(loaded) if isinstance(loaded, Mapping) else {}
        except ValueError:
            if not s.startswith("{"):
                return None, s
            m = _PREVIEW_TEXT_RE.search(s)
            return (None, json.loads(f'"{raw_lit}"')) if m and (raw_lit := m.group(1).rstrip("\\")) else (None, None)
    else:
        return None, None
    src = {**(parsed["request"] if isinstance(parsed.get("request"), Mapping) else {}), **parsed}
    sys_msg = _extract_text(src.get("system") or src.get("instructions") or src.get("systemInstruction") or src.get("system_instruction"))
    user_msg = _extract_text(src.get("prompt"))
    for m in src.get("messages") or src.get("input") or ():
        if isinstance(m, Mapping) and (txt := _extract_text(m.get("content") or m.get("parts"))):
            sys_msg = txt if m.get("role") in ("system", "developer") and not sys_msg else sys_msg
            user_msg = txt if m.get("role") == "user" and not user_msg else user_msg
    for c in src.get("contents") or ():
        if isinstance(c, Mapping) and c.get("role") in (None, "user") and not user_msg:
            user_msg = _extract_text(c.get("parts"))
    return sys_msg, user_msg


def _match_tool_responses(
    cid: str, fn: str, idx: int, mc_id: int, next_mc_id: int | None,
    tool_responses: Sequence[Mapping[str, Any]], used_tresp: set[int],
) -> list[Mapping[str, Any]]:
    scopes = [
        [(i, tr) for i, tr in enumerate(tool_responses) if i not in used_tresp and int(tr.get("model_call_id") or 0) == t]
        for t in (next_mc_id, mc_id) if t is not None
    ]
    for scope in (*scopes, [(i, tr) for i, tr in enumerate(tool_responses) if i not in used_tresp]):
        if exact := [(i, tr) for i, tr in scope if str(tr.get("call_id") or "") == cid]:
            used_tresp.update(i for i, _ in exact)
            return [tr for _, tr in exact]
    for scope in scopes:
        for pred in (lambda c: c == fn or (c.startswith(f"{fn}_") and c.endswith(f"_{idx}")), lambda c: c.startswith(f"{fn}_")):
            if matched := [(i, tr) for i, tr in scope if pred(str(tr.get("call_id") or ""))]:
                used_tresp.add(matched[0][0])
                return [matched[0][1]]
        if idx < len(scope):
            used_tresp.add(scope[idx][0])
            return [scope[idx][1]]
    return []


def build_steps_from_session_tables(
    model_calls: Sequence[Mapping[str, Any]], model_items: Sequence[Mapping[str, Any]],
    tool_calls: Sequence[Mapping[str, Any]], tool_responses: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Construct ATIF-v1.7 `steps` from Capsem `model_calls`, `model_items`, `tool_calls`, and `tool_responses`."""
    items_by_mc, tcalls_by_mc = defaultdict[int, list[Mapping[str, Any]]](list), defaultdict[int, list[Mapping[str, Any]]](list)
    for item in model_items:
        items_by_mc[int(item["model_call_id"])].append(item)
    for tc in tool_calls:
        tcalls_by_mc[int(tc["model_call_id"])].append(tc)

    steps, first_mc = list[dict[str, Any]](), model_calls[0]
    first_items = items_by_mc.get(int(first_mc["id"]), [])
    req_sys, req_user = _extract_prompts_from_request(first_mc.get("request_body"))
    prev_sys, prev_user = _extract_prompts_from_request(first_mc.get("request_body_preview"))
    reqs = [str(i["content"]) for i in first_items if i.get("kind") == "request" and i.get("content")]
    sys_msg = req_sys or (str(sp) if (sp := first_mc.get("system_prompt_preview")) else prev_sys)
    user_msg = req_user or (_extract_prompts_from_request(reqs[0])[1] if reqs else None) or prev_user
    if sys_msg:
        steps.append({"step_id": len(steps) + 1, "timestamp": first_mc.get("timestamp"), "source": "system", "message": sys_msg})
    if user_msg or not steps:
        steps.append({"step_id": len(steps) + 1, "timestamp": first_mc.get("timestamp"), "source": "user", "message": user_msg or "Execute task"})

    used_tresp: set[int] = set()
    for mc_idx, mc in enumerate(model_calls):
        mc_id, mc_items = int(mc["id"]), items_by_mc.get(int(mc["id"]), [])
        next_mc_id = int(model_calls[mc_idx + 1]["id"]) if mc_idx + 1 < len(model_calls) else None
        resp_items = [str(i["content"]) for i in mc_items if i.get("kind") == "response" and i.get("content")]
        reas_items = [str(i["content"]) for i in mc_items if i.get("kind") == "reasoning" and i.get("content")]
        msg = str(mc.get("text_content") or "\n".join(resp_items) or "")
        reasoning = mc.get("thinking_content") or "\n".join(reas_items) or None
        cached, raw_cost = _parse_tool_args(mc.get("usage_details")).get("cached_tokens"), mc.get("estimated_cost_usd")
        m_ex = {"duration_ms": mc.get("duration_ms"), "stop_reason": mc.get("stop_reason"), "provider": mc.get("provider")}
        metrics = {
            "prompt_tokens": int(mc.get("input_tokens") or 0), "completion_tokens": int(mc.get("output_tokens") or 0),
            "cached_tokens": int(cached) if isinstance(cached, int) else None,
            "cost_usd": float(raw_cost) if raw_cost is not None else None, "extra": m_ex,
        }
        atif_tcalls, obs_results = list[dict[str, Any]](), list[dict[str, Any]]()
        for idx, tc in enumerate(tcalls_by_mc.get(mc_id, [])):
            cid, fn = str(tc.get("call_id") or f"call_{mc_id}_{idx}"), str(tc.get("tool_name") or "unknown")
            atif_tcalls.append({"tool_call_id": cid, "function_name": fn, "arguments": _parse_tool_args(tc.get("arguments")), "extra": {"origin": tc.get("origin")}})
            for tr in _match_tool_responses(cid, fn, idx, mc_id, next_mc_id, tool_responses, used_tresp):
                txt = str(tr.get("content_full") if tr.get("content_full") is not None else (tr.get("content_preview") or ""))
                obs_ex = {"is_error": bool(tr.get("is_error")), **({"truncated": bool(tr["truncated"])} if tr.get("truncated") is not None else {}), **({"body_hash": tr["body_hash"]} if tr.get("body_hash") else {})}
                obs_results.append({"source_call_id": cid, "content": txt, "extra": obs_ex})

        mc_ex: dict[str, Any] = {
            "turn_id": mc.get("turn_id"), "capsem_model_call_id": mc_id,
            "messages_count": mc.get("messages_count"), "tools_count": mc.get("tools_count"),
            **({"truncated": True} if mc.get("truncated") else {}),
        }
        if mc.get("request_body") or mc.get("response_body"):
            mc_ex["llm_call"] = {k: _parse_tool_args(mc[f"{k}_body"]) for k in ("request", "response") if mc.get(f"{k}_body")}
        step: dict[str, Any] = {
            "step_id": len(steps) + 1, "source": "agent", "timestamp": mc.get("timestamp"),
            "model_name": mc.get("model"), "message": msg, "metrics": metrics, "extra": mc_ex,
            **({"reasoning_content": str(reasoning)} if reasoning else {}),
            **({"tool_calls": atif_tcalls} if atif_tcalls else {}),
            **({"observation": {"results": obs_results}} if obs_results else {}),
        }
        steps.append(step)
    return steps


def _mc_start_dt(mc: Mapping[str, Any]) -> datetime.datetime | None:
    ts = parse_iso_datetime(mc.get("timestamp"))
    return ts - datetime.timedelta(milliseconds=max(0.0, float(mc.get("duration_ms") or 0))) if ts else None


def enrich_steps_by_turn_id(
    steps: list[dict[str, Any]], model_calls: Sequence[Mapping[str, Any]],
    events_by_table: Mapping[str, Sequence[Mapping[str, Any]]], *,
    event_tables: Sequence[str] = HARBOR_EVENT_TABLES,
) -> None:
    """Attach `capsem_model_call` and `capsem_events` to agent steps using `[call_i, call_{i+1})` windows and `turn_id`."""
    if not (agent_steps := [s for s in steps if s.get("source") == "agent"]):
        return
    calls_by_turn, turn_order = dict[str, list[Mapping[str, Any]]](), list[str]()
    for mc in model_calls:
        if (tid := str(mc.get("turn_id") or "").strip()) and tid not in calls_by_turn:
            calls_by_turn[tid] = []
            turn_order.append(tid)
        if tid:
            calls_by_turn[tid].append(mc)

    call_dts, call_end_dts = [_mc_start_dt(mc) for mc in model_calls], [parse_iso_datetime(mc.get("timestamp")) for mc in model_calls]
    is_synth = len(agent_steps) == len(model_calls) and all(
        isinstance(s.get("extra"), Mapping) and s["extra"].get("capsem_model_call_id") is not None for s in agent_steps
    )
    if len(turn_order) <= 1 and len(model_calls) > 1 and len(agent_steps) > 1:
        j_mode = "timestamp_window" if is_synth else "ordinal_fallback"
        for idx, step in enumerate(agent_steps[: len(model_calls)]):
            mc, win_s, win_e = model_calls[idx], call_dts[idx], (call_dts[idx + 1] if idx + 1 < len(call_dts) else None)
            matched: dict[str, Any] = {"join_mode": j_mode, "call_index": idx, "turn_id": mc.get("turn_id")}
            for tbl in event_tables:
                rows = list(events_by_table.get(tbl, ()))
                matched[tbl] = [r for r in rows if _in_window(r, win_s, win_e)] if win_s else rows
            _step_extra(step).update(capsem_model_call=dict(mc), capsem_events=matched)
        return

    turn_spans = {
        t: (call_dts[min(idxs)], call_dts[max(idxs) + 1] if max(idxs) + 1 < len(call_dts) else None)
        for t in turn_order if (idxs := [i for i, m in enumerate(model_calls) if str(m.get("turn_id") or "").strip() == t])
    }
    for idx, step in enumerate(agent_steps):
        step_extra = _step_extra(step)
        ex_tid = step_extra.get("turn_id")
        fb_tid = str(model_calls[idx]["turn_id"]) if idx < len(model_calls) and model_calls[idx].get("turn_id") else None
        if not (tid := ex_tid if isinstance(ex_tid, str) and ex_tid in calls_by_turn else (turn_order[idx] if idx < len(turn_order) else fb_tid)):
            continue
        t_calls = calls_by_turn.get(tid, [])
        step_extra["capsem_model_call"] = dict(t_calls[0]) if len(t_calls) == 1 else [dict(c) for c in t_calls]
        matched = {"join_mode": "turn_id", "turn_id": tid}
        s_dt, next_dt = (call_dts[idx] if idx < len(call_dts) else None), (call_dts[idx + 1] if idx + 1 < len(call_dts) else None)
        end_cap = (end_dt + datetime.timedelta(milliseconds=50)) if idx < len(call_end_dts) and (end_dt := call_end_dts[idx]) else None
        has_tools = bool(step.get("tool_calls"))
        for tbl in event_tables:
            rows = events_by_table.get(tbl, ())
            ambient = {
                t for t, (ts_s, ts_e) in turn_spans.items()
                if len(turn_order) > 1 and any(str(r.get("turn_id") or "") == t and _row_datetime(r) and not _in_window(r, ts_s, ts_e) for r in rows)
            }
            e_dt = next_dt if next_dt is not None else (end_cap if ambient and not has_tools else None)
            valid_tids = set(calls_by_turn) - ambient
            if valid_tids & {str(r.get("turn_id") or "") for r in rows}:
                matched[tbl] = [
                    r for r in rows
                    if ((rt := str(r.get("turn_id") or "")) == tid and rt in valid_tids)
                    or (rt not in valid_tids and bool(s_dt) and _in_window(r, s_dt, e_dt))
                ]
            elif tbl == "substitution_events":
                matched[tbl] = list(rows) if idx == 0 else []
            elif ambient and not has_tools and tbl in ("audit_events", "exec_events", "fs_events"):
                matched[tbl] = []
            else:
                matched[tbl] = [r for r in rows if _in_window(r, s_dt, e_dt)] if s_dt else []
        step_extra["capsem_events"] = matched


def enrich_steps_by_timestamp_window(
    steps: list[dict[str, Any]], events_by_table: Mapping[str, Sequence[Mapping[str, Any]]], *,
    event_tables: Sequence[str] = HARBOR_EVENT_TABLES,
) -> None:
    """Attach `capsem_events` to steps using `[step_i.timestamp, step_{i+1}.timestamp)`."""
    step_dts = [parse_iso_datetime(s.get("timestamp")) for s in steps]
    for idx, step in enumerate(steps):
        start_dt = step_dts[idx]
        end_dt = next((step_dts[j] for j in range(idx + 1, len(steps)) if step_dts[j]), None)
        matched = {tbl: [r for r in events_by_table.get(tbl, ()) if _in_window(r, start_dt, end_dt)] for tbl in event_tables}
        if step.get("source") == "agent" or any(matched.values()):
            w_end = steps[idx + 1].get("timestamp") if idx + 1 < len(steps) else None
            _step_extra(step)["capsem_events"] = {"join_mode": "timestamp_window", "window_start": step.get("timestamp"), "window_end": w_end, **matched}


def attach_capsem_summary(
    trajectory_data: dict[str, Any], vm_id: str, db_path: str | Path, bodies_path: str | Path | None,
    drop_security_tables: bool, model_calls: Sequence[Mapping[str, Any]],
    events_by_table: Mapping[str, Sequence[Mapping[str, Any]]], output_path: str | Path | None = None,
    *, bodies: str = "previews", join_mode: str | None = None,
) -> dict[str, Any]:
    """Attach top-level `extra.capsem` metadata to `trajectory_data` and optionally write it to disk."""
    b_p = Path(bodies_path) if bodies_path else None
    counts = {"model_calls": len(model_calls), **{t: len(r) for t, r in events_by_table.items()}}
    summary: dict[str, Any] = {
        "vm_id": vm_id, "join_mode": join_mode or ("turn_id" if model_calls else "timestamp_window"),
        "bodies": bodies, "session_db": Path(db_path).name,
        "session_bodies": b_p.name if b_p and b_p.exists() else None,
        "drop_security_tables": drop_security_tables, "dropped_security_tables": drop_security_tables,
        "model_calls_count": len(model_calls),
        "total_input_tokens": sum(int(m.get("input_tokens") or 0) for m in model_calls),
        "total_output_tokens": sum(int(m.get("output_tokens") or 0) for m in model_calls),
        "total_estimated_cost_usd": round(sum(float(m.get("estimated_cost_usd") or 0.0) for m in model_calls), 8),
        "event_counts": counts, **{f"{t}_count": len(r) for t, r in events_by_table.items()},
    }
    extra = _step_extra(trajectory_data)
    extra["capsem_summary"] = extra["capsem"] = summary
    if output_path is not None:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_text(json.dumps(trajectory_data, indent=2) + "\n", encoding="utf-8")
    return trajectory_data
