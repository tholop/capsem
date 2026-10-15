"""Archived body hydration (`session.bodies` / `/vms/{id}/bodies/{event_id}`) for `capsem.trace`."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import importlib
import json
import logging
import re
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, Literal, Protocol, runtime_checkable

from . import _operations as api
from ._transport import CapsemError, HttpError, Transport

logger = logging.getLogger(__name__)

BodiesMode = Literal["none", "previews", "http", "full"]
_EVENT_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_PREFETCH_CONCURRENCY = 8


def _attr(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    val = getattr(obj, key, default)
    return getattr(val, "value", val)


def validate_event_id(raw: Any) -> str:
    """Validate that `raw` is a 12-character lowercase hexadecimal Capsem `event_id`."""
    s = str(raw or "").strip()
    if not _EVENT_ID_RE.fullmatch(s):
        raise ValueError(f"event_id must be 12 lowercase hex characters; got {raw!r}")
    return s


@runtime_checkable
class BodyReader(Protocol):
    """Protocol for synchronous or pre-fetched event body readers."""

    def get_event_bodies(self, event_id: str) -> Sequence[Any]: ...


class HttpBodyReader:
    """Async/prefetching body reader backed by `GET /vms/{id}/bodies/{event_id}`."""

    def __init__(
        self,
        transport_or_vm: Transport | Any,
        vm_id: str | None = None,
        *,
        max_bytes: int | None = None,
    ) -> None:
        if isinstance(transport_or_vm, Transport):
            self._transport = transport_or_vm
            self._vm_id = str(vm_id or "")
        else:
            self._transport = transport_or_vm._transport
            self._vm_id = str(vm_id or getattr(transport_or_vm, "_id", "") or "")
        self._max_bytes = max_bytes
        self._cache: dict[str, Sequence[Any]] = {}

    async def fetch_event_bodies(self, event_id: str) -> Sequence[Any]:
        """Fetch and cache archived bodies for `event_id` over HTTP, degrading on error."""
        eid = validate_event_id(event_id)
        if eid in self._cache:
            return list(self._cache[eid])
        try:
            resp = await api.get_vm_event_bodies(
                self._transport, id=self._vm_id, event_id=eid, max_bytes=self._max_bytes
            )
        except (HttpError, CapsemError) as exc:
            logger.warning(
                "Failed to fetch archived bodies for event %s on VM %s: %s", eid, self._vm_id, exc
            )
            self._cache[eid] = ()
            return []
        self._cache[eid] = resp.bodies
        return list(resp.bodies)

    async def prefetch(self, event_ids: Sequence[str]) -> None:
        """Prefetch a batch of `event_ids` concurrently into the local cache."""
        pending: list[str] = []
        seen: set[str] = set()
        for raw_id in event_ids:
            try:
                eid = validate_event_id(raw_id)
            except ValueError:
                continue
            if eid not in self._cache and eid not in seen:
                seen.add(eid)
                pending.append(eid)
        if not pending:
            return
        sem = asyncio.Semaphore(_PREFETCH_CONCURRENCY)

        async def _fetch_one(eid: str) -> None:
            async with sem:
                await self.fetch_event_bodies(eid)

        await asyncio.gather(*(_fetch_one(eid) for eid in pending))

    def get_event_bodies(self, event_id: str) -> Sequence[Any]:
        """Return pre-fetched bodies for `event_id` from the local cache."""
        eid = validate_event_id(event_id)
        return self._cache.get(eid, ())


def decode_security_payload(raw: bytes | Any) -> Any:
    """Decode a `security_rule_events` payload from UTF-8 JSON, `msgpack`, or a base64 envelope."""
    content_type: str | None = None
    if isinstance(raw, (bytes, bytearray, memoryview)):
        raw_bytes = bytes(raw)
    else:
        content_type = str(_attr(raw, "content_type") or "") or None
        raw_bytes = _decode_body_bytes(raw)
    if content_type and "msgpack" in content_type.lower():
        with contextlib.suppress(ImportError, ValueError, TypeError):
            return importlib.import_module("msgpack").unpackb(raw_bytes, raw=False)
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        with contextlib.suppress(ImportError, ValueError, TypeError):
            return importlib.import_module("msgpack").unpackb(raw_bytes, raw=False)
        b64 = base64.b64encode(raw_bytes).decode("ascii")
        envelope: dict[str, Any] = {"encoding": "base64", "data": b64}
        if content_type:
            envelope["content_type"] = content_type
        return envelope
    try:
        return json.loads(text)
    except ValueError:
        return text


def _decode_body_bytes(body: Any) -> bytes:
    raw_c = _attr(body, "content")
    if raw_c is None:
        raw_c = _attr(body, "data")
    content = str(raw_c or "")
    if str(_attr(body, "encoding") or "utf-8").lower() == "base64":
        return base64.b64decode(content)
    return content.encode("utf-8")


def _decode_body_text(body: Any) -> str:
    return _decode_body_bytes(body).decode("utf-8", errors="replace")


def _index_body_blobs(
    blobs: Sequence[Mapping[str, Any]],
) -> dict[str, dict[tuple[str, str], Mapping[str, Any]]]:
    by_event: dict[str, dict[tuple[str, str], Mapping[str, Any]]] = {}
    for row in blobs:
        eid, src, direction = row.get("event_id"), row.get("source_table"), row.get("direction")
        if isinstance(eid, str) and isinstance(src, str) and isinstance(direction, str):
            by_event.setdefault(eid, {})[(src, direction)] = row
    return by_event


def _body_needs_hydration(
    body_meta: Mapping[str, Any] | None, preview: Any, *, full: bool = False
) -> bool:
    if body_meta is None:
        return False
    if full or bool(body_meta.get("truncated")):
        return True
    if preview is None:
        return False
    return int(body_meta.get("stored_bytes") or 0) > len(str(preview).encode("utf-8"))


def _iter_hydration_targets(
    tables: Mapping[str, Sequence[dict[str, Any]]],
    blobs_by_event: Mapping[str, Mapping[tuple[str, str], Mapping[str, Any]]],
    *,
    full: bool = False,
) -> Iterator[tuple[str, dict[str, Any], str]]:
    for idx, mc in enumerate(tables.get("model_calls", ())):
        eid = mc.get("event_id")
        if not isinstance(eid, str) or not eid:
            continue
        ev_b = blobs_by_event.get(eid, {})
        if full:
            if ("model_calls", "request") in ev_b or ("model_calls", "response") in ev_b:
                yield eid, mc, "model_calls_full"
        elif idx == 0 and _body_needs_hydration(
            ev_b.get(("model_calls", "request")), mc.get("request_body_preview"), full=False
        ):
            yield eid, mc, "model_calls_initial_request"
    for tc in tables.get("tool_calls", ()):
        eid = tc.get("event_id")
        if isinstance(eid, str) and _body_needs_hydration(
            blobs_by_event.get(eid, {}).get(("tool_calls", "request")),
            tc.get("arguments"),
            full=full,
        ):
            yield eid, tc, "tool_calls"
    for tr in tables.get("tool_responses", ()):
        eid = tr.get("event_id")
        if isinstance(eid, str) and _body_needs_hydration(
            blobs_by_event.get(eid, {}).get(("tool_responses", "response")),
            tr.get("content_preview"),
            full=full,
        ):
            yield eid, tr, "tool_responses"
    if full or not tables.get("model_calls"):
        for ex in tables.get("exec_events", ()):
            eid = ex.get("event_id")
            if not isinstance(eid, str) or not eid:
                continue
            ev_b = blobs_by_event.get(eid, {})
            if _body_needs_hydration(
                ev_b.get(("exec_events", "stdout")), ex.get("stdout_preview"), full=full
            ) or _body_needs_hydration(
                ev_b.get(("exec_events", "stderr")), ex.get("stderr_preview"), full=full
            ):
                yield eid, ex, "exec_events"
    if full:
        for sr in tables.get("security_rule_events", ()):
            eid = sr.get("event_id")
            if (
                isinstance(eid, str)
                and ("security_rule_events", "payload") in blobs_by_event.get(eid, {})
            ):
                yield eid, sr, "security_rule_events"


def candidate_body_event_ids(
    tables: Mapping[str, Sequence[dict[str, Any]]], *, full: bool = False
) -> list[str]:
    """Return deduplicated `event_id`s whose archived bodies should be hydrated."""
    blobs_by_event = _index_body_blobs(tables.get("event_body_blobs", ()))
    out: list[str] = []
    seen: set[str] = set()
    for eid, _, _ in _iter_hydration_targets(tables, blobs_by_event, full=full):
        if eid not in seen:
            seen.add(eid)
            out.append(eid)
    return out


def hydrate_tables_from_bodies(
    tables: Mapping[str, list[dict[str, Any]]],
    reader: BodyReader,
    *,
    full: bool = False,
) -> None:
    """Hydrate truncated or full bodies in `tables` in place using `reader`."""
    blobs_by_event = _index_body_blobs(tables.get("event_body_blobs", ()))
    fetched: dict[str, dict[tuple[str, str], Any]] = {}
    for eid, row, kind in _iter_hydration_targets(tables, blobs_by_event, full=full):
        if eid not in fetched:
            try:
                bodies = reader.get_event_bodies(eid)
            except (ValueError, CapsemError):
                bodies = ()
            fetched[eid] = {
                (str(_attr(b, "source_table") or ""), str(_attr(b, "direction") or "")): b
                for b in bodies
            }
        bmap = fetched[eid]
        if kind in ("model_calls_full", "model_calls_initial_request"):
            if (req_b := bmap.get(("model_calls", "request"))) is not None:
                row["request_body"] = _decode_body_text(req_b)
            if kind == "model_calls_full" and (
                resp_b := bmap.get(("model_calls", "response"))
            ) is not None:
                row["response_body"] = _decode_body_text(resp_b)
        elif kind == "tool_calls":
            if (b := bmap.get(("tool_calls", "request"))) is not None:
                row["arguments"] = _decode_body_text(b)
        elif kind == "tool_responses":
            if (b := bmap.get(("tool_responses", "response"))) is not None:
                row["content_full"] = _decode_body_text(b)
        elif kind == "exec_events":
            if (out_b := bmap.get(("exec_events", "stdout"))) is not None:
                row["stdout"] = _decode_body_text(out_b)
            if (err_b := bmap.get(("exec_events", "stderr"))) is not None:
                row["stderr"] = _decode_body_text(err_b)
        elif kind == "security_rule_events" and (
            b := bmap.get(("security_rule_events", "payload"))
        ) is not None:
            row["payload"] = decode_security_payload(b)
