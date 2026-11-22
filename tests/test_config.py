import pytest

from tagbridge.config import ConfigError, load_config, parse_config, substitute_env

BASE = {"sparkplug": {"group_id": "G", "edge_node_id": "E"}}


def test_env_substitution_with_defaults():
    env = {"HOST": "broker", "EMPTY": ""}
    text = "a: ${HOST}\nb: ${MISSING:-fallback}\nc: ${EMPTY:-x}"
    assert substitute_env(text, env) == "a: broker\nb: fallback\nc: x"


def test_env_substitution_missing_raises():
    with pytest.raises(ConfigError, match="NOPE"):
        substitute_env("${NOPE}", {})


def test_load_config_file(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text(
        """
mqtt: {host: "${H:-localhost}"}
devices:
  - name: plc1
    type: modbus
    host: 10.0.0.5
    tags:
      - {name: line/speed, address: "hr:0", datatype: float32}
  - name: ua
    type: opcua
    endpoint: opc.tcp://x:4840
    tags:
      - {name: Motor.Speed, node: "ns=2;s=Motor.Speed", unit: rpm}
"""
    )
    config = load_config(path, env={"H": "mq"})
    assert config.mqtt.host == "mq"
    assert config.devices[0].tags[0].name == "line/speed"
    assert config.devices[1].tags[0].address == "ns=2;s=Motor.Speed"
    assert config.devices[1].tags[0].unit == "rpm"


@pytest.mark.parametrize(
    "device, message",
    [
        ({"name": "a", "type": "modbus", "tags": []}, "exactly one of host or serial_port"),
        (
            {"name": "a", "type": "modbus", "host": "h", "serial_port": "/dev/ttyUSB0"},
            "exactly one",
        ),
        (
            {
                "name": "a",
                "type": "modbus",
                "host": "h",
                "tags": [{"name": "x", "address": "hr:0"}, {"name": "x", "address": "hr:1"}],
            },
            "duplicate tag",
        ),
        ({"name": "a/b", "type": "opcua", "endpoint": "opc.tcp://x"}, "device names"),
        (
            {
                "name": "a",
                "type": "opcua",
                "endpoint": "e",
                "tags": [{"name": "x#", "node": "i=1"}],
            },
            "invalid tag name",
        ),
    ],
)
def test_invalid_devices(device, message):
    with pytest.raises(ConfigError, match=message):
        parse_config({**BASE, "devices": [device]})


def test_duplicate_devices():
    dev = {"name": "a", "type": "opcua", "endpoint": "e"}
    with pytest.raises(ConfigError, match="duplicate device"):
        parse_config({**BASE, "devices": [dev, dev]})


def test_needs_an_output():
    with pytest.raises(ConfigError, match="at least one"):
        parse_config({"sparkplug": {"enabled": False}})


def test_unknown_keys_rejected():
    with pytest.raises(ConfigError, match="extra"):
        parse_config({**BASE, "mqtt": {"hots": "x"}})
