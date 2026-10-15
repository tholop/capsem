"""HTTP body hydration, `BodyReader` protocol, and MessagePack security payload decoding for `capsem.trace`."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import struct
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal, Protocol, runtime_checkable

from . import _operations as api
from . import models
from ._transport import HttpError, Transport
from .errors import ExportUnavailableError

BodiesMode = Literal["none", "previews", "http", "full"]
VALID_BODIES_MODES: tuple[str, ...] = ("none", "previews", "http", "full")
SECURITY_TABLES: tuple[str, ...] = (
    "security_rule_events",
    "security_decision_events",
    "security_ask_events",
)


@runtime_checkable
class BodyReader(Protocol):
    """Synchronous interface used by `capsem.trace` to retrieve archived bodies by `event_id`."""

    def get_event_bodies(self, event_id: str) -> Sequence[Any]: ...


class HttpBodyReader:
    """Hydrate archived event bodies over HTTP via `GET /vms/{id}/bodies/{event_id}`."""

    def __init__(
        self, transport: Transport, vm_id: str, *, max_bytes: int | None = None
    ) -> None:
        self._transport = transport
        self._vm_id = vm_id
        self._max_bytes = max_bytes
        self._cache: dict[str, Sequence[models.ArchivedEventBody]] = {}

    async def fetch_event_bodies(self, event_id: str) -> Sequence[models.ArchivedEventBody]:
        if event_id in self._cache:
            return self._cache[event_id]
        try:
            resp = await api.get_vm_event_bodies(
                self._transport, id=self._vm_id, event_id=event_id, max_bytes=self._max_bytes
            )
        except HttpError as exc:
            raise ExportUnavailableError(
                f"HTTP body hydration unavailable for VM {self._vm_id!r} event {event_id!r}: {exc}",
                vm_id=self._vm_id,
            ) from exc
        self._cache[event_id] = resp.bodies
        return resp.bodies

    async def prefetch(self, event_ids: Iterable[str]) -> None:
        for eid in dict.fromkeys(e for e in event_ids if e):
            if eid not in self._cache:
                await self.fetch_event_bodies(eid)

    def get_event_bodies(self, event_id: str) -> Sequence[models.ArchivedEventBody]:
        if event_id in self._cache:
            return self._cache[event_id]
        return asyncio.run(self.fetch_event_bodies(event_id))


_MP_FIXED = {0xC0: None, 0xC2: False, 0xC3: True}
_MP_NUMS = {
    0xCA: ">f", 0xCB: ">d", 0xCC: ">B", 0xCD: ">H", 0xCE: ">I", 0xCF: ">Q",
    0xD0: ">b", 0xD1: ">h", 0xD2: ">i", 0xD3: ">q",
}
_MP_LENS = {
    0xD9: ">B", 0xDA: ">H", 0xDB: ">I", 0xC4: ">B", 0xC5: ">H", 0xC6: ">I",
    0xDC: ">H", 0xDD: ">I", 0xDE: ">H", 0xDF: ">I",
}


def _mp_seq(mv: memoryview, at: int, n: int, *, is_map: bool) -> tuple[Any, int]:
    if not is_map:
        arr = []
        for _ in range(n):
            v, at = _mp_val(mv, at)
            arr.append(v)
        return arr, at
    out: dict[Any, Any] = {}
    for _ in range(n):
        k, at = _mp_val(mv, at)
        out[k], at = _mp_val(mv, at)
    return out, at


def _mp_val(mv: memoryview, at: int) -> tuple[Any, int]:
    if at >= len(mv):
        raise ValueError("truncated msgpack")
    m = mv[at]
    at += 1
    if m <= 0x7F or m >= 0xE0:
        return (m if m <= 0x7F else m - 0x100), at
    if 0x80 <= m <= 0xBF:
        n = m & (0x1F if m >= 0xA0 else 0x0F)
        if m >= 0xA0:
            return bytes(mv[at : at + n]).decode("utf-8"), at + n
        return _mp_seq(mv, at, n, is_map=(m < 0x90))
    if m in _MP_FIXED:
        return _MP_FIXED[m], at
    if fmt := _MP_NUMS.get(m):
        sz = struct.calcsize(fmt)
        return struct.unpack(fmt, bytes(mv[at : at + sz]))[0], at + sz
    if lfmt := _MP_LENS.get(m):
        sz = struct.calcsize(lfmt)
        n = struct.unpack(lfmt, bytes(mv[at : at + sz]))[0]
        at += sz
        if m in (0xD9, 0xDA, 0xDB):
            return bytes(mv[at : at + n]).decode("utf-8"), at + n
        if m in (0xC4, 0xC5, 0xC6):
            return bytes(mv[at : at + n]).hex(), at + n
        return _mp_seq(mv, at, n, is_map=(m >= 0xDE))
    raise ValueError(f"unsupported msgpack marker 0x{m:02x}")


def decode_security_payload(raw: bytes | Mapping[str, Any]) -> Any:
    """Decode a MessagePack or JSON security event payload into a JSON-serializable structure."""
    data = _decode_body_bytes(raw) if isinstance(raw, Mapping) else raw
    with contextlib.suppress(ValueError, UnicodeDecodeError, struct.error, TypeError):
        val, end = _mp_val(memoryview(data), 0)
        if end == len(data):
            return val
    txt = data.decode("utf-8", errors="replace")
    try:
        return json.loads(txt)
    except ValueError:
        return txt


def _attr(obj: Any, key: str, default: Any = None) -> Any:
    return obj.get(key, default) if isinstance(obj, Mapping) else getattr(obj, key, default)


def _decode_body(body: Any) -> str:
    raw = _attr(body, "content")
    content = str(raw if raw is not None else (_attr(body, "data", "") or ""))
    enc = _attr(body, "encoding", "utf8")
    if getattr(enc, "value", str(enc)) == "base64":
        return base64.b64decode(content).decode("utf-8", errors="replace")
    return content


def _decode_body_bytes(body: Any) -> bytes:
    raw = _attr(body, "content")
    content = str(raw if raw is not None else (_attr(body, "data", "") or ""))
    enc = _attr(body, "encoding", "utf8")
    return base64.b64decode(content) if getattr(enc, "value", str(enc)) == "base64" else content.encode("utf-8")


def _needs_hydration(blob: Mapping[str, Any] | None, preview: str | None) -> bool:
    if blob is None:
        return False
    return bool(blob.get("truncated")) or int(blob.get("stored_bytes") or 0) > len((preview or "").encode("utf-8"))


def index_body_blobs(blobs: Sequence[Mapping[str, Any]]) -> dict[tuple[str, str, str], Mapping[str, Any]]:
    return {
        (str(b.get("event_id") or ""), str(b.get("source_table") or ""), str(b.get("direction") or "")): b
        for b in blobs
        if b.get("event_id")
    }


def candidate_body_event_ids(
    tables: Mapping[str, Sequence[Mapping[str, Any]]],
    blobs: Sequence[Mapping[str, Any]],
    *,
    bodies: BodiesMode,
    include_security_events: bool = False,
) -> list[str]:
    """Return deduplicated `event_id`s requiring HTTP body fetches for `bodies` mode."""
    if bodies not in ("http", "full"):
        return []
    bmap, eids = index_body_blobs(blobs), list[str]()
    for mc in tables.get("model_calls", ()):
        eid = str(mc.get("event_id") or "")
        req_b, resp_b = bmap.get((eid, "model_calls", "request")), bmap.get((eid, "model_calls", "response"))
        if _needs_hydration(req_b, mc.get("request_body_preview")) or _needs_hydration(req_b, mc.get("system_prompt_preview")) or _needs_hydration(resp_b, mc.get("text_content")):
            eids.append(eid)
    for tr in tables.get("tool_responses", ()):
        eid = str(tr.get("event_id") or "")
        if _needs_hydration(bmap.get((eid, "tool_responses", "response")), tr.get("content_preview")):
            eids.append(eid)
    for tc in tables.get("tool_calls", ()):
        eid = str(tc.get("event_id") or "")
        if _needs_hydration(bmap.get((eid, "tool_calls", "request")), tc.get("arguments")):
            eids.append(eid)
    if bodies == "full" or not tables.get("model_calls"):
        for ex in tables.get("exec_events", ()):
            eid = str(ex.get("event_id") or "")
            if (eid, "exec_events", "stdout") in bmap or (eid, "exec_events", "stderr") in bmap:
                eids.append(eid)
    if bodies == "full":
        for ne in tables.get("net_events", ()):
            eid = str(ne.get("event_id") or "")
            if (eid, "net_events", "request") in bmap or (eid, "net_events", "response") in bmap:
                eids.append(eid)
    if include_security_events:
        for stbl in SECURITY_TABLES:
            for sr in tables.get(stbl, ()):
                if ((eid := str(sr.get("event_id") or "")), stbl, "payload") in bmap:
                    eids.append(eid)
    return list(dict.fromkeys(e for e in eids if e))


def _lookup_body(body_reader: BodyReader | None, event_id: str, source_table: str, direction: str) -> Any | None:
    if body_reader is None or not event_id:
        return None
    items = body_reader.get_event_bodies(event_id)
    return next((i for i in (items or ()) if (not _attr(i, "source_table") or _attr(i, "source_table") == source_table) and _attr(i, "direction") == direction), None)


def hydrate_tables_from_bodies(
    tables: dict[str, list[dict[str, Any]]],
    blobs: Sequence[Mapping[str, Any]],
    *,
    bodies: BodiesMode,
    body_reader: BodyReader | None,
    include_security_events: bool = False,
) -> None:
    """Hydrate `tables` rows in-place from `body_reader` (or annotate truncation for `previews`)."""
    if bodies == "none":
        return
    if bodies in ("http", "full") and body_reader is None:
        raise ExportUnavailableError(f"body_reader is required when bodies={bodies!r}")
    bmap = index_body_blobs(blobs)
    for mc in tables.get("model_calls", []):
        eid = str(mc.get("event_id") or "")
        req_b, resp_b = bmap.get((eid, "model_calls", "request")), bmap.get((eid, "model_calls", "response"))
        req_trunc = _needs_hydration(req_b, mc.get("request_body_preview")) or _needs_hydration(req_b, mc.get("system_prompt_preview"))
        resp_trunc = _needs_hydration(resp_b, mc.get("text_content"))
        if bodies == "previews" and (req_trunc or resp_trunc):
            mc["truncated"] = True
        elif bodies in ("http", "full"):
            if req_trunc and (b := _lookup_body(body_reader, eid, "model_calls", "request")) is not None:
                mc["request_body"] = _decode_body(b)
                mc["truncated"] = bool(mc.get("truncated") or _attr(b, "truncated") or _attr(b, "truncated_for_transport"))
            if resp_trunc and (b := _lookup_body(body_reader, eid, "model_calls", "response")) is not None:
                mc["response_body"] = _decode_body(b)
                mc["truncated"] = bool(mc.get("truncated") or _attr(b, "truncated") or _attr(b, "truncated_for_transport"))
    for tr in tables.get("tool_responses", []):
        eid = str(tr.get("event_id") or "")
        if _needs_hydration(bmap.get((eid, "tool_responses", "response")), tr.get("content_preview")):
            if bodies == "previews":
                tr["truncated"] = True
            elif (b := _lookup_body(body_reader, eid, "tool_responses", "response")) is not None:
                tr["content_full"], tr["body_hash"] = _decode_body(b), _attr(b, "body_hash")
                tr["truncated"] = bool(_attr(b, "truncated") or _attr(b, "truncated_for_transport"))
    for tc in tables.get("tool_calls", []):
        eid = str(tc.get("event_id") or "")
        if _needs_hydration(bmap.get((eid, "tool_calls", "request")), tc.get("arguments")):
            if bodies == "previews":
                tc["truncated"] = True
            elif (b := _lookup_body(body_reader, eid, "tool_calls", "request")) is not None:
                tc["arguments"] = _decode_body(b)
    if bodies in ("http", "full") and (bodies == "full" or not tables.get("model_calls")):
        for ex in tables.get("exec_events", []):
            eid = str(ex.get("event_id") or "")
            for d in ("stdout", "stderr"):
                if (eid, "exec_events", d) in bmap and (b := _lookup_body(body_reader, eid, "exec_events", d)) is not None:
                    ex[d] = _decode_body(b)
    if bodies == "full":
        for ne in tables.get("net_events", []):
            eid = str(ne.get("event_id") or "")
            for d, k in (("request", "request_body"), ("response", "response_body")):
                if (eid, "net_events", d) in bmap and (b := _lookup_body(body_reader, eid, "net_events", d)) is not None:
                    ne[k] = _decode_body(b)
    if include_security_events and bodies in ("http", "full"):
        for stbl in SECURITY_TABLES:
            for sr in tables.get(stbl, []):
                eid = str(sr.get("event_id") or "")
                if (eid, stbl, "payload") in bmap and (b := _lookup_body(body_reader, eid, stbl, "payload")) is not None:
                    sr["payload"] = decode_security_payload(_decode_body_bytes(b))
