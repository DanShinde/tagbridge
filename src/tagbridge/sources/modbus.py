"""Modbus TCP and RTU driver with block-optimised reads."""

from __future__ import annotations

import logging
import re
import struct
from dataclasses import dataclass, field
from typing import Any, Literal

from ..config import ModbusDeviceConfig, TagConfig
from ..model import Reading, now_ms
from .base import Source, SourceError

log = logging.getLogger(__name__)

Area = Literal["co", "di", "hr", "ir"]

_AREA_ALIASES: dict[str, Area] = {
    "co": "co",
    "coil": "co",
    "coils": "co",
    "di": "di",
    "discrete": "di",
    "discrete_input": "di",
    "hr": "hr",
    "holding": "hr",
    "holding_register": "hr",
    "ir": "ir",
    "input": "ir",
    "input_register": "ir",
}
_PREFIX_DIGIT: dict[str, Area] = {"0": "co", "1": "di", "3": "ir", "4": "hr"}
_ADDRESS_RE = re.compile(r"^(?P<area>[a-z_]+)\s*[:\s]\s*(?P<offset>\d+)(?:\.(?P<bit>\d+))?$")
_NUMERIC_RE = re.compile(r"^(?P<number>\d{1,6})(?:\.(?P<bit>\d+))?$")
_MAX_BITS_PER_READ = 2000

_WORDS = {
    "bool": 1,
    "int16": 1,
    "uint16": 1,
    "int32": 2,
    "uint32": 2,
    "float32": 2,
    "int64": 4,
    "uint64": 4,
    "float64": 4,
}
_STRUCT = {
    "int16": ">h",
    "uint16": ">H",
    "int32": ">i",
    "uint32": ">I",
    "float32": ">f",
    "int64": ">q",
    "uint64": ">Q",
    "float64": ">d",
}


@dataclass(frozen=True)
class ModbusAddress:
    area: Area
    offset: int
    bit: int | None = None

    @property
    def is_bit_area(self) -> bool:
        return self.area in ("co", "di")


def parse_address(text: str) -> ModbusAddress:
    """Parse ``hr:100``, ``holding 100``, ``hr:100.3``, ``40101`` or ``400101`` addresses.

    Prefixed offsets are zero based. Classic Modicon numbers (``40001``) are one based.
    """
    value = text.strip().lower()
    match = _ADDRESS_RE.match(value)
    if match:
        area = _AREA_ALIASES.get(match["area"])
        if area is None:
            raise ValueError(f"unknown Modbus area in {text!r}; use co, di, hr or ir")
        offset = int(match["offset"])
        bit = int(match["bit"]) if match["bit"] is not None else None
    else:
        match = _NUMERIC_RE.match(value)
        if not match:
            raise ValueError(f"invalid Modbus address {text!r}")
        number = match["number"]
        bit = int(match["bit"]) if match["bit"] is not None else None
        if len(number) <= 4:
            area, offset = "co", int(number) - 1
        else:
            area_or_none = _PREFIX_DIGIT.get(number[0])
            if area_or_none is None:
                raise ValueError(f"invalid Modbus address {text!r}: must start with 0, 1, 3 or 4")
            area = area_or_none
            offset = int(number[1:]) - 1
    if offset < 0 or offset > 65535:
        raise ValueError(f"Modbus address {text!r} is out of range")
    if bit is not None:
        if area in ("co", "di"):
            raise ValueError(f"{text!r}: bit addressing only applies to registers")
        if not 0 <= bit <= 15:
            raise ValueError(f"{text!r}: bit must be between 0 and 15")
    return ModbusAddress(area, offset, bit)


def resolve_datatype(tag: TagConfig, address: ModbusAddress) -> str:
    if address.is_bit_area or address.bit is not None:
        if tag.datatype not in ("auto", "bool"):
            raise ValueError(f"tag {tag.name!r}: coils, discrete inputs and bits are bool")
        return "bool"
    if tag.datatype == "auto":
        return "uint16"
    if tag.datatype == "string" and not tag.length:
        raise ValueError(f"tag {tag.name!r}: string tags need a length")
    return tag.datatype


def word_count(datatype: str, length: int | None = None) -> int:
    if datatype == "string":
        return ((length or 0) + 1) // 2
    return _WORDS[datatype]


def registers_to_bytes(registers: list[int], byte_order: str, word_order: str) -> bytes:
    words = list(registers)
    if word_order == "little":
        words.reverse()
    out = bytearray()
    for word in words:
        pair = struct.pack(">H", word & 0xFFFF)
        out += pair[::-1] if byte_order == "little" else pair
    return bytes(out)


def bytes_to_registers(data: bytes, byte_order: str, word_order: str) -> list[int]:
    words = []
    for i in range(0, len(data), 2):
        pair = data[i : i + 2]
        if byte_order == "little":
            pair = pair[::-1]
        words.append(struct.unpack(">H", pair)[0])
    if word_order == "little":
        words.reverse()
    return words


def decode_registers(
    registers: list[int],
    datatype: str,
    byte_order: str = "big",
    word_order: str = "big",
    bit: int | None = None,
) -> Any:
    if bit is not None:
        return bool((registers[0] >> bit) & 1)
    if datatype == "string":
        raw = b"".join(
            struct.pack(">H", r)[::-1] if byte_order == "little" else struct.pack(">H", r)
            for r in registers
        )
        return raw.split(b"\x00", 1)[0].decode("latin-1").rstrip()
    if datatype == "bool":
        return bool(registers[0])
    data = registers_to_bytes(registers, byte_order, word_order)
    return struct.unpack(_STRUCT[datatype], data)[0]


def encode_value(
    value: Any,
    datatype: str,
    byte_order: str = "big",
    word_order: str = "big",
    length: int | None = None,
) -> list[int]:
    if datatype == "string":
        size = length or 0
        raw = str(value).encode("latin-1")[:size].ljust(word_count("string", size) * 2, b"\x00")
        words = []
        for i in range(0, len(raw), 2):
            pair = raw[i : i + 2]
            words.append(struct.unpack(">H", pair[::-1] if byte_order == "little" else pair)[0])
        return words
    if datatype == "bool":
        return [1 if value else 0]
    data = struct.pack(_STRUCT[datatype], value)
    return bytes_to_registers(data, byte_order, word_order)


@dataclass
class _TagPlan:
    tag: TagConfig
    address: ModbusAddress
    datatype: str
    count: int


@dataclass
class ReadBlock:
    area: Area
    start: int
    count: int
    tags: list[_TagPlan] = field(default_factory=list)


def plan_blocks(
    tags: list[_TagPlan], max_registers: int = 100, max_gap: int = 10
) -> list[ReadBlock]:
    """Group tags into as few read requests as possible."""
    blocks: list[ReadBlock] = []
    by_area: dict[Area, list[_TagPlan]] = {}
    for plan in tags:
        by_area.setdefault(plan.address.area, []).append(plan)
    areas: tuple[Area, ...] = ("co", "di", "hr", "ir")
    for area in areas:
        plans = sorted(by_area.get(area, []), key=lambda p: (p.address.offset, p.count))
        limit = _MAX_BITS_PER_READ if area in ("co", "di") else max_registers
        current: ReadBlock | None = None
        for plan in plans:
            start, end = plan.address.offset, plan.address.offset + plan.count
            if current is not None:
                current_end = current.start + current.count
                if start - current_end <= max_gap and end - current.start <= limit:
                    current.count = max(current_end, end) - current.start
                    current.tags.append(plan)
                    continue
            current = ReadBlock(area, start, plan.count, [plan])
            blocks.append(current)
    return blocks


class ModbusSource(Source):
    def __init__(self, config: ModbusDeviceConfig) -> None:
        self.config = config
        self._client: Any = None
        self._plans: dict[str, _TagPlan] = {}
        for tag in config.tags:
            try:
                address = parse_address(tag.address)
                datatype = resolve_datatype(tag, address)
            except ValueError as exc:
                raise ValueError(f"device {config.name!r}, tag {tag.name!r}: {exc}") from exc
            if tag.writable and address.area in ("di", "ir"):
                raise ValueError(
                    f"device {config.name!r}, tag {tag.name!r}: inputs cannot be writable"
                )
            count = 1 if address.bit is not None else word_count(datatype, tag.length)
            self._plans[tag.name] = _TagPlan(tag, address, datatype, count)
        self.blocks = plan_blocks(
            list(self._plans.values()), config.max_registers_per_read, config.max_gap
        )

    def datatype_of(self, tag_name: str) -> str:
        return self._plans[tag_name].datatype

    async def connect(self) -> None:
        try:
            from pymodbus.client import AsyncModbusSerialClient, AsyncModbusTcpClient
        except ImportError as exc:  # pragma: no cover
            raise SourceError("Modbus support needs: pip install 'tagbridge[modbus]'") from exc
        cfg = self.config
        if cfg.host:
            self._client = AsyncModbusTcpClient(
                cfg.host, port=cfg.port, timeout=cfg.timeout_s, retries=1, reconnect_delay=0
            )
        else:
            self._client = AsyncModbusSerialClient(
                cfg.serial_port or "",
                baudrate=cfg.baudrate,
                parity=cfg.parity,
                stopbits=cfg.stopbits,
                timeout=cfg.timeout_s,
                retries=1,
                reconnect_delay=0,
            )
        if not await self._client.connect():
            await self.close()
            target = cfg.host or cfg.serial_port
            raise SourceError(f"cannot connect to Modbus device at {target}")

    async def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if self._client is None:
            raise SourceError("not connected")
        try:
            response = await getattr(self._client, method)(
                *args, device_id=self.config.unit_id, **kwargs
            )
        except Exception as exc:
            raise SourceError(f"{method} failed: {exc}") from exc
        if response.isError():
            raise SourceError(f"{method} returned {response}")
        return response

    async def _read_block(self, block: ReadBlock) -> list[Any]:
        method = {
            "co": "read_coils",
            "di": "read_discrete_inputs",
            "hr": "read_holding_registers",
            "ir": "read_input_registers",
        }[block.area]
        response = await self._call(method, block.start, count=block.count)
        if block.area in ("co", "di"):
            return list(response.bits[: block.count])
        return list(response.registers)

    async def read(self) -> dict[str, Reading]:
        cfg = self.config
        values: dict[str, Reading] = {}
        for block in self.blocks:
            data = await self._read_block(block)
            ts = now_ms()
            for plan in block.tags:
                index = plan.address.offset - block.start
                if block.area in ("co", "di"):
                    value: Any = bool(data[index])
                else:
                    value = decode_registers(
                        data[index : index + plan.count],
                        plan.datatype,
                        cfg.byte_order,
                        cfg.word_order,
                        plan.address.bit,
                    )
                values[plan.tag.name] = Reading(value, ts)
        return values

    async def write(self, tag: TagConfig, value: Any) -> None:
        plan = self._plans[tag.name]
        address = plan.address
        if address.area == "co":
            await self._call("write_coil", address.offset, bool(value))
            return
        if address.bit is not None:
            response = await self._call("read_holding_registers", address.offset, count=1)
            current = response.registers[0]
            mask = 1 << address.bit
            new = (current | mask) if value else (current & ~mask)
            await self._call("write_register", address.offset, new & 0xFFFF)
            return
        if plan.datatype.startswith(("int", "uint")):
            value = round(value)
        try:
            registers = encode_value(
                value, plan.datatype, self.config.byte_order, self.config.word_order, tag.length
            )
        except (struct.error, ValueError, TypeError) as exc:
            raise SourceError(f"cannot encode {value!r} as {plan.datatype}: {exc}") from exc
        await self._call("write_registers", address.offset, registers)
