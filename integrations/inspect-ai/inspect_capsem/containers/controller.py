"""Container runtime boundary types (`CommandResult`, `CapsemController`)."""

from __future__ import annotations

from typing import Protocol


class CommandResult(Protocol):
    """Structural protocol for command execution results inside a Capsem VM or container."""

    @property
    def exit_code(self) -> int: ...
    @property
    def stdout(self) -> str: ...
    @property
    def stderr(self) -> str: ...
    @property
    def truncated(self) -> bool: ...


class CapsemController(Protocol):
    """Protocol abstracting Capsem host/gateway operations."""

    async def exec_in_vm(
        self, vm_id: str, command: str, *, timeout: int = 120
    ) -> CommandResult: ...
    async def upload_to_vm(self, vm_id: str, guest_path: str, data: bytes) -> None: ...
