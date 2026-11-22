import json

from tagbridge.config import SparkplugConfig, UnsConfig, parse_config
from tagbridge.engine import DeviceState
from tagbridge.model import Reading
from tagbridge.sinks.sparkplug_node import SparkplugNode
from tagbridge.sinks.uns import UnsNode
from tagbridge.sparkplug import DataType, Metric, decode_payload

CFG = parse_config(
    {
        "sparkplug": {"group_id": "G", "edge_node_id": "E"},
        "devices": [
            {
                "name": "plc",
                "type": "modbus",
                "host": "h",
                "tags": [
                    {"name": "a/temp", "address": "hr:0", "datatype": "float32", "unit": "C"},
                    {"name": "run", "address": "co:0", "writable": True},
                ],
            }
        ],
    }
)


def _device() -> DeviceState:
    state = DeviceState(CFG.devices[0], source=None)  # type: ignore[arg-type]
    state.online = True
    state.values = {"a/temp": Reading(21.5, 100), "run": Reading(True, 100)}
    state.datatypes = {"a/temp": "float32", "run": "bool"}
    return state


def test_sparkplug_sequence_and_aliases():
    node = SparkplugNode(
        SparkplugConfig(group_id="G", edge_node_id="E"), {"plc": ["a/temp", "run"]}
    )
    device = _device()
    nbirth = node.node_birth()
    assert nbirth.topic == "spBv1.0/G/NBIRTH/E"
    payload = decode_payload(nbirth.payload)
    assert payload.seq == 0
    assert payload.metrics[0].name == "bdSeq"
    dbirth = decode_payload(node.device_birth(device).payload)
    assert dbirth.seq == 1
    assert [(m.name, m.alias, m.datatype, m.value) for m in dbirth.metrics] == [
        ("a/temp", 1, DataType.FLOAT, 21.5),
        ("run", 2, DataType.BOOLEAN, True),
    ]
    ddata = node.device_data(device, {"a/temp": Reading(22.0, 200)})
    assert ddata is not None and ddata.topic == "spBv1.0/G/DDATA/E/plc"
    data = decode_payload(ddata.payload)
    assert data.seq == 2
    assert (data.metrics[0].name, data.metrics[0].alias, data.metrics[0].value) == (None, 1, 22.0)
    bad = decode_payload(node.device_data(device, {"run": Reading(None, 300, False)}).payload)
    assert bad.metrics[0].is_null
    assert node.resolve("plc", Metric(alias=2)) == "run"
    assert node.resolve("other", Metric(alias=2)) is None
    death = node.death()
    assert death.qos == 1 and decode_payload(death.payload).metrics[0].value == 0


def test_seq_wraps():
    node = SparkplugNode(SparkplugConfig(), {"plc": ["a/temp", "run"]})
    device = _device()
    node.node_birth()
    for _ in range(300):
        message = node.device_death(device)
    assert decode_payload(message.payload).seq == 300 % 256


def test_uns_messages_and_commands():
    uns = UnsNode(UnsConfig(enabled=True, topic_prefix="/acme/site/"))
    device = _device()
    messages = uns.device_birth(device)
    assert messages[0].topic == "acme/site/plc/_status" and messages[0].retain
    temp = messages[1]
    assert temp.topic == "acme/site/plc/a/temp"
    body = json.loads(temp.payload)
    assert body == {"value": 21.5, "timestamp": 100, "quality": "good", "unit": "C"}
    assert uns.command_topics([device]) == ["acme/site/plc/run/set"]
    assert uns.parse_command("acme/site/plc/a/temp/set", b"25") == ("plc", "a/temp", 25)
    assert uns.parse_command("acme/site/plc/run/set", b'{"value": false}') == ("plc", "run", False)
    assert uns.parse_command("acme/site/plc/run/set", b"on") == ("plc", "run", "on")
    assert uns.parse_command("acme/site/plc/run", b"1") is None


def test_uns_value_payload():
    uns = UnsNode(UnsConfig(enabled=True, topic_prefix="p", payload="value"))
    assert uns.tag(_device(), "run", Reading(True, 1)).payload == b"true"
