"""Sparkplug B edge node message builder (no I/O)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .. import __version__
from ..config import SparkplugConfig
from ..engine import DeviceState
from ..model import Reading, now_ms
from ..sparkplug import DataType, Metric, Payload, datatype_for, encode_payload, topic

log = logging.getLogger(__name__)

REBIRTH = "Node Control/Rebirth"
BDSEQ = "bdSeq"


@dataclass(frozen=True)
class Message:
    topic: str
    payload: bytes
    qos: int = 0
    retain: bool = False


def _convert(value: Any, datatype: DataType) -> Any:
    if value is None:
        return None
    if datatype == DataType.BOOLEAN:
        return bool(value)
    if datatype in (DataType.FLOAT, DataType.DOUBLE):
        return float(value)
    if datatype in (DataType.STRING, DataType.TEXT):
        return str(value)
    return int(value)


class SparkplugNode:
    """Builds Sparkplug B topics and payloads for one edge node.

    Each configured device becomes a Sparkplug device; each tag becomes a metric.
    """

    def __init__(self, config: SparkplugConfig, device_tags: dict[str, list[str]]) -> None:
        self.config = config
        self.bd_seq = 0
        self._seq = 0
        self._aliases: dict[tuple[str, str], int] = {}
        self._by_alias: dict[int, tuple[str, str]] = {}
        self._types: dict[tuple[str, str], DataType] = {}
        alias = 1
        for device, tags in device_tags.items():
            for tag in tags:
                self._aliases[(device, tag)] = alias
                self._by_alias[alias] = (device, tag)
                alias += 1

    def _topic(self, message_type: str, device: str | None = None) -> str:
        return topic(self.config.group_id, message_type, self.config.edge_node_id, device)

    def _next_seq(self) -> int:
        seq = self._seq
        self._seq = (self._seq + 1) % 256
        return seq

    @property
    def command_topics(self) -> list[str]:
        return [self._topic("NCMD"), self._topic("DCMD", "+")]

    def death(self) -> Message:
        """The NDEATH message, registered as the MQTT will for the current session."""
        payload = Payload(
            timestamp=now_ms(),
            metrics=[Metric(BDSEQ, self.bd_seq, DataType.INT64)],
        )
        return Message(self._topic("NDEATH"), encode_payload(payload), qos=1)

    def node_birth(self) -> Message:
        self._seq = 0
        payload = Payload(
            timestamp=now_ms(),
            seq=self._next_seq(),
            metrics=[
                Metric(BDSEQ, self.bd_seq, DataType.INT64),
                Metric(REBIRTH, False, DataType.BOOLEAN),
                Metric("Node Info/Software", "tagbridge", DataType.STRING),
                Metric("Node Info/Version", __version__, DataType.STRING),
            ],
        )
        return Message(self._topic("NBIRTH"), encode_payload(payload))

    def _metric(self, device: str, tag: str, reading: Reading, birth: bool) -> Metric:
        key = (device, tag)
        datatype = self._types[key]
        try:
            value = _convert(reading.value, datatype) if reading.good else None
        except (TypeError, ValueError):
            value = None
        alias = self._aliases.get(key) if self.config.use_aliases else None
        name = tag if birth or alias is None else None
        return Metric(
            name=name,
            value=value,
            datatype=datatype,
            alias=alias,
            timestamp=reading.timestamp,
            is_null=value is None,
        )

    def device_birth(self, device: DeviceState) -> Message:
        metrics = []
        for tag in device.config.tags:
            reading = device.values.get(tag.name, Reading(None, now_ms(), False))
            key = (device.name, tag.name)
            self._types[key] = datatype_for(device.datatypes.get(tag.name, "auto"), reading.value)
            metrics.append(self._metric(device.name, tag.name, reading, birth=True))
        payload = Payload(timestamp=now_ms(), seq=self._next_seq(), metrics=metrics)
        return Message(self._topic("DBIRTH", device.name), encode_payload(payload))

    def device_data(self, device: DeviceState, changes: dict[str, Reading]) -> Message | None:
        metrics = [
            self._metric(device.name, name, reading, birth=False)
            for name, reading in changes.items()
            if (device.name, name) in self._types
        ]
        if not metrics:
            return None
        payload = Payload(timestamp=now_ms(), seq=self._next_seq(), metrics=metrics)
        return Message(self._topic("DDATA", device.name), encode_payload(payload))

    def device_death(self, device: DeviceState) -> Message:
        payload = Payload(timestamp=now_ms(), seq=self._next_seq())
        return Message(self._topic("DDEATH", device.name), encode_payload(payload))

    def resolve(self, device: str, metric: Metric) -> str | None:
        """Find the tag name a command metric refers to, by name or alias."""
        if metric.name:
            return metric.name
        if metric.alias is not None:
            found = self._by_alias.get(metric.alias)
            if found and found[0] == device:
                return found[1]
        return None
