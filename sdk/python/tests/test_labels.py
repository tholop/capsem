"""VM label propagation and validation in the Python SDK."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from capsem import Hypervisor

from .facade_gateway import gateway


def test_create_attaches_labels_and_exposes_them_on_sandbox_info_and_list() -> None:
    async def run() -> None:
        async with gateway() as (url, state), Hypervisor(url, "token") as hv:
            labels = {"suite": "eval", "sample": "42"}
            vm = await hv.create(labels=labels)
            body = json.loads(state.requests[-1][2])
            assert body["persistent"] is False
            assert body["labels"] == labels
            info = await vm.info()
            assert info.persistent is False
            assert info.labels == labels

            # Empty labels dict is omitted from the wire request.
            await hv.create(labels={})
            empty_body = json.loads(state.requests[-1][2])
            assert empty_body.get("labels") is None

            state.names = ["vm-a", "vm-b"]
            state.sandbox_labels = {"vm-0": {"suite": "eval"}, "vm-1": None}
            listed = await hv.list()
            assert [s.labels for s in listed.sandboxes] == [{"suite": "eval"}, None]

    asyncio.run(run())


@pytest.mark.parametrize("bad_labels", ["not-a-dict", [("a", "b")], {1: "v"}, {"k": 2}])
def test_invalid_label_types_are_rejected_before_network(bad_labels: Any) -> None:
    async def run() -> None:
        async with Hypervisor("http://127.0.0.1:1", "token") as hv:
            with pytest.raises(TypeError, match="labels"):
                await hv.create(labels=bad_labels)

    asyncio.run(run())


@pytest.mark.parametrize(
    "bad_labels",
    [
        {"": "v"},
        {"k" * 64: "v"},
        {"bad key": "v"},
        {"bad:key": "v"},
        {"café": "v"},
        {"k": "v" * 256},
        {"k": "bad\nval"},
        {"k": "bad\x00val"},
        {"k": "bad\x7fval"},
        {"k": "bad\x85val"},
        {f"k{i}": "v" for i in range(65)},
    ],
)
def test_invalid_label_constraints_are_rejected_before_network(bad_labels: dict[str, str]) -> None:
    async def run() -> None:
        async with Hypervisor("http://127.0.0.1:1", "token") as hv:
            with pytest.raises(ValueError, match="label"):
                await hv.create(labels=bad_labels)

    asyncio.run(run())
