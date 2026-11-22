"""End-to-end test: demo Modbus + OPC UA servers -> gateway -> real MQTT broker.

Needs a broker. Set TAGBRIDGE_TEST_MQTT=host:port (CI uses a mosquitto service);
the test is skipped when no broker is reachable.
"""

import asyncio
import json
import os
import socket

import pytest

from tagbridge.config import parse_config
from tagbridge.demo import DemoModbus, DemoOpcUa
from tagbridge.engine import Gateway
from tagbridge.sinks.mqtt import MqttBridge
from tagbridge.sparkplug import DataType, Metric, Payload, decode_payload, encode_payload

BROKER = os.environ.get("TAGBRIDGE_TEST_MQTT", "localhost:1883")
HOST, PORT = BROKER.rsplit(":", 1)


def _broker_up() -> bool:
    try:
        with socket.create_connection((HOST, int(PORT)), timeout=1):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _broker_up(), reason=f"no MQTT broker at {BROKER}")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def test_end_to_end():
    aiomqtt = pytest.importorskip("aiomqtt")
    modbus = DemoModbus("127.0.0.1", _free_port())
    opcua = DemoOpcUa("127.0.0.1", _free_port())
    await modbus.start()
    await opcua.start()
    modbus.tick(0)
    await opcua.tick(0)
    edge = f"it-{os.getpid()}"
    config = parse_config(
        {
            "mqtt": {"host": HOST, "port": int(PORT)},
            "sparkplug": {"group_id": "IT", "edge_node_id": edge},
            "uns": {"enabled": True, "topic_prefix": f"it/{edge}", "allow_writes": True},
            "http": {"enabled": False},
            "devices": [
                {
                    "name": "press",
                    "type": "modbus",
                    "host": "127.0.0.1",
                    "port": modbus.server.port,
                    "scan_ms": 100,
                    "tags": [
                        {"name": "temperature", "address": "hr:0", "datatype": "float32"},
                        {"name": "setpoint", "address": "hr:10", "scale": 0.1, "writable": True},
                        {"name": "running", "address": "co:0", "writable": True},
                    ],
                },
                {
                    "name": "line",
                    "type": "opcua",
                    "endpoint": opcua.endpoint,
                    "scan_ms": 100,
                    "tags": [
                        {"name": "speed", "node": "ns=2;s=Line1.Speed"},
                        {"name": "target", "node": "ns=2;s=Line1.Target", "writable": True},
                    ],
                },
            ],
        }
    )
    gateway = Gateway(config)
    bridge = MqttBridge(config, gateway)
    births: dict[str, Payload] = {}
    uns: dict[str, dict] = {}

    async with aiomqtt.Client(HOST, int(PORT)) as client:
        await client.subscribe(f"spBv1.0/IT/+/{edge}/#")
        await client.subscribe(f"spBv1.0/IT/+/{edge}")
        await client.subscribe(f"it/{edge}/#")
        runner = asyncio.create_task(gateway.run([bridge.run]))

        async def collect():
            async for message in client.messages:
                topic = message.topic.value
                if topic.startswith("spBv1.0"):
                    kind = topic.split("/")[2]
                    if kind in ("NBIRTH", "DBIRTH"):
                        births[topic] = decode_payload(message.payload)
                elif not topic.endswith("_status"):
                    uns[topic] = json.loads(message.payload)

        collector = asyncio.create_task(collect())
        try:

            async def wait_for(predicate):
                while not predicate():
                    await asyncio.sleep(0.05)

            await asyncio.wait_for(
                wait_for(lambda: len(births) == 3 and f"it/{edge}/line/speed" in uns), 15
            )
            press = births[f"spBv1.0/IT/DBIRTH/{edge}/press"]
            names = {m.name: m for m in press.metrics}
            assert names["setpoint"].value == 70.0
            assert names["running"].datatype == DataType.BOOLEAN
            line = births[f"spBv1.0/IT/DBIRTH/{edge}/line"]
            assert {m.name: m.datatype for m in line.metrics}["speed"] == DataType.DOUBLE

            await client.publish(
                f"spBv1.0/IT/DCMD/{edge}/press",
                encode_payload(Payload(metrics=[Metric("setpoint", 55.5, DataType.DOUBLE)])),
            )
            await client.publish(f"it/{edge}/line/target/set", b"99.5")
            await asyncio.wait_for(wait_for(lambda: modbus.memory.hr[10] == 555), 5)
            await asyncio.wait_for(
                wait_for(lambda: uns.get(f"it/{edge}/line/target", {}).get("value") == 99.5), 5
            )
        finally:
            collector.cancel()
            runner.cancel()
            await asyncio.gather(collector, runner, return_exceptions=True)
            await modbus.server.stop()
            await opcua.stop()
            await client.publish(f"it/{edge}/press/_status", b"", retain=True)
