"""Tests for gateway discovery, path sanitization, and structured SDK errors."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import capsem
import pytest
from capsem import (
    DEFAULT_GATEWAY_URL,
    VM,
    CapsemError,
    CreateTimeoutError,
    ExecTimeoutError,
    GatewayEndpoint,
    HttpError,
    Hypervisor,
    InvalidPathError,
    VmNotFoundError,
    capsem_run_dir,
    discover_gateway,
    models,
    sanitize_file_path,
)

from .facade_gateway import gateway


def test_capsem_run_dir_priority(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("CAPSEM_RUN_DIR", raising=False)
    monkeypatch.delenv("CAPSEM_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert capsem_run_dir() == tmp_path / ".capsem" / "run"
    monkeypatch.setenv("CAPSEM_HOME", str(tmp_path / "custom-home"))
    assert capsem_run_dir() == tmp_path / "custom-home" / "run"
    monkeypatch.setenv("CAPSEM_RUN_DIR", str(tmp_path / "explicit-env-run"))
    assert capsem_run_dir() == tmp_path / "explicit-env-run"
    assert capsem_run_dir(tmp_path / "arg-run") == tmp_path / "arg-run"
    with pytest.raises(ValueError, match="run_dir"):
        capsem_run_dir("   ")


def test_discover_gateway_from_files_and_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for var in ("CAPSEM_GATEWAY_URL", "CAPSEM_GATEWAY_TOKEN", "CAPSEM_RUN_DIR", "CAPSEM_HOME"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(RuntimeError, match="gateway bearer token not found"):
        discover_gateway(run_dir=tmp_path)
    (tmp_path / "gateway.token").write_text("file-token\n", encoding="utf-8")
    assert discover_gateway(run_dir=tmp_path) == GatewayEndpoint(
        url=DEFAULT_GATEWAY_URL, token="file-token", run_dir=tmp_path
    )
    (tmp_path / "gateway.port").write_text(" 19444\n", encoding="utf-8")
    assert discover_gateway(run_dir=tmp_path).url == "http://127.0.0.1:19444"
    for bad_port in ("not-a-port", "0", "70000"):
        (tmp_path / "gateway.port").write_text(f"{bad_port}\n", encoding="utf-8")
        with pytest.raises(ValueError, match="invalid port"):
            discover_gateway(run_dir=tmp_path)
    monkeypatch.setenv("CAPSEM_GATEWAY_URL", "http://127.0.0.1:19666/")
    monkeypatch.setenv("CAPSEM_GATEWAY_TOKEN", "env-token")
    ep_env = discover_gateway(run_dir=tmp_path)
    assert (ep_env.url, ep_env.token) == ("http://127.0.0.1:19666", "env-token")
    ep_explicit = discover_gateway(
        url="http://127.0.0.1:19777", token="arg-token", run_dir=tmp_path
    )
    assert (ep_explicit.url, ep_explicit.token) == ("http://127.0.0.1:19777", "arg-token")


def test_hypervisor_connect_and_vm_use_discovered_gateway(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def run() -> None:
        async with gateway() as (url, _):
            port = url.rsplit(":", 1)[1]
            monkeypatch.delenv("CAPSEM_GATEWAY_URL", raising=False)
            monkeypatch.delenv("CAPSEM_GATEWAY_TOKEN", raising=False)
            (tmp_path / "gateway.port").write_text(f"{port}\n", encoding="utf-8")
            (tmp_path / "gateway.token").write_text("token\n", encoding="utf-8")
            async with (
                Hypervisor.connect(run_dir=tmp_path) as hv,
                VM(run_dir=tmp_path, id="vm-0") as vm,
            ):
                assert isinstance(await hv.info(), models.HypervisorInfo)
                assert isinstance(await vm.info(), models.SandboxInfo)

    asyncio.run(run())


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("<script>alert(1)</script>.txt", "scriptalert1/script.txt"),
        ("foo\x00bar.txt", "foobar.txt"),
        ("foo\u200bbar.txt", "foobar.txt"),
        ("café/Ａ٣.txt", "caf/.txt"),
        ("foo//bar///baz", "foo/bar/baz"),
        ("/foo/bar", "foo/bar"),
        ("foo/bar.txt", "foo/bar.txt"),
        ("my-file_v2.tar.gz", "my-file_v2.tar.gz"),
    ],
)
def test_sanitize_file_path_matches_service_allowlist(raw: str, expected: str) -> None:
    assert sanitize_file_path(raw) == expected
    assert capsem.files.sanitize_file_path(raw) == expected


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("", "empty path after sanitization"),
        ("///", "empty path after sanitization"),
        ("<>!@#", "empty path after sanitization"),
        ("../etc/passwd", "path traversal rejected"),
        ("foo/../bar", "path traversal rejected"),
        (".<>.", "path traversal rejected"),
    ],
)
def test_sanitize_file_path_rejects_empty_and_traversal(raw: str, message: str) -> None:
    with pytest.raises(InvalidPathError, match=message) as caught:
        sanitize_file_path(raw)
    assert isinstance(caught.value, CapsemError) and isinstance(caught.value, ValueError)


def test_sanitize_file_path_rejects_non_string() -> None:
    bad: Any = 123
    with pytest.raises(TypeError, match="path must be a string"):
        sanitize_file_path(bad)


def test_vm_not_found_error_on_name_resolution_and_404_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        async with gateway() as (url, state), Hypervisor(url, "token") as hv:
            state.names = []
            with pytest.raises(VmNotFoundError) as by_name_err:
                await hv.vm(name="gone").info()
            err = by_name_err.value
            assert isinstance(err, (HttpError, LookupError, CapsemError))
            assert (err.status, err.vm_name, err.vm_id) == (404, "gone", None)

            state.names = ["dup", "dup"]
            with pytest.raises(LookupError, match="found 2") as dup_err:
                await hv.vm(name="dup").info()
            assert not isinstance(dup_err.value, VmNotFoundError)

            vm = hv.vm(id="missing-id")
            with pytest.raises(HttpError) as file_err:
                await vm.files.read("/missing")
            assert not isinstance(file_err.value, VmNotFoundError)

            request = hv._transport.request

            async def non_vm_404(method: Any, path: str, **kwargs: Any) -> bytes:
                if path.endswith("/logs") or path.endswith("/exec"):
                    raise HttpError(404, json.dumps({"error": "no app log found"}))
                return await request(method, path, **kwargs)

            monkeypatch.setattr(hv._transport, "request", non_vm_404)
            for non_vm_call in (lambda: vm.log(), lambda: vm.exec("true")):
                with pytest.raises(HttpError) as plain_404:
                    await non_vm_call()
                assert not isinstance(plain_404.value, VmNotFoundError)

            async def sandbox_404(method: Any, path: str, **kwargs: Any) -> bytes:
                params = kwargs.get("path_parameters") or {}
                if params.get("id") == "missing-id":
                    body = {"error": "not found", "code": "vm_not_found", "vm_id": "missing-id"}
                    raise HttpError(404, json.dumps(body))
                return await request(method, path, **kwargs)

            monkeypatch.setattr(hv._transport, "request", sandbox_404)
            calls = (
                lambda: vm.info(),
                lambda: vm.exec("true"),
                lambda: vm.delete(),
                lambda: vm.fork("child"),
                lambda: vm.log(),
                lambda: vm.stats.summary(),
                lambda: vm.files.read("/work/a"),
                lambda: vm.files.write("/work/a", b"x"),
                lambda: vm.files.list(),
            )
            for call in calls:
                with pytest.raises(VmNotFoundError) as route_err:
                    await call()
                assert (route_err.value.vm_id, route_err.value.status) == ("missing-id", 404)

    asyncio.run(run())


def test_exec_timeout_error_on_http_and_client_deadlines(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        async with gateway() as (url, _), Hypervisor(url, "token") as hv:
            vm = hv.vm(id="vm-0")

            async def ipc_timeout(method: Any, path: str, **_kwargs: Any) -> bytes:
                body = {"error": "timed out", "code": "exec_timeout", "timeout_secs": 17}
                raise HttpError(500, json.dumps(body))

            monkeypatch.setattr(hv._transport, "request", ipc_timeout)
            with pytest.raises(ExecTimeoutError) as exec_500:
                await vm.exec("sleep 20", timeout_secs=20)
            assert isinstance(exec_500.value, TimeoutError)
            assert (exec_500.value.status, exec_500.value.vm_id) == (500, "vm-0")
            assert (exec_500.value.command, exec_500.value.timeout_secs) == ("sleep 20", 17)

            with pytest.raises(ExecTimeoutError) as run_500:
                await hv.run("sleep 20", timeout_secs=20)
            assert (run_500.value.status, run_500.value.timeout_secs) == (500, 17)

            async def gateway_504(method: Any, path: str, **_kwargs: Any) -> bytes:
                raise HttpError(504, "gateway timeout")

            monkeypatch.setattr(hv._transport, "request", gateway_504)
            with pytest.raises(ExecTimeoutError) as exec_504:
                await vm.exec("sleep 9", timeout_secs=9)
            assert (exec_504.value.status, exec_504.value.timeout_secs) == (504, 9)

            async def non_timeout_500(method: Any, path: str, **_kwargs: Any) -> bytes:
                raise HttpError(500, json.dumps({"error": "IPC command timed out without code"}))

            monkeypatch.setattr(hv._transport, "request", non_timeout_500)
            for fn in (lambda: vm.exec("broken"), lambda: hv.run("broken")):
                with pytest.raises(HttpError) as plain_500:
                    await fn()
                assert not isinstance(plain_500.value, ExecTimeoutError)

            async def client_timeout(method: Any, path: str, **_kwargs: Any) -> bytes:
                raise TimeoutError("client deadline exceeded")

            monkeypatch.setattr(hv._transport, "request", client_timeout)
            with pytest.raises(ExecTimeoutError) as exec_client:
                await vm.exec("sleep 11", timeout_secs=11)
            assert not isinstance(exec_client.value, HttpError)
            assert (exec_client.value.status, exec_client.value.timeout_secs) == (None, 11)

            with pytest.raises(ExecTimeoutError) as run_client:
                await hv.run("sleep 12", timeout_secs=12)
            assert (run_client.value.status, run_client.value.timeout_secs) == (None, 12)

    asyncio.run(run())
