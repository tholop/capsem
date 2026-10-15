"""Live acceptance tests for `harbor-capsem` across 3 Harbor benchmarks and endpoint modes."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
from harbor.models.agent.name import AgentName
from harbor.models.task.config import EnvironmentConfig as TaskEnvConfig
from harbor.models.trial.config import AgentConfig, EnvironmentConfig, TaskConfig, TrialConfig
from harbor.models.trial.paths import TrialPaths
from harbor_capsem import CapsemEnvironment
from inspect_capsem._controller import SdkCapsemController

pytestmark = [
    pytest.mark.skipif(os.environ.get("CAPSEM_LIVE_SELF_CHECK") != "1", reason="Live check off"),
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
    pytest.mark.filterwarnings("ignore::ResourceWarning"),
    pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning"),
]
_CRED_REF_RE = re.compile(r"^credential:blake3:[0-9a-f]{64}$")
_EXEC_CRED_RE = re.compile(r"GEMINI_API_KEY=(credential:blake3:[0-9a-f]{64})")
_HUB_CACHE = Path.home() / ".cache" / "inspect_harbor" / "tasks" / "hub"


def _find_cached_task(rel_glob: str) -> Path:
    matches = sorted(p for p in _HUB_CACHE.glob(rel_glob) if (p / "task.toml").is_file())
    if not matches:
        pytest.skip(f"Cached Harbor task not found under {_HUB_CACHE}/{rel_glob}")
    return matches[0]


def _resolve_vertex_project() -> str:
    if env_proj := os.environ.get("VERTEXAI_PROJECT", "").strip():
        return env_proj
    cmd = ["gcloud", "config", "get-value", "project"]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    proj = proc.stdout.strip() if proc.returncode == 0 else ""
    if proj and proj != "(unset)":
        return proj
    pytest.skip("VERTEXAI_PROJECT is not set and `gcloud config get-value project` is unconfigured")
    return ""


def _read_google_token() -> str:
    cmd = ["gcloud", "auth", "print-access-token"]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    pytest.skip("Could not obtain GCP access token via `gcloud auth print-access-token`")
    return ""


def _candidate_target_dirs() -> list[Path]:
    dirs: list[Path] = []
    if auth := os.environ.get("CAPSEM_CACHE_AUTHORITY", "").strip():
        ap = Path(auth).expanduser()
        dirs.extend((ap / "cache/target", ap / "target"))
    parents = Path(__file__).resolve().parents
    dirs.extend(p / "cache/target" for p in parents if (p / "cache/target").is_dir())
    return dirs


def _resolve_capsem_bin(name: str) -> Path:
    cands = [Path(os.environ["CAPSEM_BIN_DIR"]) / name] if "CAPSEM_BIN_DIR" in os.environ else []
    cands.extend(d / "cargo/debug" / name for d in _candidate_target_dirs())
    if which_bin := shutil.which(name):
        cands.append(Path(which_bin))
    for cand in cands:
        if cand.is_file():
            return cand
    pytest.skip(f"Could not locate {name} binary for live acceptance test")
    return Path(name)


def _resolve_assets_dir() -> Path:
    cands = [Path(os.environ["CAPSEM_ASSETS_DIR"])] if "CAPSEM_ASSETS_DIR" in os.environ else []
    cands.extend(d / "assets" for d in _candidate_target_dirs())
    cands.append(Path.home() / ".capsem/assets")
    for cand in cands:
        if cand.is_dir():
            return cand / "x86_64" if (cand / "x86_64").is_dir() else cand
    pytest.skip("Missing Capsem 0.7 VM assets directory")
    return Path()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def live_capsem_env() -> Any:
    project = _resolve_vertex_project()
    tmp_parent = Path("/var/tmp") if Path("/var/tmp").is_dir() else None
    capsem_home = Path(tempfile.mkdtemp(prefix="capsem-harbor-home-", dir=tmp_parent))
    assets_dir, run_dir = _resolve_assets_dir(), capsem_home / "run"
    creds_path = capsem_home / "credential-store.json"
    run_dir.mkdir(parents=True, exist_ok=True)
    default_creds = Path.home() / ".capsem/credentials/credential-store.json"
    if default_creds.is_file():
        shutil.copy2(default_creds, creds_path)
    srcs = '"127.0.0.1:5055", "localhost:5055", "127.0.0.1:5055/inspect-capsem/build"'
    admit = '"127.0.0.1:5055/inspect-capsem/build", "localhost:5055/inspect-capsem/build"'
    (capsem_home / "settings.toml").write_text(
        f"[images]\nsources=[{srcs}]\nadmit=[{admit}]\n"
    )
    svc_bin = _resolve_capsem_bin("capsem-service")
    proc_bin, gw_bin = _resolve_capsem_bin("capsem-process"), _resolve_capsem_bin("capsem-gateway")
    port, prefix, uds_path = _free_port(), f"hb-live-{os.getpid()}", run_dir / "capsem-service.sock"
    svc_env = {**os.environ, "CAPSEM_RUN_DIR": str(run_dir), "CAPSEM_HOME": str(capsem_home)}
    svc_env["CAPSEM_CREDENTIAL_STORE_PATH"] = str(creds_path)
    cmd_str = (
        f"{svc_bin} --uds-path {uds_path} --assets-dir {assets_dir} --process-binary {proc_bin} "
        f"--gateway-port {port} --gateway-binary {gw_bin} --parent-pid {os.getpid()} --foreground"
    )
    with (run_dir / "service.log").open("w", encoding="utf-8") as log_file:
        svc_proc = subprocess.Popen(cmd_str.split(), env=svc_env, stdout=log_file, stderr=log_file)
    token_file = run_dir / "gateway.token"
    for _ in range(150):
        if token_file.is_file() and token_file.read_text(encoding="utf-8").strip():
            break
        time.sleep(0.1)
    else:
        svc_proc.terminate()
        log_tail = (run_dir / "service.log").read_text(encoding="utf-8")[-2000:]
        shutil.rmtree(capsem_home, ignore_errors=True)
        raise RuntimeError(f"capsem-service failed to write gateway.token:\n{log_tail}")
    tok = token_file.read_text(encoding="utf-8").strip()
    test_env = {**svc_env, "CAPSEM_GATEWAY_URL": f"http://127.0.0.1:{port}"}
    test_env.update({"CAPSEM_GATEWAY_TOKEN": tok, "CAPSEM_VM_PREFIX": prefix})
    test_env["VERTEXAI_PROJECT"] = project
    test_env["VERTEXAI_LOCATION"] = os.environ.get("VERTEXAI_LOCATION", "global")
    test_env["OPENAI_API_KEY"] = os.environ.get("OPENAI_API_KEY", "local-proxy")
    prev_env = {k: os.environ.get(k) for k in test_env}
    os.environ.update(test_env)
    try:
        yield {"capsem_home": capsem_home, "prefix": prefix, "port": port, "project": project}
    finally:
        svc_proc.terminate()
        with contextlib.suppress(Exception):
            svc_proc.wait(timeout=10)
        shutil.rmtree(capsem_home, ignore_errors=True)
        for k, v in prev_env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


async def _assert_zero_leaked_vms(prefix: str) -> None:
    await CapsemEnvironment.cleanup_untracked_vms()
    ctrl = SdkCapsemController()
    try:
        vms, lbl = await ctrl.list_vms(), "capsem-vm-prefix"
        leaked = [v.id for v in vms if (v.labels or {}).get(lbl) == prefix]
        assert leaked == [], f"Leaked VMs for prefix {prefix}: {leaked}"
    finally:
        await ctrl.close()


def _validate_trajectory_cli(traj_path: Path) -> None:
    cmd = [sys.executable, "-m", "harbor.utils.trajectory_validator", str(traj_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"trajectory_validator failed: {proc.stdout}\n{proc.stderr}"


async def _run_harbor_trial(task_dir: Path, trials_dir: Path, **kw: Any) -> tuple[Any, Path]:
    from harbor.trial.trial import Trial

    model = kw.get("model_name", "vertex_ai/gemini-3.5-flash")
    aname = kw.get("agent_name", AgentName.TERMINUS_2.value)
    ak, aenv = kw.get("agent_kwargs", {}), kw.get("agent_env", {})
    agent_cfg = AgentConfig(name=aname, model_name=model, kwargs=ak, env=aenv)
    ekw = {"export_trace": True, "broker_credentials": True}
    env_cfg = EnvironmentConfig(import_path="harbor_capsem:CapsemEnvironment", kwargs=ekw)
    tcfg = TaskConfig(path=task_dir)
    cfg = TrialConfig(task=tcfg, trials_dir=trials_dir, agent=agent_cfg, environment=env_cfg)
    res = await (await Trial.create(cfg)).run()
    return res, next(p for p in trials_dir.iterdir() if p.is_dir())


async def test_live_three_benchmarks_host_agent(
    live_capsem_env: dict[str, Any], tmp_path: Path
) -> None:
    fix_git_dir = _find_cached_task("terminal-bench/fix-git/*")
    camel_dir = _find_cached_task("camel-ai/1361/*")
    quix_dir = _find_cached_task("quixbugs/python-minimum_spanning_tree/*")
    res_tb, tdir_tb = await _run_harbor_trial(fix_git_dir, tmp_path / "tb_fix_git")
    assert res_tb.verifier_result and res_tb.verifier_result.rewards.get("reward") == 1.0
    assert len(list(tdir_tb.glob("*.session.db"))) == 1
    traj_tb = tdir_tb / "agent" / "trajectory.json"
    _validate_trajectory_cli(traj_tb)
    traj_data = json.loads(traj_tb.read_text(encoding="utf-8"))
    assert traj_data["extra"]["capsem_summary"]["join_mode"] == "timestamp_window"
    assert traj_data["extra"]["capsem_summary"]["exec_events_count"] > 0
    assert any("capsem_events" in s.get("extra", {}) for s in traj_data["steps"])
    for slug, tdir in (("camel_1361", camel_dir), ("quix_mst", quix_dir)):
        res, out_dir = await _run_harbor_trial(tdir, tmp_path / slug)
        assert res.verifier_result and res.verifier_result.rewards.get("reward") == 1.0
        assert len(list(out_dir.glob("*.session.db"))) == 1
        _validate_trajectory_cli(out_dir / "agent" / "trajectory.json")
    await _assert_zero_leaked_vms(live_capsem_env["prefix"])


@contextlib.asynccontextmanager
async def _started_fix_git_env(tmp_path: Path, name: str) -> AsyncGenerator[CapsemEnvironment]:
    fix_git_dir = _find_cached_task("terminal-bench/fix-git/*")
    tpaths = TrialPaths(trial_dir=tmp_path / name)
    tpaths.mkdir()
    tcfg = TaskEnvConfig(docker_image="alexgshaw/fix-git:20251031", cpus=2, memory_mb=4096)
    kw: dict[str, Any] = {"broker_credentials": True, "export_trace": True}
    edir = fix_git_dir / "environment"
    env = CapsemEnvironment(edir, name, f"{name}__session", tpaths, tcfg, **kw)
    await env.start(force_build=False)
    try:
        yield env
    finally:
        await env.stop(delete=True)


async def test_live_in_vm_cli_credential_broker_and_turn_id(
    live_capsem_env: dict[str, Any], tmp_path: Path
) -> None:
    prefix, proj = live_capsem_env["prefix"], live_capsem_env["project"]
    google_tok, fix_git_dir = _read_google_token(), _find_cached_task("terminal-bench/fix-git/*")
    cli_env = {
        "GEMINI_API_KEY": google_tok,
        "GEMINI_API_KEY_AUTH_MECHANISM": "bearer",
        "GOOGLE_GEMINI_BASE_URL": "https://aiplatform.googleapis.com",
        "GOOGLE_GENAI_API_VERSION": f"v1beta1/projects/{proj}/locations/global/publishers/google",
    }
    kw = {"agent_name": AgentName.GEMINI_CLI.value, "model_name": "google/gemini-2.5-flash"}
    res_cli, tdir_cli = await _run_harbor_trial(
        fix_git_dir, tmp_path / "tb_fix_git_gemini_cli", agent_env=cli_env, **kw
    )
    assert res_cli.verifier_result and res_cli.verifier_result.rewards.get("reward") == 1.0
    db_files = list(tdir_cli.glob("*.session.db"))
    assert len(db_files) == 1
    sql = "SELECT provider, model, turn_id, input_tokens FROM model_calls"
    with contextlib.closing(sqlite3.connect(db_files[0])) as conn:
        mc_rows = conn.execute(sql).fetchall()
        exec_cmds = [r[0] for r in conn.execute("SELECT command FROM exec_events").fetchall()]
    assert len(mc_rows) >= 1 and mc_rows[0][0] == "google"
    assert "gemini-2.5-flash" in str(mc_rows[0][1])
    assert mc_rows[0][2] is not None and int(mc_rows[0][3] or 0) > 0
    assert all(google_tok not in cmd for cmd in exec_cmds)
    brokered_refs = [m.group(1) for cmd in exec_cmds if (m := _EXEC_CRED_RE.search(cmd))]
    assert brokered_refs and all(_CRED_REF_RE.match(ref) for ref in brokered_refs)
    traj_path = tdir_cli / "agent" / "trajectory.json"
    _validate_trajectory_cli(traj_path)
    enriched = json.loads(traj_path.read_text(encoding="utf-8"))
    assert enriched["agent"]["name"] == AgentName.GEMINI_CLI.value
    assert enriched["extra"]["capsem_summary"]["join_mode"] == "turn_id"
    agent_steps = [s for s in enriched["steps"] if s.get("source") == "agent"]
    mc = agent_steps[0]["extra"]["capsem_model_call"]
    assert agent_steps and "gemini-2.5-flash" in str(mc["model"])
    await _assert_zero_leaked_vms(prefix)


async def test_live_local_proxy_and_custom_base_url(
    live_capsem_env: dict[str, Any], tmp_path: Path
) -> None:
    prefix, fix_git_dir = live_capsem_env["prefix"], _find_cached_task("terminal-bench/fix-git/*")
    ak, aenv = {"api_base": "http://127.0.0.1:11434/v1"}, {"OPENAI_API_KEY": "local-proxy"}
    model, pdir = "openai/google/gemini-3.5-flash", tmp_path / "local_proxy"
    res_p, tdir_p = await _run_harbor_trial(
        fix_git_dir, pdir, model_name=model, agent_kwargs=ak, agent_env=aenv
    )
    assert res_p.verifier_result and res_p.verifier_result.rewards.get("reward") == 1.0
    _validate_trajectory_cli(tdir_p / "agent" / "trajectory.json")
    async with _started_fix_git_env(tmp_path, "custom_base_url") as env:
        check_cmd = (
            'python3 -c \'import os; print("OPENAI_KEY=" + os.environ["OPENAI_API_KEY"]); '
            'print("GEMINI_KEY=" + os.environ["GEMINI_API_KEY"])\''
        )
        l_env = {"OPENAI_BASE_URL": "http://127.0.0.1:11434/v1", "OPENAI_API_KEY": "sk-real-local", "GEMINI_API_KEY": "AIzaSyLiveBrokerCheckKey"}
        res_local = await env.exec(check_cmd, env=l_env)
        out = res_local.stdout or ""
        assert res_local.return_code == 0 and "sk-real-local" not in out
        for prefix_key in ("OPENAI_KEY=", "GEMINI_KEY="):
            line = next(ln for ln in out.splitlines() if ln.startswith(prefix_key))
            assert _CRED_REF_RE.match(line.split("=", 1)[1].strip())
    await _assert_zero_leaked_vms(prefix)
