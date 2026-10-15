"""Hermetic unit tests for `harbor_capsem._trace` (`session.db` export & ATIF enrichment)."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from capsem import Export
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths
from harbor.utils.trajectory_validator import TrajectoryValidator
from harbor_capsem import CapsemEnvironment, session_db_to_atif
from harbor_capsem._trace import export_and_enrich_trial_trace
from inspect_capsem._controller import CommandResult, _managed_vm_labels

_DDL_PATH = Path(__file__).resolve().parents[3] / "crates/capsem-logger/src/schema/ddl.rs"
_CREATE_SCHEMA = (
    _DDL_PATH.read_text(encoding="utf-8")
    .split('pub const CREATE_SCHEMA: &str = "', 1)[1]
    .split('";', 1)[0]
)
_HASH_A = "blake3:" + "a" * 64


class _FakeTraceController:
    def __init__(self, vm_id: str = "vm-trace-0") -> None:
        self._vm_id, self.stopped_vms, self.events = vm_id, [], []
        self._sessions: dict[str, Any] = {}

    async def start_vm(
        self, *, cpu_count: int, ram_gb: int, image: str | None = None, **kwargs: Any
    ) -> str:
        del cpu_count, ram_gb, image
        _managed_vm_labels(kwargs.get("labels"))

        async def _persist(name: str) -> None:
            self.events.append(f"persist:{name}")

        async def _stop() -> None:
            self.events.append("stop")

        vm: Any = SimpleNamespace(id=self._vm_id, state="running", persist=_persist, stop=_stop)
        vm.export = Export(vm)
        self._sessions[self._vm_id] = vm
        return self._vm_id

    async def stop_vm(self, vm_id: str) -> None:
        self.events.append("delete")
        self.stopped_vms.append(vm_id)

    async def list_vms(self) -> list[Any]:
        return []

    async def upload_to_vm(self, vm_id: str, guest_path: str, data: bytes) -> None:
        del vm_id, guest_path, data

    async def download_from_vm(
        self, vm_id: str, guest_path: str, *, max_bytes: int | None = None
    ) -> bytes:
        del vm_id, guest_path, max_bytes
        return b""

    async def close(self) -> None:
        return None

    async def exec_in_vm(self, vm_id: str, command: str, *, timeout: int = 120) -> CommandResult:
        del vm_id, timeout
        out = "/workspace\n" if command == "pwd" else ""
        return CommandResult(exit_code=0, stdout=out, stderr="")


def _create_session_db(
    capsem_home: Path, vm_id: str, *, include_model_calls: bool, single_turn_id: bool = False
) -> Path:
    session_dir = capsem_home / "run" / "sessions" / vm_id
    bodies_dir = session_dir / "session.bodies"
    bodies_dir.mkdir(parents=True, exist_ok=True)
    gen_id = bytes.fromhex("0123456789abcdef0123456789abcdef")
    (bodies_dir / f"g-{gen_id.hex()}.cbl").write_bytes(b"CAPSEM_BODIES_V4_PAYLOAD")
    db_path = session_dir / "session.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(_CREATE_SCHEMA)
    conn.execute("INSERT INTO archive_state VALUES (1, ?, ?, 4, 128, 1)", (gen_id, gen_id))
    conn.execute("INSERT INTO body_blocks VALUES (80, 128, 128, '2026-10-14T08:00:01Z')")
    t1, t2 = "turn-1", ("turn-1" if single_turn_id else "turn-2")
    if include_model_calls:
        conn.execute(
            "INSERT INTO model_calls (timestamp, provider, model, process_name, method, path, "
            "request_body_preview, text_content, thinking_content, input_tokens, output_tokens, "
            "estimated_cost_usd, turn_id) VALUES "
            "('2026-10-14T08:00:01Z', 'anthropic', 'claude-sonnet-4', 'claude', 'POST', "
            "'/v1/messages', 'Fix git repo', 'Running git status', 'Check repo first', "
            "120, 30, 0.0015, ?), "
            "('2026-10-14T08:00:05Z', 'anthropic', 'claude-sonnet-4', 'claude', 'POST', "
            "'/v1/messages', 'Continue', 'Fixed the branch', 'All clean', 150, 40, 0.0020, ?)",
            (t1, t2),
        )
        conn.execute(
            "INSERT INTO model_items (model_call_id, timestamp, provider, model, path, kind, "
            "item_index, content, content_hash) VALUES "
            "(1, '2026-10-14T08:00:01Z', 'anthropic', 'claude-sonnet-4', '/v1/messages', "
            "'request', 0, 'Fix the broken git repository', ?)",
            (_HASH_A,),
        )
        conn.execute(
            "INSERT INTO tool_calls (timestamp, model_call_id, call_index, call_id, tool_name, "
            "arguments, response_preview, turn_id) VALUES "
            "('2026-10-14T08:00:02Z', 1, 0, 'toolu_01', 'bash', '{\"cmd\": \"git status\"}', "
            "'On branch main', ?)",
            (t1,),
        )
        conn.execute(
            "INSERT INTO tool_responses (model_call_id, call_id, content_preview, turn_id) "
            "VALUES (1, 'toolu_01', 'On branch main', ?)",
            (t1,),
        )
    conn.executescript(
        f"""
        INSERT INTO exec_events (
            timestamp, exec_id, command, exit_code, stdout_preview, target, turn_id
        ) VALUES
        ('2026-10-14T08:00:02Z', 1, 'git status', 0, 'On branch main', 'workload', '{t1}'),
        ('2026-10-14T08:00:06Z', 2, 'git checkout -b fix', 0, 'Switched to a new branch',
         'workload', '{t2}');
        INSERT INTO audit_events (timestamp, pid, ppid, uid, exe, argv, turn_id)
        VALUES ('2026-10-14T08:00:02Z', 100, 1, 0, '/usr/bin/git', 'git status', '{t1}');
        INSERT INTO net_events (timestamp, domain, decision, status_code, turn_id)
        VALUES ('2026-10-14T08:00:01Z', 'api.anthropic.com', 'allowed', 200, '{t1}');
        INSERT INTO dns_events (timestamp, qname, qtype, qclass, rcode, decision, turn_id)
        VALUES ('2026-10-14T08:00:01Z', 'api.anthropic.com', 1, 1, 0, 'allowed', '{t1}');
        INSERT INTO fs_events (timestamp, action, path, turn_id)
        VALUES ('2026-10-14T08:00:06Z', 'modify', '/workspace/.git/HEAD', '{t2}');
        INSERT INTO security_rule_events (timestamp_unix_ms, event_id, event_type, rule_id,
        rule_action, detection_level, rule_json, turn_id) VALUES
        (1791964802000, '0000000000a1', 'process.exec', 'r1', 'allow', 'low', '{{}}', '{t1}');
        INSERT INTO security_decision_events (timestamp_unix_ms, event_id, event_type, stage,
        actor, rule_id, previous_decision, requested_decision, effective_decision, turn_id) VALUES
        (1791964802000, '0000000000a2', 'process.exec', 'rule', 'engine', 'r1', 'allow', 'allow',
         'allow', '{t1}');
        """
    )
    conn.commit()
    conn.close()
    return db_path


def _write_sample_trajectory(traj_path: Path) -> None:
    traj_path.parent.mkdir(parents=True, exist_ok=True)
    tc = {"tool_call_id": "call_1", "function_name": "bash", "arguments": {"cmd": "git status"}}
    obs = {"results": [{"source_call_id": "call_1", "content": "On branch main"}]}
    s1 = {"step_id": 1, "timestamp": "2026-10-14T08:00:00Z", "source": "user", "message": "Fix"}
    s2 = {"step_id": 2, "timestamp": "2026-10-14T08:00:01Z", "source": "agent", "message": "Check"}
    s2.update({"tool_calls": [tc], "observation": obs, "extra": None})
    s3 = {"step_id": 3, "timestamp": "2026-10-14T08:00:05Z", "source": "agent", "message": "Fixed"}
    data = {
        "schema_version": "ATIF-v1.7",
        "session_id": "sess-1",
        "agent": {"name": "test-agent", "version": "1.0"},
        "steps": [s1, s2, s3],
    }
    traj_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


async def _run_trace_env(
    tmp_path: Path,
    vm_id: str,
    trial_paths: TrialPaths,
    capsem_home: Path,
    *,
    export_trace: bool | str = True,
    include_security_events: bool | str = False,
) -> _FakeTraceController:
    env_dir = tmp_path / f"env-{vm_id}"
    env_dir.mkdir(exist_ok=True)
    ctrl = _FakeTraceController(vm_id=vm_id)
    env = CapsemEnvironment(
        environment_dir=env_dir,
        environment_name=vm_id,
        session_id=f"{vm_id}__1",
        trial_paths=trial_paths,
        task_env_config=EnvironmentConfig(docker_image="ubuntu:24.04"),
        export_trace=export_trace,
        include_security_events=include_security_events,
        capsem_home=capsem_home,
        controller=ctrl,
    )
    await env.start(force_build=False)
    await env.stop(delete=True)
    return ctrl


async def test_turn_id_enrichment_bodies_copy_and_default_security_filter(tmp_path: Path) -> None:
    capsem_home, vm_id = tmp_path / "capsem_home", "vm-cli-1"
    _create_session_db(capsem_home, vm_id, include_model_calls=True)
    trial_paths = TrialPaths(trial_dir=tmp_path / "trial")
    traj_path = trial_paths.agent_dir / "trajectory.json"
    _write_sample_trajectory(traj_path)

    ctrl = await _run_trace_env(tmp_path, vm_id, trial_paths, capsem_home)
    assert ctrl.stopped_vms == [vm_id]
    assert ctrl.events == [f"persist:export-{vm_id[:12]}", "stop", "delete"]

    exported_db = trial_paths.trial_dir / f"{vm_id}.session.db"
    exported_bodies = trial_paths.trial_dir / f"{vm_id}.session.bodies"
    assert exported_db.is_file() and exported_bodies.is_dir()
    cbl = exported_bodies / "g-0123456789abcdef0123456789abcdef.cbl"
    assert cbl.read_bytes() == b"CAPSEM_BODIES_V4_PAYLOAD"

    with contextlib.closing(sqlite3.connect(exported_db)) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not ({"security_rule_events", "security_decision_events"} & tables)

    enriched = json.loads(traj_path.read_text(encoding="utf-8"))
    assert TrajectoryValidator().validate(traj_path, validate_images=False) is True
    summary = enriched["extra"]["capsem_summary"]
    assert (summary["join_mode"], summary["model_calls_count"]) == ("turn_id", 2)
    fm = enriched["final_metrics"]
    assert (fm["total_prompt_tokens"], fm["total_completion_tokens"]) == (270, 70)
    assert summary["session_bodies"] == f"{vm_id}.session.bodies"


async def test_timestamp_window_join_and_keep_security_tables(tmp_path: Path) -> None:
    capsem_home, vm_id = tmp_path / "capsem_home", "vm-host-1"
    _create_session_db(capsem_home, vm_id, include_model_calls=False)
    trial_paths = TrialPaths(trial_dir=tmp_path / "trial_host")
    traj_path = trial_paths.agent_dir / "trajectory.json"
    _write_sample_trajectory(traj_path)

    await _run_trace_env(
        tmp_path,
        vm_id,
        trial_paths,
        capsem_home,
        export_trace="true",
        include_security_events="true",
    )
    exported_db = trial_paths.trial_dir / f"{vm_id}.session.db"
    with contextlib.closing(sqlite3.connect(exported_db)) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"security_rule_events", "security_decision_events"} <= tables

    enriched = json.loads(traj_path.read_text(encoding="utf-8"))
    assert TrajectoryValidator().validate(traj_path, validate_images=False) is True
    assert enriched["extra"]["capsem_summary"]["join_mode"] == "timestamp_window"
    s2_events = enriched["steps"][1]["extra"]["capsem_events"]
    s3_events = enriched["steps"][2]["extra"]["capsem_events"]
    assert [e["command"] for e in s2_events["exec_events"]] == ["git status"]
    assert [e["command"] for e in s3_events["exec_events"]] == ["git checkout -b fix"]


async def test_synthesize_atif_model_and_exec_only_plus_vm_export_bridge(tmp_path: Path) -> None:
    capsem_home, vm_id = tmp_path / "capsem_home", "vm-synth-1"
    db_path = _create_session_db(capsem_home, vm_id, include_model_calls=True, single_turn_id=True)
    trial_paths = TrialPaths(trial_dir=tmp_path / "trial_synth")
    traj_path = trial_paths.agent_dir / "trajectory.json"

    await _run_trace_env(tmp_path, vm_id, trial_paths, capsem_home)
    assert TrajectoryValidator().validate(traj_path, validate_images=False) is True
    synthesized = json.loads(traj_path.read_text(encoding="utf-8"))
    assert synthesized["steps"][0]["message"] == "Fix the broken git repository"
    assert len(synthesized["steps"][1]["extra"]["capsem_events"]["exec_events"]) == 1
    assert len(synthesized["steps"][2]["extra"]["capsem_events"]["exec_events"]) == 1
    assert session_db_to_atif(db_path, vm_id=vm_id)["final_metrics"]["total_steps"] == 3

    exec_vm = "vm-exec-only"
    db_exec = _create_session_db(capsem_home, exec_vm, include_model_calls=False)
    exec_paths = TrialPaths(trial_dir=tmp_path / "trial_exec_only")
    await _run_trace_env(tmp_path, exec_vm, exec_paths, capsem_home)
    exec_traj_path = exec_paths.agent_dir / "trajectory.json"
    assert TrajectoryValidator().validate(exec_traj_path, validate_images=False) is True
    exec_traj = json.loads(exec_traj_path.read_text(encoding="utf-8"))
    assert exec_traj["extra"]["capsem_summary"]["join_mode"] == "exec_only"
    assert exec_traj["steps"][1]["llm_call_count"] == 0

    vm_paths = TrialPaths(trial_dir=tmp_path / "trial_vm_export")

    async def _fake_ledger(dest: Path, **_kw: Any) -> tuple[Path, Path]:
        return db_exec, db_exec.parent / "session.bodies"

    async def _fake_atif(*_a: Any, **_kw: Any) -> dict[str, Any]:
        return session_db_to_atif(db_exec, vm_id=exec_vm)

    fake_vm = SimpleNamespace(export=SimpleNamespace(ledger=_fake_ledger, atif=_fake_atif))
    out_db, _ = await export_and_enrich_trial_trace(vm_id=exec_vm, trial_paths=vm_paths, vm=fake_vm)
    vm_traj = json.loads((vm_paths.agent_dir / "trajectory.json").read_text(encoding="utf-8"))
    assert out_db == db_exec and vm_traj["extra"]["capsem_summary"]["session_db"] == db_exec.name
