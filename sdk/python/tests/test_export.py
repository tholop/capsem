"""Hermetic unit tests for `vm.export` (`quiesce`, `warc`, `ledger`, `atif`) & `HttpBodyReader`."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from capsem import VM, ExportUnavailableError, HttpError, Hypervisor
from capsem.trace import (
    EXPORT_PERSIST_PREFIX,
    HttpBodyReader,
    build_steps_from_session_tables,
    enrich_steps_by_turn_id,
    is_export_persisted_vm,
    quiesce_vm_for_export,
    validate_atif_trajectory,
)

from .facade_gateway import gateway
from .test_trace import _HASH_A, _HASH_B, populate_real_session_db


def _body(
    event_id: str, source_table: str, direction: str, content: str, *,
    encoding: str = "utf8", content_type: str = "application/json",
    body_hash: str = _HASH_A, truncated: bool = False,
) -> dict[str, Any]:
    return {
        "event_id": event_id, "source_table": source_table, "direction": direction,
        "content_type": content_type, "encoding": encoding, "content": content,
        "original_bytes": len(content), "stored_bytes": len(content),
        "truncated": truncated, "truncated_for_transport": False, "body_hash": body_hash,
    }


def test_vm_export_warc_ledger_and_atif_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    populate_real_session_db(run_dir, "vm-0", with_models=True, with_exec=True)
    monkeypatch.setenv("CAPSEM_RUN_DIR", str(run_dir))
    sys_txt = "Full system prompt from archive"
    req_json = json.dumps({"system": sys_txt, "messages": [{"role": "user", "content": "hi"}]})
    resp_json = json.dumps({"candidates": [{"content": "Full response from archive"}]})
    sec_json = json.dumps({"event_type": "process.exec", "rule": "r1"})

    async def run() -> None:
        async with gateway() as (url, state), Hypervisor(url, "token") as hv:
            state.warc_bytes = b"WARC/1.1\r\nContent-Length: 0\r\n\r\n"
            state.event_bodies[("vm-0", "000000000001")] = {
                "event_id": "000000000001",
                "bodies": [
                    _body("000000000001", "model_calls", "request", req_json, body_hash=_HASH_A),
                    _body("000000000001", "model_calls", "response", resp_json, body_hash=_HASH_B),
                ],
            }
            state.event_bodies[("vm-0", "000000000031")] = {
                "event_id": "000000000031",
                "bodies": [_body(
                    "000000000031", "tool_responses", "response",
                    "On branch main (full hydrated tool output)", content_type="text/plain",
                )],
            }
            state.event_bodies[("vm-0", "000000000041")] = {
                "event_id": "000000000041",
                "bodies": [_body(
                    "000000000041", "exec_events", "stdout",
                    "On branch main (full stdout)", content_type="text/plain",
                )],
            }
            state.event_bodies[("vm-0", "0000000000a1")] = {
                "event_id": "0000000000a1",
                "bodies": [_body(
                    "0000000000a1", "security_rule_events", "payload", sec_json,
                    encoding="utf8", content_type="application/json", body_hash=_HASH_B,
                )],
            }
            vm = hv.vm(id="vm-0")
            assert await vm.export.warc() == state.warc_bytes
            warc_file = tmp_path / "out" / "bodies.warc"
            assert await vm.export.warc(warc_file) == warc_file
            assert warc_file.read_bytes() == state.warc_bytes

            ledger_dir = tmp_path / "ledger"
            db_p, bodies_p = await vm.export.ledger(
                ledger_dir, include_security_events=False, labels={"env": "test"}, write_meta=True
            )
            assert db_p.is_file() and bodies_p is not None and bodies_p.is_dir()
            assert [p.name for p in bodies_p.iterdir()] == [
                "g-0123456789abcdef0123456789abcdef.cbl"
            ]
            assert (ledger_dir / "vm-0.meta.json").is_file()

            atif_http = await vm.export.atif(output_path=tmp_path / "atif_http.json")
            assert atif_http["extra"]["capsem_summary"]["bodies"] == "http"
            assert atif_http["steps"][0]["source"] == "system"
            assert atif_http["steps"][0]["message"] == sys_txt
            assert atif_http["steps"][1]["source"] == "user"
            assert atif_http["steps"][1]["message"] == "hi"
            step1 = atif_http["steps"][2]
            assert (
                step1["observation"]["results"][0]["content"]
                == "On branch main (full hydrated tool output)"
            )
            assert "llm_call" not in step1["extra"]

            atif_full = await vm.export.atif(bodies="full", include_security_events=True)
            s1_full = atif_full["steps"][2]
            assert s1_full["extra"]["llm_call"]["request"]["system"] == sys_txt
            s1_events = s1_full["extra"]["capsem_events"]
            assert s1_events["exec_events"][0]["stdout"] == "On branch main (full stdout)"
            assert s1_events["security_rule_events"][0]["payload"] == {
                "event_type": "process.exec", "rule": "r1",
            }

            def _st(sid: int, ts: str, src: str, msg: str) -> dict[str, Any]:
                return {"step_id": sid, "timestamp": ts, "source": src, "message": msg}

            ext_traj: dict[str, Any] = {
                "schema_version": "ATIF-v1.7", "session_id": "ext-1",
                "agent": {"name": "ext", "version": "1.0"},
                "steps": [
                    _st(1, "2026-10-14T08:00:00Z", "user", "go"),
                    _st(2, "2026-10-14T08:00:01Z", "agent", "s1"),
                    _st(3, "2026-10-14T08:00:05Z", "agent", "s2"),
                ],
            }
            ext_path = tmp_path / "ext_in.json"
            ext_path.write_text(json.dumps(ext_traj), encoding="utf-8")
            enriched = await vm.export.atif(ext_path, bodies="previews")
            assert enriched["steps"][1]["extra"]["capsem_model_call"]["turn_id"] == "turn-1"
            enriched_dict = await vm.export.atif(ext_traj, bodies="none")
            assert enriched_dict["extra"]["capsem_summary"]["bodies"] == "none"

    asyncio.run(run())


def test_vm_export_errors_degradation_and_quiesce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    populate_real_session_db(run_dir, "vm-0", with_models=True)
    monkeypatch.setenv("CAPSEM_RUN_DIR", str(run_dir))
    assert is_export_persisted_vm(
        SimpleNamespace(persistent=True, name=f"{EXPORT_PERSIST_PREFIX}abc")
    )
    assert not is_export_persisted_vm(SimpleNamespace(persistent=True, name="my-dev-vm"))
    assert not is_export_persisted_vm(
        SimpleNamespace(persistent=False, name=f"{EXPORT_PERSIST_PREFIX}abc")
    )

    async def run() -> None:
        async with gateway() as (url, state):
            async with VM("http://10.0.0.99:8080", "token", id="vm-0") as remote_vm:
                with pytest.raises(ExportUnavailableError, match=r"local session\.db"):
                    await remote_vm.export.ledger(tmp_path / "out")
            async with VM(url, "token", id="vm-missing") as missing_vm:
                with pytest.raises(ExportUnavailableError, match=r"local session\.db not found"):
                    await missing_vm.export.atif()
            state.event_bodies[("vm-0", "000000000001")] = 404
            async with VM(url, "token", id="vm-0") as vm:
                reader = HttpBodyReader(vm, max_bytes=4096)
                assert reader.get_event_bodies("000000000001") == ()
                await reader.prefetch(["invalid-id", "000000000001", "000000000001"])
                assert await reader.fetch_event_bodies("000000000001") == []
                degraded = await vm.export.atif(bodies="http", quiesce=True)
                assert degraded["steps"][0]["message"] == "You are helpful."
                await vm.export.ledger(tmp_path / "quiesced", quiesce=True)

            events: list[str] = []

            async def _ok_persist(name: str) -> None:
                events.append(f"persist:{name}")

            async def _conflict_persist(_name: str) -> None:
                raise HttpError(400, '{"error":"VM \\"vm-p\\" is already persistent"}')

            async def _managed_fail(_name: str) -> None:
                raise HttpError(409, '{"error":"managed sessions cannot become persistent"}')

            async def _rec_stop() -> None:
                events.append("stop")

            async def _fail_stop() -> None:
                raise RuntimeError("stop failed")

            async def _info_stopped() -> SimpleNamespace:
                return SimpleNamespace(status="stopped", persistent=True)

            async def _info_pr() -> SimpleNamespace:
                return SimpleNamespace(status="running", persistent=True)

            async def _info_404() -> SimpleNamespace:
                raise HttpError(404, '{"error":"not found"}')

            assert not await quiesce_vm_for_export(None, "vm-0")
            assert not await quiesce_vm_for_export(SimpleNamespace(), "vm-0")
            vm_s = SimpleNamespace(info=_info_stopped, persist=_ok_persist, stop=_rec_stop)
            assert await quiesce_vm_for_export(vm_s, "vm-s") and events == []
            vm_pr = SimpleNamespace(info=_info_pr, persist=_ok_persist, stop=_rec_stop)
            assert await quiesce_vm_for_export(vm_pr, "vm-pr") and events == ["stop"]
            vm_404 = SimpleNamespace(info=_info_404, persist=_ok_persist, stop=_rec_stop)
            assert not await quiesce_vm_for_export(vm_404, "vm-404")
            vm_ok = SimpleNamespace(persist=_ok_persist, stop=_rec_stop)
            vm_conf = SimpleNamespace(persist=_conflict_persist, stop=_rec_stop)
            vm_mgd = SimpleNamespace(persist=_managed_fail, stop=_rec_stop)
            vm_fail = SimpleNamespace(persist=_ok_persist, stop=_fail_stop)
            assert await quiesce_vm_for_export(vm_ok, "vm-0")
            assert await quiesce_vm_for_export(vm_conf, "vm-p")
            assert not await quiesce_vm_for_export(vm_mgd, "vm-m")
            assert not await quiesce_vm_for_export(vm_fail, "vm-f")
            assert events == ["stop", "persist:export-vm-0", "stop", "stop", "persist:export-vm-f"]

    asyncio.run(run())


def test_gemini_cli_tool_response_pairing_prompts_and_ambient_turn_id() -> None:
    trunc_req = (
        '{"model":"models/gemini-2.5-flash","contents":[{"role":"user","parts":'
        '[{"text":"Please fix the git repository"}]}],"systemInstruction":{"parts":[{"text":"trunc'
    )
    model_calls = [
        {
            "id": 1, "timestamp": "2026-10-14T18:52:48Z", "provider": "google",
            "model": "gemini-2.5-flash", "system_prompt_preview": "You are Gemini CLI.",
            "request_body_preview": trunc_req, "turn_id": "turn-uuid-1",
            "input_tokens": 100, "output_tokens": 30,
        },
        {
            "id": 2, "timestamp": "2026-10-14T18:52:53Z", "provider": "google",
            "model": "gemini-2.5-flash", "turn_id": "turn-uuid-2",
            "input_tokens": 150, "output_tokens": 25,
        },
        {
            "id": 3, "timestamp": "2026-10-14T18:52:59Z", "provider": "google",
            "model": "gemini-2.5-flash", "text_content": "Repository fixed.",
            "turn_id": "vm-ambient-tid", "input_tokens": 180, "output_tokens": 15,
        },
    ]

    def _tc(mc: int, cid: str, fn: str, args: str) -> dict[str, Any]:
        return {"model_call_id": mc, "call_id": cid, "tool_name": fn, "arguments": args}

    def _tr(mc: int, cid: str, prev: str) -> dict[str, Any]:
        return {"model_call_id": mc, "call_id": cid, "content_preview": prev, "is_error": 0}

    tool_calls = [
        _tc(1, "gemini_update_topic_0", "update_topic", '{"topic":"Inspect"}'),
        _tc(1, "gemini_run_shell_command_1", "run_shell_command", '{"command":"git status"}'),
        _tc(2, "gemini_update_topic_0", "update_topic", '{"topic":"Merge"}'),
        _tc(2, "gemini_run_shell_command_1", "run_shell_command", '{"command":"git merge fix"}'),
    ]
    tool_responses = [
        _tr(2, "update_topic_1792003967212_0", "ok-topic-1"),
        _tr(2, "run_shell_command_1792003967253_1", "On branch master"),
        _tr(3, "update_topic_1792003970888_0", "ok-topic-2"),
        _tr(3, "run_shell_command_1792003970942_1", "Merged fix"),
    ]
    steps = build_steps_from_session_tables(model_calls, [], tool_calls, tool_responses)
    assert [s["source"] for s in steps] == ["system", "user", "agent", "agent", "agent"]
    assert steps[0]["message"] == "You are Gemini CLI."
    assert steps[1]["message"] == "Please fix the git repository"
    s2_res, s3_res = steps[2]["observation"]["results"], steps[3]["observation"]["results"]
    assert [r["content"] for r in s2_res] == ["ok-topic-1", "On branch master"]
    assert [r["source_call_id"] for r in s2_res] == [
        "gemini_update_topic_0", "gemini_run_shell_command_1"
    ]
    assert [r["content"] for r in s3_res] == ["ok-topic-2", "Merged fix"]
    amb, g_dom = "vm-ambient-tid", "aiplatform.googleapis.com"
    events = {
        "exec_events": [
            {"timestamp": "2026-10-14T18:52:05Z", "command": "setup", "turn_id": amb},
            {"timestamp": "2026-10-14T18:52:49Z", "command": "git status", "turn_id": amb},
            {"timestamp": "2026-10-14T18:52:55Z", "command": "git merge fix", "turn_id": amb},
        ],
        "net_events": [
            {"timestamp": "2026-10-14T18:52:10Z", "domain": "registry.npmjs.org", "turn_id": amb},
            {"timestamp": "2026-10-14T18:52:48Z", "domain": g_dom, "turn_id": "turn-uuid-1"},
            {"timestamp": "2026-10-14T18:52:53Z", "domain": g_dom, "turn_id": "turn-uuid-2"},
            {"timestamp": "2026-10-14T18:52:59Z", "domain": g_dom, "turn_id": amb},
        ],
    }
    enrich_steps_by_turn_id(steps, model_calls, events)
    s2_ex = steps[2]["extra"]["capsem_events"]["exec_events"]
    s3_ex = steps[3]["extra"]["capsem_events"]["exec_events"]
    assert [e["command"] for e in s2_ex] == ["git status"]
    assert [e["command"] for e in s3_ex] == ["git merge fix"]
    assert steps[4]["extra"]["capsem_events"]["exec_events"] == []
    assert len(steps[4]["extra"]["capsem_events"]["net_events"]) == 1
    traj = {
        "schema_version": "ATIF-v1.7", "session_id": "s",
        "agent": {"name": "g", "version": "1"}, "steps": steps,
    }
    assert validate_atif_trajectory(traj) == []
