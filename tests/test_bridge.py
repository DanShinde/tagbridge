from tagbridge.config import parse_config
from tagbridge.engine import Gateway
from tagbridge.model import Reading
from tagbridge.sinks.mqtt import MqttBridge

CONFIG = {
    "sparkplug": {"group_id": "G", "edge_node_id": "E"},
    "uns": {"enabled": True, "topic_prefix": "p"},
    "devices": [
        {"name": "d", "type": "modbus", "host": "h", "tags": [{"name": "t", "address": "hr:0"}]}
    ],
}


def _bridge():
    gateway = Gateway(parse_config(CONFIG), source_factory=lambda cfg: None)  # type: ignore[arg-type,return-value]
    bridge = MqttBridge(gateway.config, gateway)
    device = gateway.devices["d"]
    device.online = True
    device.values = {"t": Reading(1, 1)}
    device.datatypes = {"t": "uint16"}
    return bridge, device


def topics(messages):
    return [m.topic for m in messages]


def test_device_born_once_per_session():
    bridge, device = _bridge()
    first = bridge._render("online", device, None)
    assert "spBv1.0/G/DBIRTH/E/d" in topics(first)
    assert bridge._render("online", device, None) == []
    data = bridge._render("data", device, {"t": Reading(2, 2)})
    assert topics(data) == ["spBv1.0/G/DDATA/E/d", "p/d/t"]
    death = bridge._render("offline", device, None)
    assert topics(death) == ["spBv1.0/G/DDEATH/E/d", "p/d/_status"]
    assert bridge._render("offline", device, None) == []
    assert bridge._render("data", device, {"t": Reading(3, 3)}) == []
    assert "spBv1.0/G/DBIRTH/E/d" in topics(bridge._render("online", device, None))
