"""Hermetic unit tests for `capsem.trace` against the canonical 0.7 `CREATE_SCHEMA`."""

from __future__ import annotations

import base64
import datetime as dt
import json
import sqlite3
import struct
from pathlib import Path
from typing import Any, cast

import pytest
from capsem.trace import (
    CAPSEM_EVENT_TABLES,
    DROPPED_SECURITY_TABLES,
    build_steps_from_session_tables,
    decode_security_payload,
    enrich_atif_trajectory,
    enrich_steps_by_timestamp_window,
    enrich_steps_by_turn_id,
    export_session_ledger,
    find_session_db,
    format_iso_utc,
    load_db_events,
    load_db_tables,
    match_sample_db,
    parse_iso_datetime,
    parse_iso_epoch,
    read_archived_body_index,
    session_db_to_atif,
    should_drop_security_tables,
    slice_events,
    validate_atif_trajectory,
)

_DDL_PATH = Path(__file__).resolve().parents[3] / "crates/capsem-logger/src/schema/ddl.rs"
_CREATE_SCHEMA = (
    _DDL_PATH.read_text(encoding="utf-8")
    .split('pub const CREATE_SCHEMA: &str = "', 1)[1]
    .split('";', 1)[0]
)
_HASH_A = "blake3:" + "a" * 64
_HASH_B = "blake3:" + "b" * 64
_CRED_REF = "credential:blake3:" + "c" * 64


def populate_real_session_db(
    base_dir: Path, vm_id: str, *, with_models: bool = True, with_exec: bool = True, single_turn: bool = False
) -> Path:
    sdir = base_dir / "sessions" / vm_id
    sdir.mkdir(parents=True, exist_ok=True)
    db_path, bodies_dir = sdir / "session.db", sdir / "session.bodies"
    bodies_dir.mkdir(parents=True, exist_ok=True)
    gen_id = bytes.fromhex("0123456789abcdef0123456789abcdef")
    stale_id = bytes.fromhex("fedcba9876543210fedcba9876543210")
    payload = b'{"messages":[{"role":"user","content":"fix git"}]}'
    (bodies_dir / f"g-{gen_id.hex()}.cbl").write_bytes(payload)
    (bodies_dir / f"g-{stale_id.hex()}.cbl").write_bytes(b"stale")
    tid2 = "turn-1" if single_turn else "turn-2"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(_CREATE_SCHEMA)
        conn.execute("INSERT INTO archive_state VALUES (1, ?, ?, 4, 128, 1)", (gen_id, gen_id))
        conn.execute("INSERT INTO body_blocks VALUES (80, 256, 256, '2026-10-14T08:00:01Z')")
        if with_models:
            conn.execute(
                "INSERT INTO model_calls (id, event_id, timestamp, provider, protocol, model, "
                "process_name, method, path, system_prompt_preview, messages_count, tools_count, "
                "text_content, thinking_content, stop_reason, input_tokens, output_tokens, "
                "usage_details, duration_ms, estimated_cost_usd, request_body_preview, turn_id) VALUES "
                "(1, '000000000001', '2026-10-14T08:00:01Z', 'google', 'google', 'gemini-3.1-pro', "
                "'gemini-cli', 'POST', '/v1/m', 'You are helpful.', 1, 1, NULL, NULL, 'tool_use', "
                "120, 40, '{\"cached_tokens\":20}', 450, 0.002, '{\"req\":', 'turn-1'), "
                "(2, '000000000002', '2026-10-14T08:00:05Z', 'google', 'google', 'gemini-3.1-pro', "
                "'gemini-cli', 'POST', '/v1/m', NULL, 3, 1, 'Fixed.', 'Done thinking.', 'end_turn', "
                "160, 20, 'not-json', 300, 0.001, NULL, ?)",
                (tid2,),
            )
            conn.execute(
                "INSERT INTO model_items (event_id, model_call_id, timestamp, provider, model, path, "
                "trace_id, turn_id, kind, item_index, call_id, content, content_hash) VALUES "
                "('000000000011', 1, '2026-10-14T08:00:01Z', 'google', 'm', '/v1/m', 'tr-1', 'turn-1', "
                "'reasoning', 0, '', 'Check git status', ?), "
                "('000000000012', 1, '2026-10-14T08:00:01Z', 'google', 'm', '/v1/m', 'tr-1', 'turn-1', "
                "'response', 1, '', 'Running git status', ?)",
                (_HASH_A, _HASH_B),
            )
            conn.execute(
                "INSERT INTO tool_calls (id, event_id, timestamp, model_call_id, provider, status, "
                "call_index, call_id, tool_name, arguments, response_preview, origin, transport, turn_id) "
                "VALUES (1, '000000000021', '2026-10-14T08:00:02Z', 1, 'google', 'responded', 0, "
                "'call-1', 'bash', '{\"cmd\":\"git status\"}', 'On branch', 'native', 'http', 'turn-1')"
            )
            conn.execute(
                "INSERT INTO tool_responses (id, event_id, model_call_id, call_id, content_preview, "
                "is_error, turn_id) VALUES (1, '000000000031', 2, 'call-1', 'On branch', 0, 'turn-1')"
            )
            conn.execute(
                "INSERT INTO event_body_blobs (event_id, event_type, source_table, direction, "
                "content_type, original_bytes, stored_bytes, truncated, body_hash, block_offset, "
                "body_offset, body_len, trace_id, turn_id, created_at) VALUES "
                "('000000000001', 'model.call', 'model_calls', 'request', 'application/json', "
                "64, 50, 1, ?, 80, 0, 50, 'tr-1', 'turn-1', '2026-10-14T08:00:01Z'), "
                "('000000000001', 'model.call', 'model_calls', 'response', 'application/json', "
                "64, 50, 1, ?, 80, 50, 50, 'tr-1', 'turn-1', '2026-10-14T08:00:01Z'), "
                "('000000000031', 'model.call', 'tool_responses', 'response', 'text/plain', "
                "32, 24, 1, ?, 80, 100, 24, 'tr-1', 'turn-1', '2026-10-14T08:00:02Z')",
                (_HASH_A, _HASH_B, _HASH_A),
            )
        if with_exec:
            conn.execute(
                "INSERT INTO exec_events (id, event_id, timestamp, exec_id, command, exit_code, "
                "duration_ms, stdout_preview, stderr_preview, target, turn_id, process_name) VALUES "
                "(1, '000000000041', '2026-10-14T08:00:02Z', 1, 'git status', 0, 12, 'On branch', '', "
                "'workload', 'trace-exec-1', 'bash'), "
                "(2, '000000000042', '2026-10-14T08:00:06Z', 2, 'git checkout -b fix', 0, 15, "
                "'Switched', '', 'workload', 'trace-exec-2', 'bash')"
            )
            conn.execute(
                "INSERT INTO event_body_blobs (event_id, event_type, source_table, direction, "
                "content_type, original_bytes, stored_bytes, truncated, body_hash, block_offset, "
                "body_offset, body_len, trace_id, turn_id, created_at) VALUES "
                "('000000000041', 'process.exec_complete', 'exec_events', 'stdout', 'text/plain', "
                "20, 20, 0, ?, 80, 124, 20, 'tr-1', 'trace-exec-1', '2026-10-14T08:00:02Z')",
                (_HASH_A,),
            )
        conn.execute(
            "INSERT INTO fs_events (event_id, timestamp, action, path, kind, turn_id) VALUES "
            "('000000000051', '2026-10-14T08:00:03Z', 'modified', '/repo/a', 'file', 'turn-1')"
        )
        conn.execute(
            "INSERT INTO net_events (event_id, timestamp, domain, decision, status_code, "
            "request_body_preview, turn_id) VALUES "
            "('000000000061', '2026-10-14T08:00:01Z', 'generativelanguage.googleapis.com', "
            "'allowed', 200, '{\"net\":1}', 'turn-1')"
        )
        conn.execute(
            "INSERT INTO dns_events (event_id, timestamp, qname, qtype, qclass, rcode, decision, turn_id) "
            "VALUES ('000000000071', '2026-10-14T08:00:01Z', 'api.google.com', 1, 1, 0, 'allowed', 'turn-1')"
        )
        conn.execute(
            "INSERT INTO audit_events (event_id, timestamp, pid, ppid, uid, exe, argv, turn_id) "
            "VALUES ('000000000081', '2026-10-14T08:00:02Z', 10, 1, 0, '/bin/git', 'git status', 'turn-1')"
        )
        conn.execute(
            "INSERT INTO substitution_events (event_id, timestamp, material_class, source, "
            "algorithm, substitution_ref, outcome, provider, turn_id) VALUES "
            "('000000000091', '2026-10-14T08:00:00Z', 'api_key', 'env', 'blake3', ?, 'brokered', "
            "'google', 'trace-sub')",
            (_CRED_REF,),
        )
        conn.execute(
            "INSERT INTO security_rule_events (timestamp_unix_ms, event_id, event_type, rule_id, "
            "rule_action, detection_level, rule_json, turn_id) VALUES "
            "(1791964802000, '0000000000a1', 'process.exec', 'r1', 'allow', 'low', '{\"id\":\"r1\"}', 'turn-1')"
        )
        conn.execute(
            "INSERT INTO security_decision_events (timestamp_unix_ms, event_id, event_type, stage, "
            "actor, rule_id, previous_decision, requested_decision, effective_decision, turn_id) VALUES "
            "(1791964802000, '0000000000a2', 'process.exec', 'rule', 'engine', 'r1', 'allow', 'allow', 'allow', 'turn-1')"
        )
        conn.execute(
            "INSERT INTO security_ask_events (timestamp_unix_ms, ask_id, event_id, event_type, "
            "rule_id, rule_name, status, rule_json, turn_id) VALUES "
            "(1791964802000, '0000000000a3', '0000000000a4', 'process.exec', 'r1', 'rule1', 'approved', '{\"id\":\"r1\"}', 'turn-1')"
        )
        conn.execute(
            "INSERT INTO event_body_blobs (event_id, event_type, source_table, direction, "
            "content_type, original_bytes, stored_bytes, truncated, body_hash, block_offset, "
            "body_offset, body_len, trace_id, turn_id, created_at) VALUES "
            "('0000000000a1', 'security.rule', 'security_rule_events', 'payload', 'application/msgpack', "
            "16, 16, 0, ?, 80, 144, 16, 'tr-1', 'turn-1', '2026-10-14T08:00:02Z')",
            (_HASH_B,),
        )
    return db_path


def test_timestamp_and_security_table_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    assert parse_iso_datetime(None) is None and parse_iso_datetime("   ") is None
    assert parse_iso_datetime("not-a-timestamp") is None
    naive = dt.datetime.fromisoformat("2026-10-14T08:00:00")
    aware = dt.datetime(2026, 10, 14, 10, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))
    assert parse_iso_datetime(naive) == dt.datetime(2026, 10, 14, 8, 0, 0, tzinfo=dt.UTC)
    assert parse_iso_datetime(aware) == dt.datetime(2026, 10, 14, 8, 0, 0, tzinfo=dt.UTC)
    assert parse_iso_epoch(1700000000) == 1700000000.0 and parse_iso_epoch(None) is None
    assert format_iso_utc("2026-10-14T08:00:01+00:00") == "2026-10-14T08:00:01Z"
    assert format_iso_utc(None) is None

    for k in ("CAPSEM_TRACE_KEEP_SECURITY_TABLES", "CAPSEM_TRACE_KEEP_SECURITY", "CAPSEM_TRACE_DROP_SECURITY_TABLES", "CAPSEM_TRACE_COMPACT"):
        monkeypatch.delenv(k, raising=False)
    assert should_drop_security_tables() is True
    assert should_drop_security_tables(drop_security_tables=False) is False
    assert should_drop_security_tables(compact=False) is False
    monkeypatch.setenv("CAPSEM_TRACE_KEEP_SECURITY", "1")
    assert should_drop_security_tables() is False


def test_find_export_generation_pin_and_missing_table_error(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    populate_real_session_db(run_dir, "vm-1")
    assert find_session_db("") is None and find_session_db("missing", run_dir=run_dir) is None
    assert find_session_db("vm-1", capsem_home=tmp_path) == run_dir / "sessions" / "vm-1" / "session.db"

    out_dir = tmp_path / "exported"
    assert export_session_ledger("missing", out_dir, run_dir=run_dir) == (None, None)
    exp_db, exp_bodies = export_session_ledger(
        "vm-1", out_dir, labels={"task": "t1"}, drop_security_tables=True, run_dir=run_dir
    )
    assert exp_db is not None and exp_db.is_file()
    assert exp_bodies is not None and exp_bodies.is_dir()
    cbl_files = [p.name for p in exp_bodies.iterdir()]
    assert cbl_files == ["g-0123456789abcdef0123456789abcdef.cbl"]
    assert json.loads((out_dir / "vm-1.meta.json").read_text(encoding="utf-8"))["labels"] == {"task": "t1"}

    assert load_db_tables(tmp_path / "no.db", ("exec_events",)) == {"exec_events": []}
    assert load_db_tables(exp_db, DROPPED_SECURITY_TABLES) == {t: [] for t in DROPPED_SECURITY_TABLES}
    with pytest.raises(sqlite3.OperationalError, match="nonexistent_table"):
        load_db_tables(exp_db, ("nonexistent_table",))

    kept_db, _ = export_session_ledger("vm-1", tmp_path / "kept", drop_security_tables=False, run_dir=run_dir)
    assert kept_db is not None
    ev_sec = load_db_events(kept_db, include_security_events=True)
    assert all(t in ev_sec for t in (*CAPSEM_EVENT_TABLES, *DROPPED_SECURITY_TABLES))
    ep_s, ep_e = parse_iso_epoch("2026-10-14T08:00:01Z"), parse_iso_epoch("2026-10-14T08:00:03Z")
    assert ep_s is not None and ep_e is not None
    assert len(slice_events(ev_sec, ep_s, ep_e)["exec_events"]) == 1

    db_cache = {exp_db: load_db_events(exp_db), kept_db: ev_sec}
    assert match_sample_db({}, ep_s, ep_e, set()) is None
    assert match_sample_db({exp_db: db_cache[exp_db]}, None, None, set()) == exp_db
    assert match_sample_db(db_cache, ep_s, ep_e, {"git status"}) in (exp_db, kept_db)
    assert read_archived_body_index(exp_db)["format_version"] == 4


def test_synthesize_exec_only_and_enrich_atif(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    db1 = populate_real_session_db(run_dir, "vm-turn", with_models=True, single_turn=True)
    synth = session_db_to_atif(db1, vm_id="vm-turn", output_path=tmp_path / "synth.json")
    assert validate_atif_trajectory(synth) == []
    assert synth["steps"][0]["source"] == "system"
    assert synth["steps"][1]["extra"]["capsem_events"]["join_mode"] == "timestamp_window"
    assert len(synth["steps"][1]["extra"]["capsem_events"]["exec_events"]) == 1
    assert len(synth["steps"][2]["extra"]["capsem_events"]["exec_events"]) == 1

    db_exec = populate_real_session_db(run_dir, "vm-exec", with_models=False, with_exec=True)
    exec_traj = session_db_to_atif(db_exec, vm_id="vm-exec", bodies="previews")
    assert validate_atif_trajectory(exec_traj) == []
    assert exec_traj["extra"]["capsem_summary"]["join_mode"] == "exec_only"
    assert exec_traj["steps"][0]["source"] == "user"
    assert exec_traj["steps"][1]["llm_call_count"] == 0
    assert exec_traj["steps"][1]["message"] == "git status"
    assert exec_traj["steps"][1]["observation"]["results"][0]["content"] == "On branch"

    db_empty = populate_real_session_db(run_dir, "vm-empty", with_models=False, with_exec=False)
    with pytest.raises(ValueError, match="no model_calls or exec_events"):
        session_db_to_atif(db_empty, vm_id="vm-empty")
    with pytest.raises(ValueError, match="Invalid bodies mode"):
        session_db_to_atif(db_exec, vm_id="vm-exec", bodies=cast(Any, "bad"))

    traj_ord: dict[str, Any] = {
        "schema_version": "ATIF-v1.7",
        "session_id": "s-ord",
        "agent": {"name": "a", "version": "1"},
        "steps": [
            {"step_id": 1, "source": "agent", "message": "m1", "extra": None},
            {"step_id": 2, "source": "agent", "message": "m2"},
        ],
    }
    res_ord = enrich_atif_trajectory(traj_ord, db1, vm_id="vm-turn")
    assert res_ord["steps"][0]["extra"]["capsem_events"]["join_mode"] == "ordinal_fallback"

    enrich_steps_by_turn_id([], [], {})
    enrich_steps_by_timestamp_window([], {})
    raw_steps = build_steps_from_session_tables(
        [{"id": 1, "timestamp": "2026-10-14T08:00:01Z", "turn_id": "t"}],
        [],
        [{"model_call_id": 1, "call_id": "c1", "tool_name": "bash", "arguments": "not-json"}],
        [],
    )
    assert raw_steps[0]["source"] == "user"


def test_msgpack_decoder_covers_types_and_fallbacks() -> None:
    msg = (
        b"\x83"  # fixmap(3)
        b"\xa3cmd\xd9\x04bash"  # fixstr 'cmd' -> str8 'bash'
        b"\xa4nums\x96\x01\xff\xcc\x08\xd1\xff\xf0\xca"
        + struct.pack(">f", 1.5)
        + b"\xcb"
        + struct.pack(">d", 2.5)
        + b"\xa5flags\x94\xc0\xc2\xc3\xc4\x02ab"
    )
    decoded = decode_security_payload(
        {"encoding": "base64", "data": base64.b64encode(msg).decode("ascii")}
    )
    assert decoded["cmd"] == "bash" and decoded["nums"][0] == 1 and decoded["nums"][1] == -1
    assert decoded["flags"] == [None, False, True, "6162"]
    assert decode_security_payload({"encoding": "utf-8", "data": '{"k": 1}'}) == {"k": 1}
    assert decode_security_payload({"encoding": "utf-8", "data": "plain"}) == "plain"
