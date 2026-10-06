"""Hypervisor overview, VM creation, logs and updates through the gateway."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Self

from . import _operations as api
from . import models
from ._client import Client
from ._debug import Debug
from ._mcp import Mcp
from ._networks import Networks
from ._validation import (
    _create_vm_with_timeout,
    _exec_with_timeout,
    _memory_mb,
    _validate_labels,
)
from .errors import CreateTimeoutError
from .execution import (
    CREATE_READY_SECS,
    GATEWAY_REQUEST_BUDGET_SECS,
    ExecResult,
    command_deadline,
)
from .registry import Registry
from .vm import VM

__all__ = ["CreateTimeoutError", "Hypervisor"]


class Hypervisor(Client):
    def __init__(
        self,
        url: str | None = None,
        token: str | None = None,
        *,
        run_dir: str | Path | None = None,
        timeout: float = 30,
    ) -> None:
        super().__init__(url, token, run_dir=run_dir, timeout=timeout)
        self.networks = Networks(self._transport)
        self.mcp = Mcp(self._transport)
        self.debug = Debug(self._transport)

    @classmethod
    def connect(
        cls,
        url: str | None = None,
        token: str | None = None,
        *,
        run_dir: str | Path | None = None,
        timeout: float = 30,
    ) -> Self:
        """Create a `Hypervisor` client using gateway discovery when `url` or `token` is omitted."""
        return cls(url, token, run_dir=run_dir, timeout=timeout)

    async def info(self) -> models.HypervisorInfo:
        return await api.get_hypervisor_info(self._transport)

    async def list(self) -> models.ListResponse:
        return await api.list_vms(self._transport)

    def vm(self, *, id: str | None = None, name: str | None = None) -> VM:
        """Attach by ID or name, borrowing this connection without making a request."""
        return VM._attach(self._transport, id=id, name=name)

    async def create(
        self,
        *,
        name: str = "",
        cpus: int | None = None,
        memory: int | None = None,
        env: dict[str, str] | None = None,
        labels: Mapping[str, str] | None = None,
        networks: Sequence[models.NetworkInfo] = (),
        image: str | None = None,
        command: Sequence[str] = (),
        registry: Registry | None = None,
    ) -> VM:
        """Provision a VM or container workload.

        Optional advisory `labels` (up to 64 key/value pairs; keys `1..=63` ASCII
        `[A-Za-z0-9._/-]`, values `<= 255` UTF-8 bytes without control characters)
        are attached at creation, remain immutable afterward, survive `persist`
        and `resume`, and are not inherited by `fork`.
        """
        normalized_labels = _validate_labels(labels)
        if cpus is not None and (isinstance(cpus, bool) or not isinstance(cpus, int) or cpus < 1):
            raise ValueError("cpus must be positive")
        if image is None and (command or registry is not None):
            raise ValueError("container command and registry require an image")
        if registry is not None and not isinstance(registry, Registry):
            raise TypeError("registry must be a Registry object")
        if image is not None and (not isinstance(image, str) or not image):
            raise ValueError("image must be a nonempty string")
        network_names: list[str] = []
        for network in networks:
            if not isinstance(network, models.NetworkInfo):
                raise TypeError("networks must contain NetworkInfo objects returned by capsem.networks")
            network_names.append(network.name)
        wire: models.ContainerSpec | None = None
        if image is not None:
            wire = models.ContainerSpec(
                image=image, args=list(command), env=env or {}, attach=False,
            ) if registry is None else models.ContainerSpec(
                image=image, args=list(command), env=env or {}, registry=registry._wire(), attach=False,
            )
        request = models.ProvisionRequest(
            name=name or None, persistent=bool(name),
            cpus=cpus, ram_mb=_memory_mb(memory),
            env=env if image is None else None, networks=network_names,
        )
        if normalized_labels:
            request.labels = normalized_labels
        if wire is not None:
            request.container = wire
        # The service answers only once a container workload is ready, so
        # wait at least as long as it does before giving up on the create.
        request_timeout = max(
            self._transport.timeout, CREATE_READY_SECS + GATEWAY_REQUEST_BUDGET_SECS
        )
        return await _create_vm_with_timeout(
            api.create_vm(
                self._transport,
                body=request,
                request_timeout=request_timeout,
            ),
            self._transport,
            bind_vm=VM._bind,
            name=name,
            container=image is not None,
            request_timeout=request_timeout,
        )

    async def log(self, source: models.HostLogSource = models.HostLogSource.SERVICE, *,
                  grep: str | None = None, tail: int | None = None,
                  max_bytes: int | None = None) -> models.HostLogsResponse:
        return await api.get_hypervisor_logs(self._transport, name=source, grep=grep, tail=tail, max_bytes=max_bytes)

    async def run(
        self,
        command: str,
        *,
        timeout_secs: int | None = None,
        cpus: int | None = None,
        memory: int | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        return await _exec_with_timeout(
            api.run_vm(
                self._transport,
                body=models.RunRequest(
                    command=command,
                    timeout_secs=timeout_secs,
                    cpus=cpus,
                    ram_mb=_memory_mb(memory),
                    env=env,
                ),
                request_timeout=command_deadline(self._transport.timeout, timeout_secs),
            ),
            command=command,
            timeout_secs=timeout_secs,
        )

    async def purge(self, *, all: bool = False) -> models.PurgeResponse:
        return await api.purge_vms(self._transport, body=models.PurgeRequest(all=all))

    async def update(self) -> models.UpdateActionResponse:
        return await api.update_hypervisor(self._transport, body=models.UpdateApplyRequest(confirmed=True))

    async def restart(self) -> models.RestartResponse:
        """Restart an idle managed service; reconnect with a new gateway token."""
        return await api.restart_hypervisor(self._transport)
