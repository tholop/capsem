"""Hermetic unit tests for `vm.export` (`warc`, `ledger`, `atif`) and `HttpBodyReader`."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from capsem import VM, ExportUnavailableError, Hypervisor
from capsem._export import _quiesce_vm_for_export

from .facade_gateway import gateway
from .test_trace import _HASH_A, _HASH_B, populate_real_session_db


def _body(
    event_id: str,
    source_table: str,
    direction: str,
    content: str,
    *,
    encoding: str = "utf8",
    content_type: str = "application/json",
    body_hash: str = _HASH_A,
    truncated: bool = False,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "source_table": source_table,
        "direction": direction,
        "content_type": content_type,
        "encoding": encoding,
        "content": content,
        "original_bytes": len(content),
        "stored_bytes": len(content),
        "truncated": truncated,
        "truncated_for_transport": False,
        "body_hash": body_hash,
    }


def test_vm_export_warc_ledger_and_atif_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    populate_real_session_db(run_dir, "vm-0", with_models=True, with_exec=True)
    monkeypatch.setenv("CAPSEM_RUN_DIR", str(run_dir))

    req_json = json.dumps(
        {"system": "Full system prompt from archive", "messages": [{"role": "user", "content": "hi"}]}
    )
    resp_json = json.dumps({"candidates": [{"content": "Full response from archive"}]})
    sec_msgpack = base64.b64encode(b"\x81\xa4rule\xa2r1").decode("ascii")

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
                "bodies": [
                    _body(
                        "000000000031",
                        "tool_responses",
                        "response",
                        "On branch main (full hydrated tool output)",
                        content_type="text/plain",
                    )
                ],
            }
            state.event_bodies[("vm-0", "000000000041")] = {
                "event_id": "000000000041",
                "bodies": [
                    _body(
                        "000000000041",
                        "exec_events",
                        "stdout",
                        "On branch main (full stdout)",
                        content_type="text/plain",
                    )
                ],
            }
            state.event_bodies[("vm-0", "0000000000a1")] = {
                "event_id": "0000000000a1",
                "bodies": [
                    _body(
                        "0000000000a1",
                        "security_rule_events",
                        "payload",
                        sec_msgpack,
                        encoding="base64",
                        content_type="application/msgpack",
                        body_hash=_HASH_B,
                    )
                ],
            }

            vm = hv.vm(id="vm-0")
            raw_warc = await vm.export.warc()
            assert raw_warc == state.warc_bytes
            warc_file = tmp_path / "out" / "bodies.warc"
            assert await vm.export.warc(warc_file) == warc_file
            assert warc_file.read_bytes() == state.warc_bytes

            ledger_dir = tmp_path / "ledger"
            db_p, bodies_p = await vm.export.ledger(
                ledger_dir, drop_security_tables=True, labels={"env": "test"}, write_meta=True
            )
            assert db_p.is_file() and bodies_p is not None and bodies_p.is_dir()
            assert [p.name for p in bodies_p.iterdir()] == ["g-0123456789abcdef0123456789abcdef.cbl"]
            assert (ledger_dir / "vm-0.meta.json").is_file()

            atif_http = await vm.export.atif(output_path=tmp_path / "atif_http.json")
            assert atif_http["extra"]["capsem_summary"]["bodies"] == "http"
            assert atif_http["steps"][0]["source"] == "system"
            assert atif_http["steps"][0]["message"] == "Full system prompt from archive"
            assert atif_http["steps"][1]["source"] == "user"
            assert atif_http["steps"][1]["message"] == "hi"
            step1 = atif_http["steps"][2]
            assert step1["observation"]["results"][0]["content"] == "On branch main (full hydrated tool output)"
            assert step1["extra"]["llm_call"]["request"]["system"] == "Full system prompt from archive"

            atif_full = await vm.export.atif(bodies="full", include_security_events=True)
            s1_events = atif_full["steps"][2]["extra"]["capsem_events"]
            assert s1_events["exec_events"][0]["stdout"] == "On branch main (full stdout)"
            assert s1_events["security_rule_events"][0]["payload"] == {"rule": "r1"}

            ext_traj: dict[str, Any] = {
                "schema_version": "ATIF-v1.7",
                "session_id": "ext-1",
                "agent": {"name": "ext", "version": "1.0"},
                "steps": [
                    {"step_id": 1, "timestamp": "2026-10-14T08:00:00Z", "source": "user", "message": "go"},
                    {"step_id": 2, "timestamp": "2026-10-14T08:00:01Z", "source": "agent", "message": "step1"},
                    {"step_id": 3, "timestamp": "2026-10-14T08:00:05Z", "source": "agent", "message": "step2"},
                ],
            }
            ext_path = tmp_path / "ext_in.json"
            ext_path.write_text(json.dumps(ext_traj), encoding="utf-8")
            enriched = await vm.export.atif(ext_path, bodies="previews")
            assert enriched["steps"][1]["extra"]["capsem_model_call"]["turn_id"] == "turn-1"
            enriched_dict = await vm.export.atif(ext_traj, bodies="none")
            assert enriched_dict["extra"]["capsem_summary"]["bodies"] == "none"

    asyncio.run(run())


def test_vm_export_unavailable_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = tmp_path / "run"
    populate_real_session_db(run_dir, "vm-0", with_models=True)
    monkeypatch.setenv("CAPSEM_RUN_DIR", str(run_dir))

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
                with pytest.raises(ExportUnavailableError, match="HTTP body hydration unavailable"):
                    await vm.export.atif(bodies="http")

            events: list[str] = []

            async def _ok_persist(name: str) -> None:
                events.append(f"persist:{name}")

            async def _already_persisted(_name: str) -> None:
                raise RuntimeError('VM "vm-p" is already persistent')

            async def _managed_fail(_name: str) -> None:
                raise RuntimeError("managed sessions cannot become persistent")

            async def _rec_stop() -> None:
                events.append("stop")

            await _quiesce_vm_for_export(None, "vm-0")
            await _quiesce_vm_for_export(SimpleNamespace(), "vm-0")
            no_persist = SimpleNamespace(stop=_rec_stop)
            await _quiesce_vm_for_export(no_persist, "vm-np")
            await _quiesce_vm_for_export(no_persist, "vm-np")
            await _quiesce_vm_for_export(SimpleNamespace(persist=_ok_persist, stop=_rec_stop), "vm-0")
            await _quiesce_vm_for_export(SimpleNamespace(persist=_already_persisted, stop=_rec_stop), "vm-p")
            await _quiesce_vm_for_export(SimpleNamespace(persist=_managed_fail, stop=_rec_stop), "vm-m")
            assert events == ["stop", "persist:export-vm-0", "stop", "stop"]

    asyncio.run(run())


def test_gemini_cli_tool_response_pairing_prompts_and_ambient_turn_id() -> None:
    from capsem.trace import (
        build_steps_from_session_tables,
        enrich_steps_by_turn_id,
        validate_atif_trajectory,
    )

    trunc_req = (
        '{"model":"models/gemini-2.5-flash","contents":[{"role":"user","parts":'
        '[{"text":"Please fix the git repository"}]}],"systemInstruction":{"parts":[{"text":"trunc'
    )
    model_calls = [
        {
            "id": 1, "timestamp": "2026-10-14T18:52:48Z", "provider": "google", "model": "gemini-2.5-flash",
            "system_prompt_preview": "You are Gemini CLI.", "request_body_preview": trunc_req,
            "turn_id": "turn-uuid-1", "input_tokens": 100, "output_tokens": 30,
        },
        {
            "id": 2, "timestamp": "2026-10-14T18:52:53Z", "provider": "google", "model": "gemini-2.5-flash",
            "turn_id": "turn-uuid-2", "input_tokens": 150, "output_tokens": 25,
        },
        {
            "id": 3, "timestamp": "2026-10-14T18:52:59Z", "provider": "google", "model": "gemini-2.5-flash",
            "text_content": "Repository fixed.", "turn_id": "vm-ambient-tid", "input_tokens": 180, "output_tokens": 15,
        },
    ]
    tool_calls = [
        {"model_call_id": 1, "call_id": "gemini_update_topic_0", "tool_name": "update_topic", "arguments": '{"topic":"Inspect"}'},
        {"model_call_id": 1, "call_id": "gemini_run_shell_command_1", "tool_name": "run_shell_command", "arguments": '{"command":"git status"}'},
        {"model_call_id": 2, "call_id": "gemini_update_topic_0", "tool_name": "update_topic", "arguments": '{"topic":"Merge"}'},
        {"model_call_id": 2, "call_id": "gemini_run_shell_command_1", "tool_name": "run_shell_command", "arguments": '{"command":"git merge fix"}'},
    ]
    tool_responses = [
        {"model_call_id": 2, "call_id": "update_topic_1792003967212_0", "content_preview": "ok-topic-1", "is_error": 0},
        {"model_call_id": 2, "call_id": "run_shell_command_1792003967253_1", "content_preview": "On branch master", "is_error": 0},
        {"model_call_id": 3, "call_id": "update_topic_1792003970888_0", "content_preview": "ok-topic-2", "is_error": 0},
        {"model_call_id": 3, "call_id": "run_shell_command_1792003970942_1", "content_preview": "Merged fix", "is_error": 0},
    ]
    steps = build_steps_from_session_tables(model_calls, [], tool_calls, tool_responses)
    assert [s["source"] for s in steps] == ["system", "user", "agent", "agent", "agent"]
    assert steps[0]["message"] == "You are Gemini CLI."
    assert steps[1]["message"] == "Please fix the git repository"
    assert [r["content"] for r in steps[2]["observation"]["results"]] == ["ok-topic-1", "On branch master"]
    assert [r["source_call_id"] for r in steps[2]["observation"]["results"]] == [
        "gemini_update_topic_0", "gemini_run_shell_command_1"
    ]
    assert [r["content"] for r in steps[3]["observation"]["results"]] == ["ok-topic-2", "Merged fix"]

    events = {
        "exec_events": [
            {"timestamp": "2026-10-14T18:52:05Z", "command": "setup", "turn_id": "vm-ambient-tid"},
            {"timestamp": "2026-10-14T18:52:49Z", "command": "git status", "turn_id": "vm-ambient-tid"},
            {"timestamp": "2026-10-14T18:52:55Z", "command": "git merge fix", "turn_id": "vm-ambient-tid"},
        ],
        "net_events": [
            {"timestamp": "2026-10-14T18:52:10Z", "domain": "registry.npmjs.org", "turn_id": "vm-ambient-tid"},
            {"timestamp": "2026-10-14T18:52:48Z", "domain": "aiplatform.googleapis.com", "turn_id": "turn-uuid-1"},
            {"timestamp": "2026-10-14T18:52:53Z", "domain": "aiplatform.googleapis.com", "turn_id": "turn-uuid-2"},
            {"timestamp": "2026-10-14T18:52:59Z", "domain": "aiplatform.googleapis.com", "turn_id": "vm-ambient-tid"},
        ],
    }
    enrich_steps_by_turn_id(steps, model_calls, events)
    assert [e["command"] for e in steps[2]["extra"]["capsem_events"]["exec_events"]] == ["git status"]
    assert [e["command"] for e in steps[3]["extra"]["capsem_events"]["exec_events"]] == ["git merge fix"]
    assert steps[4]["extra"]["capsem_events"]["exec_events"] == []
    assert len(steps[4]["extra"]["capsem_events"]["net_events"]) == 1
    assert validate_atif_trajectory({"schema_version": "ATIF-v1.7", "session_id": "s", "agent": {"name": "g", "version": "1"}, "steps": steps}) == []
