"""SDK acceptance owns disposable VMs and communicates through the TCP gateway."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from helpers.gateway import GatewayInstance
from helpers.service import ServiceInstance

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.integration
@pytest.mark.parametrize("language", ["python", "typescript"])
def test_sdk_live_vm_lifecycle_and_binary_files(language: str) -> None:
    service = ServiceInstance()
    gateway = GatewayInstance(service.uds_path)
    try:
        service.start()
        gateway.start()
        result = subprocess.run(
            ["uv", "run", "--frozen", "--no-sync", "python", "-m", "tests.live_acceptance"]
            if language == "python" else ["node", "tools/live-acceptance.mjs"],
            cwd=ROOT / "sdk" / language, env={
                **{key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"},
                "SDK_GATEWAY_URL": gateway.base_url, "SDK_GATEWAY_TOKEN": gateway.token,
            }, capture_output=True, text=True, timeout=240, check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "SDK_LIVE_ACCEPTANCE_OK" in result.stdout
        print(result.stdout.strip())
    finally:
        gateway.stop()
        service.stop()


@pytest.mark.integration
def test_inspect_ai_live_vm_sandbox_acceptance() -> None:
    service = ServiceInstance()
    gateway = GatewayInstance(service.uds_path)
    try:
        service.start()
        gateway.start()
        result = subprocess.run(
            [
                "uv",
                "run",
                "--frozen",
                "--no-sync",
                "python",
                "-m",
                "pytest",
                "-q",
                "-s",
                "-o",
                "addopts=",
                "tests/test_live.py",
                "-k",
                "test_live_vm_sandbox_acceptance",
            ],
            cwd=ROOT / "integrations" / "inspect-ai",
            env={
                **{key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"},
                "CAPSEM_GATEWAY_URL": gateway.base_url,
                "CAPSEM_GATEWAY_TOKEN": gateway.token,
                "CAPSEM_LIVE_VM_ACCEPTANCE": "1",
            },
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "INSPECT_CAPSEM_VM_ACCEPTANCE_OK" in result.stdout
        print(result.stdout.strip())
    finally:
        gateway.stop()
        service.stop()
