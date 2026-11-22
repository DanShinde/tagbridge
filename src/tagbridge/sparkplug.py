"""Minimal Sparkplug B payload codec.

Implements the subset of the Sparkplug B protobuf schema (``sparkplug_b.proto``)
needed by an edge node: scalar metrics with names, aliases, timestamps and null
values. Encoding is done by hand so the package does not depend on generated
protobuf code or a specific ``protobuf`` runtime version.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

NAMESPACE = "spBv1.0"


class DataType(IntEnum):
    UNKNOWN = 0
    INT8 = 1
    INT16 = 2
    INT32 = 3
    INT64 = 4
    UINT8 = 5
    UINT16 = 6
    UINT32 = 7
    UINT64 = 8
    FLOAT = 9
    DOUBLE = 10
    BOOLEAN = 11
    STRING = 12
    DATETIME = 13
    TEXT = 14


_INT_BITS = {
    DataType.INT8: 8,
    DataType.INT16: 16,
    DataType.INT32: 32,
    DataType.INT64: 64,
    DataType.UINT8: 8,
    DataType.UINT16: 16,
    DataType.UINT32: 32,
    DataType.UINT64: 64,
    DataType.DATETIME: 64,
}
_SIGNED = {DataType.INT8, DataType.INT16, DataType.INT32, DataType.INT64}
_UINT32_FIELD = {
    DataType.INT8,
    DataType.INT16,
    DataType.INT32,
    DataType.UINT8,
    DataType.UINT16,
    DataType.UINT32,
}
_UINT64_FIELD = {DataType.INT64, DataType.UINT64, DataType.DATETIME}

_WT_VARINT = 0
_WT_I64 = 1
_WT_LEN = 2
_WT_I32 = 5


@dataclass
class Metric:
    name: str | None = None
    value: Any = None
    datatype: DataType = DataType.UNKNOWN
    alias: int | None = None
    timestamp: int | None = None
    is_null: bool = False


@dataclass
class Payload:
    timestamp: int | None = None
    seq: int | None = None
    metrics: list[Metric] = field(default_factory=list)


def _varint(value: int) -> bytes:
    if value < 0:
        value &= (1 << 64) - 1
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _key(field_no: int, wire_type: int) -> bytes:
    return _varint((field_no << 3) | wire_type)


def _len_field(field_no: int, data: bytes) -> bytes:
    return _key(field_no, _WT_LEN) + _varint(len(data)) + data


def _encode_metric(metric: Metric) -> bytes:
    out = bytearray()
    if metric.name is not None:
        out += _len_field(1, metric.name.encode("utf-8"))
    if metric.alias is not None:
        out += _key(2, _WT_VARINT) + _varint(metric.alias)
    if metric.timestamp is not None:
        out += _key(3, _WT_VARINT) + _varint(metric.timestamp)
    out += _key(4, _WT_VARINT) + _varint(int(metric.datatype))
    if metric.is_null or metric.value is None:
        out += _key(7, _WT_VARINT) + _varint(1)
        return bytes(out)
    dt = metric.datatype
    value = metric.value
    if dt in _UINT32_FIELD:
        out += _key(10, _WT_VARINT) + _varint(int(value) & 0xFFFFFFFF)
    elif dt in _UINT64_FIELD:
        out += _key(11, _WT_VARINT) + _varint(int(value) & 0xFFFFFFFFFFFFFFFF)
    elif dt == DataType.FLOAT:
        out += _key(12, _WT_I32) + struct.pack("<f", float(value))
    elif dt == DataType.DOUBLE:
        out += _key(13, _WT_I64) + struct.pack("<d", float(value))
    elif dt == DataType.BOOLEAN:
        out += _key(14, _WT_VARINT) + _varint(1 if value else 0)
    elif dt in (DataType.STRING, DataType.TEXT):
        out += _len_field(15, str(value).encode("utf-8"))
    else:
        raise ValueError(f"unsupported Sparkplug datatype {dt!r}")
    return bytes(out)


def encode_payload(payload: Payload) -> bytes:
    out = bytearray()
    if payload.timestamp is not None:
        out += _key(1, _WT_VARINT) + _varint(payload.timestamp)
    for metric in payload.metrics:
        out += _len_field(2, _encode_metric(metric))
    if payload.seq is not None:
        out += _key(3, _WT_VARINT) + _varint(payload.seq)
    return bytes(out)


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("truncated varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 70:
            raise ValueError("varint too long")


def _fields(data: bytes) -> list[tuple[int, int, Any]]:
    pos = 0
    fields: list[tuple[int, int, Any]] = []
    while pos < len(data):
        key, pos = _read_varint(data, pos)
        field_no, wire_type = key >> 3, key & 7
        value: Any
        if wire_type == _WT_VARINT:
            value, pos = _read_varint(data, pos)
        elif wire_type == _WT_I64:
            value = data[pos : pos + 8]
            pos += 8
        elif wire_type == _WT_I32:
            value = data[pos : pos + 4]
            pos += 4
        elif wire_type == _WT_LEN:
            length, pos = _read_varint(data, pos)
            value = data[pos : pos + length]
            pos += length
        else:
            raise ValueError(f"unsupported wire type {wire_type}")
        if pos > len(data):
            raise ValueError("truncated payload")
        fields.append((field_no, wire_type, value))
    return fields


def _from_unsigned(raw: int, dt: DataType) -> int:
    bits = _INT_BITS[dt]
    raw &= (1 << bits) - 1
    if dt in _SIGNED and raw >= 1 << (bits - 1):
        raw -= 1 << bits
    return raw


def _decode_metric(data: bytes) -> Metric:
    metric = Metric()
    raw: dict[int, Any] = {}
    for field_no, _wt, value in _fields(data):
        raw[field_no] = value
    if 1 in raw:
        metric.name = bytes(raw[1]).decode("utf-8")
    if 2 in raw:
        metric.alias = raw[2]
    if 3 in raw:
        metric.timestamp = raw[3]
    if 4 in raw:
        try:
            metric.datatype = DataType(raw[4])
        except ValueError:
            metric.datatype = DataType.UNKNOWN
    if raw.get(7):
        metric.is_null = True
        return metric
    dt = metric.datatype
    if 10 in raw:
        metric.value = _from_unsigned(raw[10], dt) if dt in _INT_BITS else raw[10]
    elif 11 in raw:
        metric.value = _from_unsigned(raw[11], dt) if dt in _INT_BITS else raw[11]
    elif 12 in raw:
        metric.value = struct.unpack("<f", raw[12])[0]
    elif 13 in raw:
        metric.value = struct.unpack("<d", raw[13])[0]
    elif 14 in raw:
        metric.value = bool(raw[14])
    elif 15 in raw:
        metric.value = bytes(raw[15]).decode("utf-8")
    return metric


def decode_payload(data: bytes) -> Payload:
    payload = Payload()
    for field_no, _wt, value in _fields(data):
        if field_no == 1:
            payload.timestamp = value
        elif field_no == 2:
            payload.metrics.append(_decode_metric(value))
        elif field_no == 3:
            payload.seq = value
    return payload


_TAG_TYPES = {
    "bool": DataType.BOOLEAN,
    "int16": DataType.INT16,
    "uint16": DataType.UINT16,
    "int32": DataType.INT32,
    "uint32": DataType.UINT32,
    "int64": DataType.INT64,
    "uint64": DataType.UINT64,
    "float32": DataType.FLOAT,
    "float64": DataType.DOUBLE,
    "string": DataType.STRING,
}


def datatype_for(tag_datatype: str, value: Any = None) -> DataType:
    """Map a tagbridge datatype (or a Python value for ``auto``) to a Sparkplug type."""
    if tag_datatype in _TAG_TYPES:
        return _TAG_TYPES[tag_datatype]
    if isinstance(value, bool):
        return DataType.BOOLEAN
    if isinstance(value, int):
        return DataType.INT64
    if isinstance(value, float):
        return DataType.DOUBLE
    return DataType.STRING


def topic(group_id: str, message_type: str, edge_node_id: str, device_id: str | None = None) -> str:
    parts = [NAMESPACE, group_id, message_type, edge_node_id]
    if device_id is not None:
        parts.append(device_id)
    return "/".join(parts)
