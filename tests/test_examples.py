"""Every example config must load and every example device must build a source."""

from pathlib import Path

import pytest
import yaml

from tagbridge.config import load_config
from tagbridge.sources import Source, create_source

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

# The ${VAR:-default} placeholders in the examples resolve from these.
ENV = {"MQTT_HOST": "mqtt.example", "PLC_HOST": "plc.example", "DEMO_HOST": "demo.example"}


def _example_configs() -> list[Path]:
    return sorted(EXAMPLES.glob("*.yaml"))


def test_there_are_example_configs():
    assert _example_configs(), "expected at least one examples/*.yaml"


@pytest.mark.parametrize("path", _example_configs(), ids=lambda p: p.name)
def test_example_config_loads(path):
    config = load_config(path, env=ENV)
    # Environment substitution really happened.
    assert config.mqtt.host == "mqtt.example"
    assert config.devices, f"{path.name} has no devices"


@pytest.mark.parametrize("path", _example_configs(), ids=lambda p: p.name)
def test_example_devices_build_sources(path):
    config = load_config(path, env=ENV)
    for device in config.active_devices:
        source = create_source(device)
        assert isinstance(source, Source), f"{path.name}: {device.name} built {type(source)}"


def test_fakeplc_compose_example_has_the_three_services():
    compose = yaml.safe_load(
        (EXAMPLES / "fakeplc" / "docker-compose.yml").read_text(encoding="utf-8")
    )
    services = compose["services"]
    assert set(services) == {"mosquitto", "fakeplc", "tagbridge"}
    assert services["mosquitto"]["image"].startswith("eclipse-mosquitto:")
    # fakeplc runs its bundled line1.yaml and exposes Modbus + OPC UA.
    fakeplc = services["fakeplc"]
    assert "fakeplc serve /line1.yaml" in fakeplc["command"][-1]
    assert any(v.endswith(":/line1.yaml:ro") for v in fakeplc["volumes"])
    assert "5020:5020" in fakeplc["ports"] and "4840:4840" in fakeplc["ports"]
    # tagbridge points at the broker and the fake PLC and mounts the line1 mapping.
    tagbridge = services["tagbridge"]
    assert tagbridge["environment"]["MQTT_HOST"] == "mosquitto"
    assert tagbridge["environment"]["PLC_HOST"] == "fakeplc"
    assert any(v.endswith(":/config/tagbridge.yaml:ro") for v in tagbridge["volumes"])
    assert tagbridge["depends_on"] == ["mosquitto", "fakeplc"]


def test_compose_example_config_covers_fakeplc_line1_tags():
    """The mounted bridge config should expose the fakeplc line1 tags over both protocols."""
    config = load_config(EXAMPLES / "fakeplc-line1.yaml", env=ENV)
    by_name = {d.name: d for d in config.active_devices}
    assert {"line1-modbus", "line1-opcua"} <= set(by_name)
    modbus_tags = {t.name for t in by_name["line1-modbus"].tags}
    opcua_tags = {t.name for t in by_name["line1-opcua"].tags}
    for tag in ["Motor/Start", "Motor/Running", "Tank/Level", "Pump/Setpoint", "Batch/Count"]:
        assert tag in modbus_tags, f"{tag} missing from the Modbus device"
    for tag in ["Motor/Start", "Motor/Running", "Batch/Count"]:
        assert tag in opcua_tags, f"{tag} missing from the OPC UA device"


def test_fakeplc_line1_yaml_bundled_in_the_example_is_valid_plc_config():
    """The bundled fakeplc config must at least parse and name its tags."""
    data = yaml.safe_load((EXAMPLES / "fakeplc" / "line1.yaml").read_text(encoding="utf-8"))
    assert data["plc"] == "line1"
    tags = data["tags"]
    for tag in ["Motor.Start", "Tank.Level", "Pump.Setpoint", "Batch.Count", "Line.Temp"]:
        assert tag in tags, f"{tag} missing from bundled line1.yaml"
