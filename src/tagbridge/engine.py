"""Device scan loops and the gateway that ties sources to publishers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import GatewayConfig, ModbusDeviceConfig, OpcUaDeviceConfig, TagConfig
from .model import Reading, apply_scaling, changed, coerce, effective_datatype, remove_scaling
from .sources import Source, create_source
from .sources.base import SourceError

log = logging.getLogger(__name__)

DeviceConfigT = ModbusDeviceConfig | OpcUaDeviceConfig


class WriteError(Exception):
    """A write request was rejected or failed."""


class Listener(Protocol):
    def device_online(self, device: DeviceState) -> None: ...

    def device_offline(self, device: DeviceState) -> None: ...

    def device_data(self, device: DeviceState, changes: dict[str, Reading]) -> None: ...


@dataclass
class DeviceState:
    config: DeviceConfigT
    source: Source
    online: bool = False
    values: dict[str, Reading] = field(default_factory=dict)
    datatypes: dict[str, str] = field(default_factory=dict)
    last_error: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def tags(self) -> dict[str, TagConfig]:
        return {t.name: t for t in self.config.tags}


class Gateway:
    def __init__(
        self,
        config: GatewayConfig,
        source_factory: Callable[[DeviceConfigT], Source] = create_source,
    ) -> None:
        self.config = config
        self.devices: dict[str, DeviceState] = {
            d.name: DeviceState(d, source_factory(d)) for d in config.active_devices
        }
        self.listeners: list[Listener] = []
        self.stats: Counter[str] = Counter()
        self.mqtt_connected = False

    def add_listener(self, listener: Listener) -> None:
        self.listeners.append(listener)

    def _notify(self, method: str, *args: Any) -> None:
        for listener in self.listeners:
            try:
                getattr(listener, method)(*args)
            except Exception:
                log.exception("listener %r failed in %s", listener, method)

    async def run_device(self, state: DeviceState) -> None:
        cfg = state.config
        delay = cfg.reconnect_min_s
        scan_s = cfg.scan_ms / 1000
        loop = asyncio.get_running_loop()
        while True:
            try:
                await state.source.connect()
            except SourceError as exc:
                self._record_error(state, exc, "connect")
                await asyncio.sleep(delay)
                delay = min(delay * 2, cfg.reconnect_max_s)
                continue
            delay = cfg.reconnect_min_s
            log.info("%s: connected", cfg.name)
            try:
                while True:
                    started = loop.time()
                    async with state.lock:
                        raw = await state.source.read()
                    self.stats["reads"] += 1
                    self._process(state, raw)
                    await asyncio.sleep(max(0.0, scan_s - (loop.time() - started)))
            except SourceError as exc:
                self._record_error(state, exc, "read")
            finally:
                if state.online:
                    state.online = False
                    self._notify("device_offline", state)
                with contextlib.suppress(Exception):
                    await state.source.close()
            await asyncio.sleep(delay)

    def _record_error(self, state: DeviceState, exc: Exception, stage: str) -> None:
        self.stats[f"{stage}_errors"] += 1
        message = str(exc)
        if message != state.last_error:
            log.warning("%s: %s error: %s", state.name, stage, message)
        state.last_error = message

    def _process(self, state: DeviceState, raw: dict[str, Reading]) -> None:
        tags = state.tags
        readings: dict[str, Reading] = {}
        for name, reading in raw.items():
            tag = tags.get(name)
            if tag is None:
                continue
            try:
                value = apply_scaling(tag, reading.value)
            except (TypeError, ValueError):
                readings[name] = Reading(None, reading.timestamp, False)
                continue
            readings[name] = Reading(value, reading.timestamp, reading.good)
        if not state.online:
            state.values = readings
            state.datatypes = {name: self._datatype(state, tag) for name, tag in tags.items()}
            state.online = True
            state.last_error = None
            self._notify("device_online", state)
            return
        changes = {
            name: reading
            for name, reading in readings.items()
            if changed(tags[name], state.values.get(name), reading)
        }
        if changes:
            state.values.update(changes)
            self.stats["changes"] += len(changes)
            self._notify("device_data", state, changes)

    @staticmethod
    def _datatype(state: DeviceState, tag: TagConfig) -> str:
        if tag.scale is not None or tag.offset is not None:
            return effective_datatype(tag)
        datatype_of = getattr(state.source, "datatype_of", None)
        if datatype_of is not None:
            resolved: str = datatype_of(tag.name)
            return resolved
        return tag.datatype

    async def write(self, device: str, tag_name: str, value: Any) -> None:
        state = self.devices.get(device)
        if state is None:
            raise WriteError(f"unknown device {device!r}")
        tag = state.tags.get(tag_name)
        if tag is None:
            raise WriteError(f"unknown tag {tag_name!r} on {device!r}")
        if not tag.writable:
            raise WriteError(f"tag {device}/{tag_name} is not writable")
        if not state.online:
            raise WriteError(f"device {device!r} is offline")
        try:
            raw = remove_scaling(tag, coerce(tag, value))
        except (TypeError, ValueError) as exc:
            raise WriteError(f"bad value for {device}/{tag_name}: {exc}") from exc
        try:
            async with state.lock:
                await state.source.write(tag, raw)
        except Exception as exc:
            self.stats["write_errors"] += 1
            raise WriteError(f"write {device}/{tag_name} failed: {exc}") from exc
        self.stats["writes"] += 1
        log.info("%s/%s <- %r", device, tag_name, value)

    async def run(self, extra: list[Callable[[], Any]] | None = None) -> None:
        """Run all device loops plus any extra coroutines until cancelled."""
        async with asyncio.TaskGroup() if hasattr(asyncio, "TaskGroup") else _Group() as group:
            for state in self.devices.values():
                group.create_task(self.run_device(state), name=f"device:{state.name}")
            for factory in extra or []:
                group.create_task(factory())


class _Group:
    """Small TaskGroup stand-in for Python 3.10."""

    def __init__(self) -> None:
        self._tasks: list[asyncio.Task[Any]] = []

    async def __aenter__(self) -> _Group:
        return self

    def create_task(self, coro: Any, name: str | None = None) -> asyncio.Task[Any]:
        task: asyncio.Task[Any] = asyncio.ensure_future(coro)
        self._tasks.append(task)
        return task

    async def __aexit__(self, *exc: Any) -> None:
        try:
            await asyncio.gather(*self._tasks)
        finally:
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
