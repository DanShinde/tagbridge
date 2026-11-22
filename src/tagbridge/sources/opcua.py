"""OPC UA client driver using subscriptions or polling."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from ..config import OpcUaDeviceConfig, TagConfig
from ..model import Reading, now_ms
from .base import Source, SourceError

log = logging.getLogger(__name__)

_VARIANT_TO_DATATYPE = {
    "Boolean": "bool",
    "SByte": "int16",
    "Int16": "int16",
    "Byte": "uint16",
    "UInt16": "uint16",
    "Int32": "int32",
    "UInt32": "uint32",
    "Int64": "int64",
    "UInt64": "uint64",
    "Float": "float32",
    "Double": "float64",
}


def to_plain(value: Any) -> Any:
    """Convert OPC UA values to JSON and Sparkplug friendly Python scalars."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.isoformat()
    text = getattr(value, "Text", None)
    if isinstance(text, str):
        return text
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (list, tuple)):
        return str([to_plain(v) for v in value])
    return str(value)


def _source_ms(data_value: Any) -> int:
    stamp = getattr(data_value, "SourceTimestamp", None) or getattr(
        data_value, "ServerTimestamp", None
    )
    if isinstance(stamp, dt.datetime):
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=dt.timezone.utc)
        return int(stamp.timestamp() * 1000)
    return now_ms()


def _reading(data_value: Any) -> Reading:
    status = getattr(data_value, "StatusCode_", None) or getattr(data_value, "StatusCode", None)
    good = bool(status.is_good()) if status is not None else True
    value = data_value.Value.Value if data_value.Value is not None else None
    return Reading(to_plain(value) if good else None, _source_ms(data_value), good)


class _Handler:
    def __init__(self, source: OpcUaSource) -> None:
        self._source = source

    def datachange_notification(self, node: Any, val: Any, data: Any) -> None:
        name = self._source._names.get(node.nodeid.to_string())
        if name is not None:
            self._source._cache[name] = _reading(data.monitored_item.Value)

    def status_change_notification(self, status: Any) -> None:
        self._source._lost = True


class OpcUaSource(Source):
    def __init__(self, config: OpcUaDeviceConfig) -> None:
        self.config = config
        self._client: Any = None
        self._nodes: dict[str, Any] = {}
        self._names: dict[str, str] = {}
        self._variant_types: dict[str, Any] = {}
        self._datatypes: dict[str, str] = {}
        self._cache: dict[str, Reading] = {}
        self._subscription: Any = None
        self._lost = False

    def datatype_of(self, tag_name: str) -> str:
        return self._datatypes.get(tag_name, "auto")

    async def connect(self) -> None:
        try:
            from asyncua import Client
        except ImportError as exc:  # pragma: no cover
            raise SourceError("OPC UA support needs: pip install 'tagbridge[opcua]'") from exc
        cfg = self.config
        client = Client(cfg.endpoint, timeout=cfg.timeout_s)
        if cfg.username:
            client.set_user(cfg.username)
        if cfg.password:
            client.set_password(cfg.password)
        try:
            if cfg.security:
                await client.set_security_string(cfg.security)
            await client.connect()
        except Exception as exc:
            raise SourceError(f"cannot connect to {cfg.endpoint}: {exc}") from exc
        self._client = client
        self._lost = False
        self._cache = {}
        try:
            await self._resolve_nodes()
            if cfg.mode == "subscribe" and self._nodes:
                self._subscription = await client.create_subscription(cfg.scan_ms, _Handler(self))
                await self._subscription.subscribe_data_change(list(self._nodes.values()))
            await self._poll()
        except SourceError:
            await self.close()
            raise
        except Exception as exc:
            await self.close()
            raise SourceError(f"{cfg.endpoint}: {exc}") from exc

    async def _resolve_nodes(self) -> None:
        self._nodes.clear()
        self._names.clear()
        for tag in self.config.tags:
            try:
                node = self._client.get_node(tag.address)
            except Exception as exc:
                raise SourceError(f"tag {tag.name!r}: invalid node id {tag.address!r}") from exc
            self._nodes[tag.name] = node
            self._names[node.nodeid.to_string()] = tag.name
            try:
                variant_type = await node.read_data_type_as_variant_type()
            except Exception as exc:
                log.warning("%s: cannot read data type of %s: %s", self.config.name, tag.name, exc)
                variant_type = None
            self._variant_types[tag.name] = variant_type
            if tag.datatype != "auto":
                self._datatypes[tag.name] = tag.datatype
            elif variant_type is not None:
                self._datatypes[tag.name] = _VARIANT_TO_DATATYPE.get(variant_type.name, "string")

    async def _poll(self) -> None:
        if not self._nodes:
            return
        names = list(self._nodes)
        data_values = await self._client.read_attributes([self._nodes[n] for n in names])
        for name, data_value in zip(names, data_values, strict=True):
            self._cache[name] = _reading(data_value)

    async def close(self) -> None:
        client, self._client = self._client, None
        self._subscription = None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                log.debug("error while disconnecting from %s", self.config.endpoint, exc_info=True)

    async def read(self) -> dict[str, Reading]:
        if self._client is None:
            raise SourceError("not connected")
        if self._lost:
            raise SourceError("subscription lost")
        try:
            await self._client.check_connection()
            if self.config.mode == "poll":
                await self._poll()
        except Exception as exc:
            raise SourceError(f"{self.config.endpoint}: {exc}") from exc
        return dict(self._cache)

    async def write(self, tag: TagConfig, value: Any) -> None:
        if self._client is None:
            raise SourceError("not connected")
        from asyncua import ua

        node = self._nodes[tag.name]
        variant_type = self._variant_types.get(tag.name)
        try:
            variant = ua.Variant(value, variant_type) if variant_type else ua.Variant(value)
            await node.write_value(ua.DataValue(variant))
        except Exception as exc:
            raise SourceError(f"write {tag.name!r} failed: {exc}") from exc
