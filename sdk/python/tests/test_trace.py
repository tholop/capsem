"""Hermetic unit tests for `capsem.trace` against the canonical 0.7 `CREATE_SCHEMA`."""

from __future__ import annotations

import base64
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import capsem
import pytest
from capsem import trace

_DDL_PATH = Path(__file__).resolve().parents[3] / "crates/capsem-logger/src/schema/ddl.rs"
_CREATE_SCHEMA = _DDL_PATH.read_text().split('CREATE_SCHEMA: &str = "', 1)[1].split('";', 1)[0]
_HASH_A, _HASH_B = "blake3:" + "a" * 64, "blake3:" + "b" * 64
_CRED_REF = "credential:blake3:" + "c" * 64


def populate_real_session_db(
    base_dir: Path, vm_id: str, *,
    with_models: bool = True, with_exec: bool = True, single_turn: bool = False,
) -> Path:
    sdir = base_dir / "sessions" / vm_id
    db_path, bodies_dir = sdir / "session.db", sdir / "session.bodies"
    bodies_dir.mkdir(parents=True, exist_ok=True)
    gen_id = bytes.fromhex("0123456789abcdef0123456789abcdef")
    stale_id = bytes.fromhex("fedcba9876543210fedcba9876543210")
    (bodies_dir / f"g-{gen_id.hex()}.cbl").write_bytes(
        b'{"messages":[{"role":"user","content":"fix git"}]}'
    )
    (bodies_dir / f"g-{stale_id.hex()}.cbl").write_bytes(b"stale")
    tid2 = "turn-1" if single_turn else "turn-2"
    ts1, ts2 = "2026-10-14T08:00:01Z", "2026-10-14T08:00:02Z"
    blob_sql = (
        "INSERT INTO event_body_blobs (event_id, event_type, source_table, direction, "
        "content_type, original_bytes, stored_bytes, truncated, body_hash, block_offset, "
        f"body_offset, body_len, trace_id, turn_id, created_at) VALUES "
        f"(?, ?, ?, ?, ?, ?, ?, ?, ?, 80, ?, ?, 'tr-1', ?, '{ts1}')"
    )
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            f"{_CREATE_SCHEMA} INSERT INTO body_blocks VALUES (80, 256, 256, '{ts1}');"
            "INSERT INTO fs_events (event_id, timestamp, action, path, kind, turn_id) VALUES "
            "('000000000051', '2026-10-14T08:00:03Z', 'modified', '/repo/a', 'file', 'turn-1');"
            f"INSERT INTO net_events (event_id, timestamp, domain, decision, status_code, "
            f"request_body_preview, turn_id) VALUES ('000000000061', '{ts1}', "
            "'generativelanguage.googleapis.com', 'allowed', 200, '{\"net\":1}', 'turn-1');"
            f"INSERT INTO dns_events (event_id, timestamp, qname, qtype, qclass, rcode, decision, "
            f"turn_id) VALUES ('000000000071', '{ts1}', 'api.google.com', 1, 1, 0, 'allowed', "
            "'turn-1'); INSERT INTO audit_events (event_id, timestamp, pid, ppid, uid, exe, argv, "
            f"turn_id) VALUES ('000000000081', '{ts2}', 10, 1, 0, '/bin/git', 'git status', "
            "'turn-1'); INSERT INTO security_rule_events (timestamp_unix_ms, event_id, event_type, "
            "rule_id, rule_action, detection_level, rule_json, turn_id) VALUES (1791964802000, "
            "'0000000000a1', 'process.exec', 'r1', 'allow', 'low', '{\"id\":\"r1\"}', 'turn-1');"
            "INSERT INTO security_decision_events (timestamp_unix_ms, event_id, event_type, stage, "
            "actor, rule_id, previous_decision, requested_decision, effective_decision, turn_id) "
            "VALUES (1791964802000, '0000000000a2', 'process.exec', 'rule', 'engine', 'r1', "
            "'allow', 'allow', 'allow', 'turn-1'); INSERT INTO security_ask_events "
            "(timestamp_unix_ms, ask_id, event_id, event_type, rule_id, rule_name, status, "
            "rule_json, turn_id) VALUES (1791964802000, '0000000000a3', '0000000000a4', "
            "'process.exec', 'r1', 'rule1', 'approved', '{\"id\":\"r1\"}', 'turn-1');"
            "INSERT INTO substitution_events (event_id, timestamp, material_class, source, "
            "algorithm, substitution_ref, outcome, provider, turn_id) VALUES ('000000000091', "
            f"'2026-10-14T08:00:00Z', 'api_key', 'env', 'blake3', '{_CRED_REF}', 'brokered', "
            "'google', 'trace-sub');"
        )
        conn.execute("INSERT INTO archive_state VALUES (1, ?, ?, 4, 128, 1)", (gen_id, gen_id))
        blobs = [("0000000000a1", "security.rule", "security_rule_events", "payload",
                  "application/msgpack", 16, 16, 0, _HASH_B, 144, 16, "turn-1")]
        if with_models:
            conn.executescript(
                "INSERT INTO model_calls (id, event_id, timestamp, provider, protocol, model, "
                "process_name, method, path, system_prompt_preview, messages_count, tools_count, "
                "text_content, thinking_content, stop_reason, input_tokens, output_tokens, "
                "usage_details, duration_ms, estimated_cost_usd, request_body_preview, turn_id) "
                f"VALUES (1, '000000000001', '{ts1}', 'google', 'google', 'gemini-3.1-pro', "
                "'gemini-cli', 'POST', '/v1/m', 'You are helpful.', 1, 1, NULL, NULL, 'tool_use', "
                "120, 40, '{\"cached_tokens\":20}', 450, 0.002, '{\"req\":', 'turn-1'), "
                "(2, '000000000002', '2026-10-14T08:00:05Z', 'google', 'google', 'gemini-3.1-pro', "
                "'gemini-cli', 'POST', '/v1/m', NULL, 3, 1, 'Fixed.', 'Done thinking.', "
                f"'end_turn', 160, 20, 'not-json', 300, 0.001, NULL, '{tid2}');"
                "INSERT INTO model_items (event_id, model_call_id, timestamp, provider, model, "
                "path, trace_id, turn_id, kind, item_index, call_id, content, content_hash) VALUES "
                f"('000000000011', 1, '{ts1}', 'google', 'm', '/v1/m', 'tr-1', 'turn-1', "
                f"'reasoning', 0, '', 'Check git status', '{_HASH_A}'), ('000000000012', 1, "
                f"'{ts1}', 'google', 'm', '/v1/m', 'tr-1', 'turn-1', 'response', 1, '', "
                f"'Running git status', '{_HASH_B}');"
                "INSERT INTO tool_calls (id, event_id, timestamp, model_call_id, provider, status, "
                "call_index, call_id, tool_name, arguments, response_preview, origin, transport, "
                f"turn_id) VALUES (1, '000000000021', '{ts2}', 1, 'google', 'responded', 0, "
                "'call-1', 'bash', '{\"cmd\":\"git status\"}', 'On branch', 'native', 'http', "
                "'turn-1'); INSERT INTO tool_responses (id, event_id, model_call_id, call_id, "
                "content_preview, is_error, turn_id) VALUES (1, '000000000031', 2, 'call-1', "
                "'On branch', 0, 'turn-1');"
            )
            blobs.extend([
                ("000000000001", "model.call", "model_calls", "request",
                 "application/json", 64, 50, 1, _HASH_A, 0, 50, "turn-1"),
                ("000000000001", "model.call", "model_calls", "response",
                 "application/json", 64, 50, 1, _HASH_B, 50, 50, "turn-1"),
                ("000000000031", "model.call", "tool_responses", "response",
                 "text/plain", 32, 24, 1, _HASH_A, 100, 24, "turn-1"),
            ])
        if with_exec:
            conn.execute(
                "INSERT INTO exec_events (id, event_id, timestamp, exec_id, command, exit_code, "
                "duration_ms, stdout_preview, stderr_preview, target, turn_id, process_name) "
                f"VALUES (1, '000000000041', '{ts2}', 1, 'git status', 0, 12, 'On branch', '', "
                "'workload', 'trace-exec-1', 'bash'), (2, '000000000042', "
                "'2026-10-14T08:00:06Z', 2, 'git checkout -b fix', 0, 15, 'Switched', '', "
                "'workload', 'trace-exec-2', 'bash')"
            )
            blobs.append(("000000000041", "process.exec_complete", "exec_events", "stdout",
                          "text/plain", 20, 20, 0, _HASH_A, 124, 20, "trace-exec-1"))
        conn.executemany(blob_sql, blobs)
    return db_path


def test_timestamp_flags_and_ledger_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert trace.parse_iso_datetime(None) is None and trace.parse_iso_datetime("   ") is None
    assert trace.parse_iso_datetime("not-a-timestamp") is None
    naive = dt.datetime.fromisoformat("2026-10-14T08:00:00")
    aware = dt.datetime(2026, 10, 14, 10, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))
    utc_ref = dt.datetime(2026, 10, 14, 8, 0, 0, tzinfo=dt.UTC)
    assert trace.parse_iso_datetime(naive) == utc_ref and trace.parse_iso_datetime(aware) == utc_ref
    assert trace.parse_iso_datetime("2026-10-14T08:00:00Z") == utc_ref
    monkeypatch.delenv("CAPSEM_TRACE_INCLUDE_SECURITY", raising=False)
    assert not trace.resolve_include_security_events()
    assert trace.resolve_include_security_events(include_security_events=True)
    monkeypatch.setenv("CAPSEM_TRACE_INCLUDE_SECURITY", "1")
    assert trace.resolve_include_security_events()
    assert not trace.resolve_include_security_events(include_security_events=False)

    run_dir, out_dir = tmp_path / "run", tmp_path / "exported"
    db_path = populate_real_session_db(run_dir, "vm-1")
    assert trace.find_session_db("") is None
    assert trace.find_session_db("missing", run_dir=run_dir) is None
    assert trace.find_session_db("vm-1", capsem_home=tmp_path) == db_path
    assert trace.has_session_events(db_path) and not trace.has_session_events(tmp_path / "no.db")
    assert trace.export_session_ledger("missing", out_dir, run_dir=run_dir) == (None, None)
    exp_db, exp_bodies = trace.export_session_ledger(
        "vm-1", out_dir, labels={"task": "t1"}, include_security_events=False, run_dir=run_dir
    )
    assert exp_db is not None and exp_db.is_file() and exp_bodies is not None
    assert [p.name for p in exp_bodies.iterdir()] == ["g-0123456789abcdef0123456789abcdef.cbl"]
    assert json.loads((out_dir / "vm-1.meta.json").read_text())["labels"] == {"task": "t1"}
    assert trace.load_db_tables(tmp_path / "no.db", ("exec_events",)) == {"exec_events": []}
    sec_tbls = trace.DROPPED_SECURITY_TABLES
    assert trace.load_db_tables(exp_db, sec_tbls) == {t: [] for t in sec_tbls}
    with pytest.raises(sqlite3.OperationalError, match="nonexistent_table"):
        trace.load_db_tables(exp_db, ("nonexistent_table",))
    kept_db, _ = trace.export_session_ledger(
        "vm-1", tmp_path / "kept", include_security_events=True, run_dir=run_dir
    )
    assert kept_db is not None
    ev_sec = trace.load_db_tables(
        kept_db, (*trace.HARBOR_EVENT_TABLES, *sec_tbls), order_by_id=False
    )
    assert len(ev_sec["security_rule_events"]) == 1


def test_synthesize_exec_only_and_enrich_atif(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    db1 = populate_real_session_db(run_dir, "vm-turn", with_models=True, single_turn=True)
    synth = trace.session_db_to_atif(db1, vm_id="vm-turn", output_path=tmp_path / "synth.json")
    assert trace.validate_atif_trajectory(synth) == []
    assert synth["agent"]["version"] == capsem.__version__ and "capsem" not in synth["extra"]
    summary = synth["extra"]["capsem_summary"]
    assert "drop_security_tables" not in summary and "event_counts" not in summary
    assert summary["dropped_security_tables"] is True and summary["exec_events_count"] == 2
    assert synth["steps"][0]["source"] == "system"
    assert synth["steps"][1]["extra"]["capsem_events"]["join_mode"] == "timestamp_window"
    assert len(synth["steps"][1]["extra"]["capsem_events"]["exec_events"]) == 1
    assert len(synth["steps"][2]["extra"]["capsem_events"]["exec_events"]) == 1
    db_exec = populate_real_session_db(run_dir, "vm-exec", with_models=False, with_exec=True)
    exec_traj = trace.session_db_to_atif(db_exec, vm_id="vm-exec", bodies="previews")
    assert trace.validate_atif_trajectory(exec_traj) == []
    assert exec_traj["extra"]["capsem_summary"]["join_mode"] == "exec_only"
    assert exec_traj["steps"][1]["llm_call_count"] == 0
    assert exec_traj["steps"][1]["message"] == "git status"
    db_empty = populate_real_session_db(run_dir, "vm-empty", with_models=False, with_exec=False)
    assert not trace.has_session_events(db_empty)
    with pytest.raises(ValueError, match="no model_calls or exec_events"):
        trace.session_db_to_atif(db_empty, vm_id="vm-empty")
    with pytest.raises(ValueError, match="Invalid bodies mode"):
        trace.session_db_to_atif(db_exec, vm_id="vm-exec", bodies=cast(Any, "bad"))
    traj_ord: dict[str, Any] = {
        "schema_version": "ATIF-v1.8", "session_id": "s-ord",
        "agent": {"name": "a", "version": "1"},
        "steps": [
            {"step_id": 1, "source": "agent", "message": [{"text": "m1"}], "extra": None},
            {"step_id": 2, "source": "agent", "message": "m2"},
        ],
    }
    assert trace.validate_atif_trajectory(traj_ord) == []
    res_ord = trace.enrich_atif_trajectory(
        traj_ord, db1, vm_id="vm-turn", output_path=tmp_path / "ord.json"
    )
    assert res_ord["steps"][0]["extra"]["capsem_events"]["join_mode"] == "ordinal_fallback"
    assert (tmp_path / "ord.json").is_file()
    trace.enrich_steps_by_turn_id([], [], {})
    trace.enrich_steps_by_timestamp_window([], {})


def test_step_builder_edge_cases_and_hydration_heuristics(monkeypatch: pytest.MonkeyPatch) -> None:
    def _tc(mc: int | None, cid: str, fn: str, args: str = "{}") -> dict[str, Any]:
        return {"model_call_id": mc, "call_id": cid, "tool_name": fn, "arguments": args}

    def _tr(mc: int, cid: str, prev: str, err: int = 0) -> dict[str, Any]:
        return {"model_call_id": mc, "call_id": cid, "content_preview": prev, "is_error": err}

    mcalls = [
        {"id": 1, "timestamp": "2026-10-14T08:00:01Z",
         "system_prompt_preview": "sys", "request_body_preview": '{"text": "hello \\u00'},
        {"id": 2, "timestamp": "2026-10-14T08:00:02Z",
         "request_body": '{"input": "Responses string prompt"}'},
    ]
    items = [
        {"model_call_id": None, "kind": "response", "content": "orphan"},
        {"model_call_id": "bad", "kind": "response", "content": "bad"},
        {"model_call_id": 1, "kind": "response", "content": "ok"},
    ]
    tcalls = [
        _tc(None, "orphan", "bash"), _tc(1, "call_1_0", "read", "not-json"),
        _tc(1, "call_1_1", "read_file"), _tc(2, "c_ord_0", "alpha"), _tc(2, "c_ord_1", "beta"),
    ]
    tresp = [
        _tr(2, "read_file_99_1", "f-c"), _tr(2, "read_99_0", "r-c"),
        _tr(2, "r0", "first-ordinal"), _tr(2, "r1", "second-ordinal", 1),
    ]
    steps = trace.build_steps_from_session_tables(mcalls, items, tcalls, tresp)
    assert steps[0]["message"] == "sys" and steps[1]["message"] == "hello"
    assert [r["content"] for r in steps[2]["observation"]["results"]] == ["r-c", "f-c"]
    assert [r["content"] for r in steps[3]["observation"]["results"]] == [
        "first-ordinal", "second-ordinal"
    ]
    assert (
        trace.build_steps_from_session_tables([mcalls[1]], [], [], [])[0]["message"]
        == "Responses string prompt"
    )

    def _blob(eid: str, tbl: str, d: str, b: int, tr: int = 0) -> dict[str, Any]:
        return {
            "event_id": eid, "source_table": tbl, "direction": d,
            "stored_bytes": b, "truncated": tr,
        }

    tables: dict[str, list[dict[str, Any]]] = {
        "model_calls": [
            {"id": 1, "event_id": "000000000001", "system_prompt_preview": "s",
             "request_body_preview": '{"full_request": 1}'},
            {"id": 2, "event_id": "000000000002", "request_body_preview": '{"trunc":'},
        ],
        "tool_calls": [{"id": 1, "event_id": "000000000021", "arguments": '{"a": 1}'}],
        "tool_responses": [], "exec_events": [],
        "security_rule_events": [{"id": 1, "event_id": "0000000000a1"}],
        "event_body_blobs": [
            _blob("000000000001", "model_calls", "request", 19),
            _blob("000000000001", "model_calls", "response", 500),
            _blob("000000000002", "model_calls", "request", 100, 1),
            _blob("000000000021", "tool_calls", "request", 8),
            _blob("0000000000a1", "security_rule_events", "payload", 16),
        ],
    }
    assert trace.candidate_body_event_ids(tables, full=False) == []
    assert trace.candidate_body_event_ids(tables, full=True) == [
        "000000000001", "000000000002", "000000000021", "0000000000a1"
    ]

    class _DummyReader:
        def get_event_bodies(self, event_id: str) -> list[dict[str, Any]]:
            return [{
                "event_id": event_id, "source_table": "tool_calls",
                "direction": "request", "encoding": "utf-8", "content": '{"hydrated": true}',
            }]

    trace.hydrate_tables_from_bodies(tables, _DummyReader(), full=True)
    assert tables["tool_calls"][0]["arguments"] == '{"hydrated": true}'
    raw_bin = b"\x81\xa4rule\xa2r1"
    b64 = base64.b64encode(raw_bin).decode("ascii")
    env = {"encoding": "base64", "data": b64, "content_type": "application/msgpack"}
    assert trace.decode_security_payload(env) == env
    assert trace.decode_security_payload(raw_bin) == {"encoding": "base64", "data": b64}
    assert trace.decode_security_payload({"encoding": "utf-8", "data": '{"k": 1}'}) == {"k": 1}
    assert trace.decode_security_payload({"encoding": "utf-8", "data": "plain"}) == "plain"
    monkeypatch.setitem(
        sys.modules, "msgpack",
        SimpleNamespace(unpackb=lambda data, **_kw: {"rule": "r1", "len": len(data)}),
    )
    assert trace.decode_security_payload(raw_bin) == {"rule": "r1", "len": len(raw_bin)}
    with pytest.raises(ValueError, match="12 lowercase hex"):
        trace.validate_event_id("../bad")
    assert trace.validate_atif_trajectory({"schema_version": "bad", "session_id": ""}) != []
