"""Plain MQTT / Unified Namespace message builder (no I/O)."""

from __future__ import annotations

import json
from typing import Any

from ..config import UnsConfig
from ..engine import DeviceState
from ..model import Reading
from .sparkplug_node import Message

STATUS = "_status"
GATEWAY = "_gateway"


class UnsNode:
    """Publishes one retained topic per tag under ``<prefix>/<device>/<tag>``."""

    def __init__(self, config: UnsConfig) -> None:
        self.config = config

    @property
    def prefix(self) -> str:
        return self.config.topic_prefix

    def gateway_status(self, online: bool) -> Message:
        return Message(
            f"{self.prefix}/{GATEWAY}/{STATUS}",
            b"online" if online else b"offline",
            qos=1,
            retain=True,
        )

    def device_status(self, device: DeviceState, online: bool) -> Message:
        return Message(
            f"{self.prefix}/{device.name}/{STATUS}",
            b"online" if online else b"offline",
            qos=1,
            retain=True,
        )

    def tag(self, device: DeviceState, name: str, reading: Reading) -> Message:
        if self.config.payload == "value":
            body = b"" if reading.value is None else _raw(reading.value).encode("utf-8")
        else:
            doc: dict[str, Any] = {
                "value": reading.value,
                "timestamp": reading.timestamp,
                "quality": "good" if reading.good else "bad",
            }
            unit = device.tags[name].unit if name in device.tags else None
            if unit:
                doc["unit"] = unit
            body = json.dumps(doc, separators=(",", ":")).encode("utf-8")
        return Message(
            f"{self.prefix}/{device.name}/{name}", body, qos=0, retain=self.config.retain
        )

    def device_birth(self, device: DeviceState) -> list[Message]:
        messages = [self.device_status(device, True)]
        messages += [self.tag(device, name, r) for name, r in device.values.items()]
        return messages

    def command_topics(self, devices: list[DeviceState]) -> list[str]:
        return [
            f"{self.prefix}/{device.name}/{tag.name}/set"
            for device in devices
            for tag in device.config.tags
            if tag.writable
        ]

    def parse_command(self, topic: str, payload: bytes) -> tuple[str, str, Any] | None:
        """Return ``(device, tag, value)`` for ``<prefix>/<device>/<tag>/set`` messages."""
        head = self.prefix + "/"
        if not topic.startswith(head) or not topic.endswith("/set"):
            return None
        rest = topic[len(head) : -len("/set")]
        device, _, tag = rest.partition("/")
        if not device or not tag:
            return None
        text = payload.decode("utf-8").strip()
        try:
            value: Any = json.loads(text)
        except ValueError:
            value = text
        if isinstance(value, dict):
            if "value" not in value:
                return None
            value = value["value"]
        return device, tag, value


def _raw(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
