"""Standalone Capsem SandboxEnvironment for Inspect AI."""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import logging
import shlex
import uuid
from typing import Any, ClassVar, Literal, overload

from inspect_ai.util import ExecResult, SandboxConnection, SandboxEnvironment
from inspect_ai.util import SandboxEnvironmentConfigType as _CfgType

from inspect_capsem import _lifecycle as _lc
from inspect_capsem._compose import coerce_config
from inspect_capsem._controller import CapsemController, SdkCapsemController
from inspect_capsem._exec import exec_in_sandbox
from inspect_capsem._files import read_guest_file, write_guest_file
from inspect_capsem._lifecycle import sweep_leftover_vms
from inspect_capsem._tools import bake_sandbox_tools_into_controller
from inspect_capsem.config import CapsemSandboxConfig

logger = logging.getLogger(__name__)
_INIT_TEARDOWN_TIMEOUT_SECS = _lc._INIT_TEARDOWN_TIMEOUT_SECS


async def sweep_process_owned_vms(
    task_name: str | None = None, controller: CapsemController | None = None
) -> list[str]:
    """Stop any VMs started by this process (or by `task_name`) that have not yet been stopped."""
    return await _lc.sweep_process_owned_vms(
        task_name=task_name, controller=controller, controller_factory=lambda: SdkCapsemController()
    )


@atexit.register
def _atexit_sweep_process_owned_vms() -> None:
    with contextlib.suppress(Exception):
        coro = sweep_process_owned_vms()
        asyncio.run(asyncio.wait_for(coro, timeout=_INIT_TEARDOWN_TIMEOUT_SECS))


class CapsemSandboxEnvironment(SandboxEnvironment):
    """Spec-compliant Inspect AI `SandboxEnvironment` backed by Capsem."""

    _active_environments: ClassVar[dict[str, CapsemSandboxEnvironment]] = {}

    def __init__(
        self,
        vm_id: str,
        controller: CapsemController | None = None,
        *,
        container_id: str | None = None,
        working_dir: str = "/workspace",
        execution_mode: Literal["vm", "container"] = "vm",
        owns_controller: bool | None = None,
        task_name: str | None = None,
        user: str | None = None,
    ) -> None:
        super().__init__()
        self._vm_id, self._working_dir, self._cleaned_up = vm_id, working_dir, False
        self._container_id, self._execution_mode = container_id, execution_mode
        self._controller = SdkCapsemController() if controller is None else controller
        self._owns_controller = (controller is None) if owns_controller is None else owns_controller
        self._task_name, self._user = task_name, user
        self._instance_id = f"{vm_id}:{container_id or 'vm'}:{uuid.uuid4().hex[:6]}"
        CapsemSandboxEnvironment._active_environments[self._instance_id] = self

    @property
    def vm_id(self) -> str:
        return self._vm_id

    @property
    def container_id(self) -> str | None:
        return self._container_id

    @property
    def execution_mode(self) -> Literal["vm", "container"]:
        return self._execution_mode

    @classmethod
    def config_files(cls) -> list[str]:
        compose = [f"{p}.{e}" for p in ("compose", "docker-compose") for e in ("yaml", "yml")]
        return [*compose, "Dockerfile", "Containerfile"]

    @classmethod
    def is_docker_compatible(cls) -> bool:
        return True

    @classmethod
    def default_concurrency(cls) -> int | None:
        return 4

    @classmethod
    def config_deserialize(cls, config: dict[str, Any]) -> CapsemSandboxConfig:
        return coerce_config(config, resolve_compose=False)

    @classmethod
    async def task_init(cls, task_name: str, config: _CfgType | str | None) -> None:
        del task_name, config
        with contextlib.suppress(Exception):
            controller = SdkCapsemController()
            try:
                coro = sweep_leftover_vms(controller)
                await asyncio.wait_for(coro, timeout=_INIT_TEARDOWN_TIMEOUT_SECS)
            finally:
                await controller.close()

    @classmethod
    async def task_cleanup(
        cls, task_name: str, config: _CfgType | str | None, cleanup: bool
    ) -> None:
        del config
        envs = [e for e in cls._active_environments.values() if task_name in (None, e._task_name)]
        if not cleanup:
            surviving_ids = _lc._abandon_process_owned_vms(task_name)
            for env in envs:
                cls._active_environments.pop(env._instance_id, None)
                if env._vm_id and env._vm_id not in surviving_ids:
                    surviving_ids.append(env._vm_id)
                if env._owns_controller:
                    env._owns_controller = False
                    with contextlib.suppress(Exception):
                        await env._controller.close()
            _lc._report_surviving_vms(surviving_ids)
            return

        for env in envs:
            with contextlib.suppress(Exception):
                await env.cleanup()
        with contextlib.suppress(Exception):
            coro = sweep_process_owned_vms(task_name)
            await asyncio.wait_for(coro, timeout=_INIT_TEARDOWN_TIMEOUT_SECS)

    @classmethod
    async def sample_init(
        cls, task_name: str, config: _CfgType | str | None, metadata: dict[str, str]
    ) -> dict[str, SandboxEnvironment]:
        cfg, controller, vm_id = (
            coerce_config(config, sample_metadata=metadata),
            SdkCapsemController(),
            "",
        )
        try:
            vm_id = await _lc._init_sample_vm(controller, cfg, task_name)
            container_id, working_dir = None, cfg.working_dir
            if cfg.execution_mode == "container":
                from inspect_capsem import containers as _c

                spec = cfg.to_container_spec()
                container_id = await _c.prepare_oci_workload_container(controller, vm_id, spec)
                if not spec.working_dir_explicit:
                    working_dir = await _c.resolve_container_working_dir(
                        controller, vm_id, container_id
                    )
            else:
                mkdir_cmd = f"mkdir -p {shlex.quote(cfg.working_dir)}"
                await controller.exec_in_vm(vm_id, mkdir_cmd, timeout=30)
            await bake_sandbox_tools_into_controller(controller, vm_id, container_id=container_id)
            env = cls(
                vm_id,
                controller,
                container_id=container_id,
                working_dir=working_dir,
                execution_mode=cfg.execution_mode,
                task_name=task_name,
                user=cfg.user,
            )
            env._owns_controller = True
            return {"default": env}
        except BaseException:
            try:
                if vm_id:
                    await _lc._teardown_failed_init(controller, vm_id, _INIT_TEARDOWN_TIMEOUT_SECS)
            finally:
                with contextlib.suppress(Exception):
                    await controller.close()
            raise

    @classmethod
    async def sample_cleanup(
        cls,
        task_name: str,
        config: _CfgType | str | None,
        environments: dict[str, SandboxEnvironment],
        interrupted: bool,
    ) -> None:
        del task_name, config, interrupted
        for env in environments.values():
            if isinstance(env, CapsemSandboxEnvironment):
                await env.cleanup()

    @classmethod
    async def cli_cleanup(cls, id: str | None) -> None:
        stopped_vms: set[str] = set()
        for env in list(cls._active_environments.values()):
            if id is None or id in (env.vm_id, env.container_id, env._instance_id):
                stopped_vms.add(env.vm_id)
                await env.cleanup()
        if id is None:
            stopped_vms.update(await sweep_process_owned_vms())
        await _lc._sweep_untracked_managed_vms(lambda: SdkCapsemController(), id, stopped_vms)

    async def cleanup(self) -> None:
        CapsemSandboxEnvironment._active_environments.pop(self._instance_id, None)
        try:
            if not self._cleaned_up and self._vm_id:
                try:
                    await self._controller.stop_vm(self._vm_id)
                    self._cleaned_up = True
                    _lc._unregister_process_owned_vm(self._vm_id)
                except Exception as exc:
                    logger.warning("Failed to stop Capsem VM %s: %s", self._vm_id, exc)
        finally:
            if self._owns_controller:
                self._owns_controller = False
                with contextlib.suppress(Exception):
                    await self._controller.close()

    async def exec(
        self,
        cmd: list[str],
        input: str | bytes | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        user: str | None = None,
        timeout: int | None = None,
        timeout_retry: bool = True,
        concurrency: bool = True,
    ) -> ExecResult[str]:
        del timeout_retry, concurrency
        kw: dict[str, Any] = {
            "working_dir": self._working_dir,
            "default_user": self._user,
            "input_data": input,
            "cwd": cwd,
            "env_vars": env,
            "user": user,
            "timeout": timeout,
            "container_id": self._container_id,
        }
        return await exec_in_sandbox(self._controller, self._vm_id, cmd, **kw)

    async def write_file(self, file: str, contents: str | bytes) -> None:
        c, u = self._container_id, self._user
        await write_guest_file(
            self._controller,
            self._vm_id,
            self._working_dir,
            file,
            contents,
            container_id=c,
            default_user=u,
        )

    @overload
    async def read_file(self, file: str, text: Literal[True] = True) -> str: ...
    @overload
    async def read_file(self, file: str, text: Literal[False]) -> bytes: ...

    async def read_file(self, file: str, text: bool = True) -> str | bytes:
        return await read_guest_file(
            self._controller, self._vm_id, self._working_dir, file, text=text
        )

    async def connection(self, *, user: str | None = None) -> SandboxConnection:
        del user
        cmd = f"capsem exec {shlex.quote(self._vm_id)} -- bash"
        return SandboxConnection(type="capsem", command=cmd, container=self._vm_id)


__all__ = ["CapsemSandboxEnvironment", "sweep_leftover_vms", "sweep_process_owned_vms"]
