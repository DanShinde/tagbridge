"""A small Modbus TCP + OPC UA server with moving values, for demos and tests."""

from __future__ import annotations

import asyncio
import logging
import math
import struct
import time
from typing import Any

from .sources.modbus import encode_value

log = logging.getLogger(__name__)


class ModbusMemory:
    """In-memory Modbus data model served by :class:`MiniModbusServer`."""

    def __init__(self, size: int = 1000) -> None:
        self.co = [False] * size
        self.di = [False] * size
        self.hr = [0] * size
        self.ir = [0] * size


class MiniModbusServer:
    """A minimal Modbus TCP server (function codes 1-6, 15, 16) for demos and tests."""

    def __init__(self, memory: ModbusMemory, host: str = "0.0.0.0", port: int = 5020) -> None:
        self.memory, self.host, self.port = memory, host, port
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._client, self.host, self.port)
        sockets: list[Any] = list(self._server.sockets or [])
        if sockets and self.port == 0:
            self.port = sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                header = await reader.readexactly(7)
                tid, pid, length, unit = struct.unpack(">HHHB", header)
                pdu = await reader.readexactly(length - 1)
                reply = self.handle(pdu)
                writer.write(struct.pack(">HHHB", tid, pid, len(reply) + 1, unit) + reply)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    def handle(self, pdu: bytes) -> bytes:
        fc = pdu[0]
        mem = self.memory
        try:
            if fc in (1, 2):
                addr, count = struct.unpack(">HH", pdu[1:5])
                bits: list[bool] = (mem.co if fc == 1 else mem.di)[addr : addr + count]
                if len(bits) != count:
                    return bytes([fc | 0x80, 2])
                data = bytearray((count + 7) // 8)
                for i, bit in enumerate(bits):
                    if bit:
                        data[i // 8] |= 1 << (i % 8)
                return bytes([fc, len(data)]) + bytes(data)
            if fc in (3, 4):
                addr, count = struct.unpack(">HH", pdu[1:5])
                regs = (mem.hr if fc == 3 else mem.ir)[addr : addr + count]
                if len(regs) != count:
                    return bytes([fc | 0x80, 2])
                return bytes([fc, count * 2]) + struct.pack(f">{count}H", *regs)
            if fc == 5:
                addr, value = struct.unpack(">HH", pdu[1:5])
                mem.co[addr] = value == 0xFF00
                return pdu[:5]
            if fc == 6:
                addr, value = struct.unpack(">HH", pdu[1:5])
                mem.hr[addr] = value
                return pdu[:5]
            if fc == 15:
                addr, count = struct.unpack(">HH", pdu[1:5])
                coil_bytes = pdu[6:]
                for i in range(count):
                    mem.co[addr + i] = bool(coil_bytes[i // 8] >> (i % 8) & 1)
                return pdu[:5]
            if fc == 16:
                addr, count = struct.unpack(">HH", pdu[1:5])
                mem.hr[addr : addr + count] = struct.unpack(f">{count}H", pdu[6 : 6 + count * 2])
                return pdu[:5]
        except (IndexError, struct.error):
            return bytes([fc | 0x80, 3])
        return bytes([fc | 0x80, 1])


class DemoModbus:
    """Holding registers: 0 temperature float32, 2 pressure x10 uint16, 3 part count uint32,
    10 setpoint x10 uint16 (writable), 20 alarm word (bit 0 high alarm). Coil 0: running."""

    def __init__(self, host: str = "0.0.0.0", port: int = 5020) -> None:
        self.memory = ModbusMemory()
        self.server = MiniModbusServer(self.memory, host, port)
        self.memory.hr[10] = 700
        self.memory.co[0] = True
        self.count = 0

    def tick(self, t: float) -> None:
        mem = self.memory
        running = mem.co[0]
        setpoint = mem.hr[10] / 10
        temperature = setpoint + 5 * math.sin(t / 10) if running else 20.0
        pressure = 42 + 3 * math.sin(t / 7) if running else 0.0
        if running:
            self.count += 1
        mem.hr[0:2] = encode_value(temperature, "float32")
        mem.hr[2] = round(pressure * 10)
        mem.hr[3:5] = encode_value(self.count, "uint32")
        mem.hr[20] = 1 if temperature > setpoint + 4 else 0

    async def start(self) -> None:
        await self.server.start()
        log.info("demo Modbus TCP server on %s:%s", self.server.host, self.server.port)


class DemoOpcUa:
    """Variables under ns=2: Line1.Speed, Line1.State, Line1.Count, Line1.Target (writable)."""

    def __init__(self, host: str = "0.0.0.0", port: int = 4840) -> None:
        self.endpoint = f"opc.tcp://{host}:{port}/tagbridge/demo"
        self.server: Any = None
        self.vars: dict[str, Any] = {}

    async def start(self) -> None:
        from asyncua import Server, ua

        server = Server()
        await server.init()
        server.set_endpoint(self.endpoint)
        server.set_server_name("tagbridge demo")
        server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        ns = await server.register_namespace("urn:tagbridge:demo")
        line = await server.nodes.objects.add_object(ua.NodeId("Line1", ns), "Line1")
        specs = {
            "Speed": (0.0, ua.VariantType.Double),
            "State": ("Running", ua.VariantType.String),
            "Count": (0, ua.VariantType.Int32),
            "Target": (120.0, ua.VariantType.Double),
        }
        for name, (value, variant_type) in specs.items():
            var = await line.add_variable(
                ua.NodeId(f"Line1.{name}", ns), name, ua.Variant(value, variant_type)
            )
            self.vars[name] = var
        await self.vars["Target"].set_writable()
        self.server = server
        await server.start()
        log.info("demo OPC UA server on %s", self.endpoint)

    async def tick(self, t: float) -> None:
        from asyncua import ua

        target = await self.vars["Target"].read_value()
        speed = target + 4 * math.sin(t / 5)
        state = "Running" if int(t) % 60 < 50 else "Changeover"
        await self.vars["Speed"].write_value(ua.Variant(speed, ua.VariantType.Double))
        await self.vars["State"].write_value(ua.Variant(state, ua.VariantType.String))
        count = await self.vars["Count"].read_value()
        await self.vars["Count"].write_value(ua.Variant(count + 1, ua.VariantType.Int32))

    async def stop(self) -> None:
        if self.server is not None:
            await self.server.stop()


async def run_demo(
    host: str = "0.0.0.0",
    modbus_port: int | None = 5020,
    opcua_port: int | None = 4840,
    interval_s: float = 1.0,
) -> None:
    modbus = DemoModbus(host, modbus_port) if modbus_port else None
    opcua = DemoOpcUa(host, opcua_port) if opcua_port else None
    if modbus:
        await modbus.start()
    if opcua:
        await opcua.start()
    start = time.monotonic()
    try:
        while True:
            t = time.monotonic() - start
            if modbus:
                modbus.tick(t)
            if opcua:
                await opcua.tick(t)
            await asyncio.sleep(interval_s)
    finally:
        if modbus:
            await modbus.server.stop()
        if opcua:
            await opcua.stop()
