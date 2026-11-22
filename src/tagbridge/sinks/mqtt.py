"""MQTT connection that publishes Sparkplug B and/or Unified Namespace messages."""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any

import aiomqtt

from ..config import GatewayConfig
from ..engine import DeviceState, Gateway, WriteError
from ..model import Reading
from ..sparkplug import decode_payload
from .sparkplug_node import REBIRTH, Message, SparkplugNode
from .uns import UnsNode

log = logging.getLogger(__name__)

Event = tuple[str, Any, Any]


class MqttBridge:
    """Listens to gateway events and mirrors them to an MQTT broker."""

    def __init__(self, config: GatewayConfig, gateway: Gateway) -> None:
        self.config = config
        self.gateway = gateway
        self.sparkplug = (
            SparkplugNode(
                config.sparkplug,
                {name: [t.name for t in s.config.tags] for name, s in gateway.devices.items()},
            )
            if config.sparkplug.enabled
            else None
        )
        self.uns = UnsNode(config.uns) if config.uns.enabled else None
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=10_000)
        self._connected = asyncio.Event()
        self._tasks: set[asyncio.Task[Any]] = set()
        gateway.add_listener(self)

    # Listener interface -------------------------------------------------------------

    def _put(self, event: Event) -> None:
        if not self._connected.is_set():
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self.gateway.stats["dropped"] += 1

    def device_online(self, device: DeviceState) -> None:
        self._put(("online", device, None))

    def device_offline(self, device: DeviceState) -> None:
        self._put(("offline", device, None))

    def device_data(self, device: DeviceState, changes: dict[str, Reading]) -> None:
        self._put(("data", device, dict(changes)))

    # Connection ---------------------------------------------------------------------

    def _client(self) -> aiomqtt.Client:
        cfg = self.config.mqtt
        if self.sparkplug is not None:
            death = self.sparkplug.death()
            will = aiomqtt.Will(death.topic, death.payload, qos=death.qos, retain=False)
        else:
            assert self.uns is not None
            status = self.uns.gateway_status(False)
            will = aiomqtt.Will(status.topic, status.payload, qos=1, retain=True)
        tls_params = None
        if cfg.tls.enabled:
            tls_params = aiomqtt.TLSParameters(
                ca_certs=cfg.tls.ca_file, certfile=cfg.tls.cert_file, keyfile=cfg.tls.key_file
            )
        client_id = cfg.client_id or (
            f"tagbridge-{self.config.sparkplug.edge_node_id}"
            if self.sparkplug
            else f"tagbridge-{socket.gethostname()}"
        )
        return aiomqtt.Client(
            cfg.host,
            cfg.port,
            username=cfg.username,
            password=cfg.password,
            identifier=client_id,
            will=will,
            keepalive=cfg.keepalive,
            clean_session=True,
            tls_params=tls_params,
            tls_insecure=cfg.tls.insecure if cfg.tls.enabled else None,
        )

    async def run(self) -> None:
        cfg = self.config.mqtt
        delay = cfg.reconnect_min_s
        while True:
            try:
                async with self._client() as client:
                    delay = cfg.reconnect_min_s
                    log.info("MQTT connected to %s:%s", cfg.host, cfg.port)
                    await self._session(client)
            except aiomqtt.MqttError as exc:
                log.warning("MQTT connection to %s:%s lost: %s", cfg.host, cfg.port, exc)
            finally:
                self._connected.clear()
                self.gateway.mqtt_connected = False
                if self.sparkplug is not None:
                    self.sparkplug.bd_seq = (self.sparkplug.bd_seq + 1) % 256
            await asyncio.sleep(delay)
            delay = min(delay * 2, cfg.reconnect_max_s)

    async def _session(self, client: aiomqtt.Client) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()
        self._connected.set()
        self.gateway.mqtt_connected = True
        devices = list(self.gateway.devices.values())
        topics: list[str] = []
        if self.sparkplug is not None and self.config.sparkplug.allow_writes:
            topics += self.sparkplug.command_topics
        elif self.sparkplug is not None:
            topics.append(self.sparkplug.command_topics[0])
        if self.uns is not None and self.config.uns.allow_writes:
            topics += self.uns.command_topics(devices)
        for topic in topics:
            await client.subscribe(topic, qos=1)
        await self._births(client)
        commands = asyncio.create_task(self._commands(client))
        try:
            await self._consume(client)
        except asyncio.CancelledError:
            await asyncio.shield(self._goodbye(client))
            raise
        finally:
            commands.cancel()
            await asyncio.gather(commands, return_exceptions=True)

    async def _goodbye(self, client: aiomqtt.Client) -> None:
        """Announce a clean shutdown; the broker only sends the will on a lost connection."""
        messages = []
        if self.uns is not None:
            messages += [self.uns.device_status(d, False) for d in self.gateway.devices.values()]
            messages.append(self.uns.gateway_status(False))
        if self.sparkplug is not None:
            messages.append(self.sparkplug.death())

        async def send_all() -> None:
            for message in messages:
                await self._publish(client, message)

        try:
            await asyncio.wait_for(send_all(), 2)
        except Exception:
            log.debug("could not publish shutdown messages", exc_info=True)

    async def _consume(self, client: aiomqtt.Client) -> None:
        while True:
            kind, device, data = await self._queue.get()
            if kind == "rebirth":
                await self._births(client)
                continue
            for message in self._render(kind, device, data):
                await self._publish(client, message)

    async def _publish(self, client: aiomqtt.Client, message: Message) -> None:
        await client.publish(message.topic, message.payload, qos=message.qos, retain=message.retain)
        self.gateway.stats["published"] += 1

    async def _births(self, client: aiomqtt.Client) -> None:
        if self.uns is not None:
            await self._publish(client, self.uns.gateway_status(True))
        if self.sparkplug is not None:
            await self._publish(client, self.sparkplug.node_birth())
        for device in self.gateway.devices.values():
            if device.online:
                for message in self._render("online", device, None):
                    await self._publish(client, message)
            elif self.uns is not None:
                await self._publish(client, self.uns.device_status(device, False))

    def _render(self, kind: str, device: DeviceState, data: Any) -> list[Message]:
        messages: list[Message] = []
        if self.sparkplug is not None:
            if kind == "online":
                messages.append(self.sparkplug.device_birth(device))
            elif kind == "offline":
                messages.append(self.sparkplug.device_death(device))
            elif kind == "data":
                message = self.sparkplug.device_data(device, data)
                if message is not None:
                    messages.append(message)
        if self.uns is not None:
            if kind == "online":
                messages += self.uns.device_birth(device)
            elif kind == "offline":
                messages.append(self.uns.device_status(device, False))
            elif kind == "data":
                messages += [self.uns.tag(device, name, r) for name, r in data.items()]
        return messages

    # Commands -----------------------------------------------------------------------

    async def _commands(self, client: aiomqtt.Client) -> None:
        async for message in client.messages:
            topic = message.topic.value
            payload = message.payload
            if not isinstance(payload, (bytes, bytearray)):
                payload = str(payload).encode("utf-8")
            try:
                await self._handle_command(topic, bytes(payload))
            except Exception:
                log.exception("failed to handle command on %s", topic)

    async def _handle_command(self, topic: str, payload: bytes) -> None:
        if self.sparkplug is not None and topic.startswith("spBv1.0/"):
            parts = topic.split("/")
            decoded = decode_payload(payload)
            if parts[2] == "NCMD":
                if any(m.name == REBIRTH and m.value for m in decoded.metrics):
                    log.info("rebirth requested")
                    self._put(("rebirth", None, None))
                return
            if parts[2] == "DCMD" and len(parts) == 5:
                device = parts[4]
                for metric in decoded.metrics:
                    tag = self.sparkplug.resolve(device, metric)
                    if tag is not None and not metric.is_null:
                        self._spawn_write(device, tag, metric.value)
            return
        if self.uns is not None:
            command = self.uns.parse_command(topic, payload)
            if command is not None:
                self._spawn_write(*command)

    def _spawn_write(self, device: str, tag: str, value: Any) -> None:
        task = asyncio.create_task(self._write(device, tag, value))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _write(self, device: str, tag: str, value: Any) -> None:
        try:
            await self.gateway.write(device, tag, value)
        except WriteError as exc:
            log.warning("write rejected: %s", exc)
