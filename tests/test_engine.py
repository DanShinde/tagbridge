import asyncio
from typing import Any

import pytest

from tagbridge.config import parse_config
from tagbridge.engine import Gateway, WriteError
from tagbridge.model import Reading
from tagbridge.sources.base import Source, SourceError


class FakeSource(Source):
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.fail = False
        self.writes: list[tuple[str, Any]] = []
        self.connects = 0

    async def connect(self) -> None:
        if self.fail:
            raise SourceError("down")
        self.connects += 1

    async def close(self) -> None:
        pass

    async def read(self) -> dict[str, Reading]:
        if self.fail:
            raise SourceError("lost")
        return {k: Reading(v, 1) for k, v in self.values.items()}

    async def write(self, tag, value) -> None:
        self.writes.append((tag.name, value))
        self.values[tag.name] = value


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    def device_online(self, device):
        self.events.append(("online", dict(device.values)))

    def device_offline(self, device):
        self.events.append(("offline", None))

    def device_data(self, device, changes):
        self.events.append(("data", {k: r.value for k, r in changes.items()}))


CONFIG = {
    "sparkplug": {"group_id": "G", "edge_node_id": "E"},
    "devices": [
        {
            "name": "d",
            "type": "modbus",
            "host": "x",
            "scan_ms": 10,
            "reconnect_min_s": 0.01,
            "reconnect_max_s": 0.02,
            "tags": [
                {"name": "temp", "address": "hr:0", "deadband": 0.5},
                {"name": "sp", "address": "hr:1", "scale": 0.1, "writable": True},
                {"name": "ro", "address": "hr:2"},
            ],
        }
    ],
}


@pytest.fixture
def setup():
    source = FakeSource()
    gateway = Gateway(parse_config(CONFIG), source_factory=lambda cfg: source)
    recorder = Recorder()
    gateway.add_listener(recorder)
    return gateway, source, recorder


async def _until(predicate, timeout=2.0):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait(), timeout)


async def test_report_by_exception_with_deadband(setup):
    gateway, source, recorder = setup
    source.values = {"temp": 20.0, "sp": 700, "ro": 1}
    task = asyncio.create_task(gateway.run_device(gateway.devices["d"]))
    try:
        await _until(lambda: recorder.events)
        kind, values = recorder.events[0]
        assert kind == "online"
        assert values["sp"].value == 70.0
        source.values["temp"] = 20.3  # inside deadband
        await asyncio.sleep(0.05)
        assert len(recorder.events) == 1
        source.values["temp"] = 20.6
        await _until(lambda: len(recorder.events) == 2)
        assert recorder.events[1] == ("data", {"temp": 20.6})
    finally:
        task.cancel()


async def test_offline_and_reconnect(setup):
    gateway, source, recorder = setup
    source.values = {"temp": 1.0, "sp": 1, "ro": 1}
    task = asyncio.create_task(gateway.run_device(gateway.devices["d"]))
    try:
        await _until(lambda: recorder.events)
        source.fail = True
        await _until(lambda: recorder.events[-1][0] == "offline")
        assert not gateway.devices["d"].online
        source.fail = False
        await _until(lambda: recorder.events[-1][0] == "online")
        assert source.connects == 2
        assert gateway.stats["read_errors"] == 1
    finally:
        task.cancel()


async def test_write_scaling_and_checks(setup):
    gateway, source, _ = setup
    source.values = {"temp": 1.0, "sp": 1, "ro": 1}
    with pytest.raises(WriteError, match="offline"):
        await gateway.write("d", "sp", 1)
    task = asyncio.create_task(gateway.run_device(gateway.devices["d"]))
    try:
        await _until(lambda: gateway.devices["d"].online)
        await gateway.write("d", "sp", 65.5)
        assert source.writes == [("sp", 655)]
        with pytest.raises(WriteError, match="not writable"):
            await gateway.write("d", "ro", 1)
        with pytest.raises(WriteError, match="unknown tag"):
            await gateway.write("d", "nope", 1)
        with pytest.raises(WriteError, match="unknown device"):
            await gateway.write("x", "sp", 1)
        with pytest.raises(WriteError, match="bad value"):
            await gateway.write("d", "sp", "abc")
    finally:
        task.cancel()
