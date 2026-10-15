"""Connection ownership shared by the public gateway clients."""

from __future__ import annotations

from pathlib import Path
from typing import Self

from ._transport import Transport
from .discovery import discover_gateway


class Client:
    def __init__(self, url: str | None = None, token: str | None = None, *,
                 run_dir: str | Path | None = None, timeout: float = 30) -> None:
        explicit_run_dir = Path(run_dir).expanduser() if run_dir is not None else None
        resolved_run_dir = explicit_run_dir
        if url is None or token is None:
            endpoint = discover_gateway(url=url, token=token, run_dir=run_dir)
            url, token = endpoint.url, endpoint.token
            resolved_run_dir = endpoint.run_dir
        self.__transport = Transport(url, token, timeout=timeout)
        self.__transport._run_dir = resolved_run_dir
        self.__transport._explicit_run_dir = explicit_run_dir
        self._owns_connection = True
        self._closed = False

    @classmethod
    def _from_transport(cls, transport: Transport) -> Self:
        instance = cls.__new__(cls)
        instance.__transport = transport
        instance._owns_connection = False
        instance._closed = False
        return instance

    @property
    def _transport(self) -> Transport:
        if self._closed:
            raise RuntimeError("SDK client is closed")
        return self.__transport

    async def __aenter__(self) -> Self:
        if self._closed:
            raise RuntimeError("SDK client is closed")
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            if self._owns_connection:
                await self.__transport.close()
