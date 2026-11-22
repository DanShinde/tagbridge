"""The shipped Grafana dashboard must not drift from the /metrics output."""

import json
import re
from pathlib import Path

import yaml

from tagbridge.config import parse_config
from tagbridge.engine import Gateway
from tagbridge.http import render_metrics

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "deploy" / "grafana" / "tagbridge-dashboard.json"
CHART_COPY = ROOT / "deploy" / "helm" / "tagbridge" / "grafana" / "tagbridge-dashboard.json"
CHART_VALUES = ROOT / "deploy" / "helm" / "tagbridge" / "values.yaml"
CHART_TEMPLATE = ROOT / "deploy" / "helm" / "tagbridge" / "templates" / "grafanadashboard.yaml"

CONFIG = {
    "sparkplug": {"group_id": "G", "edge_node_id": "E"},
    "uns": {"enabled": True, "topic_prefix": "p"},
    "devices": [
        {"name": "d", "type": "modbus", "host": "h", "tags": [{"name": "t", "address": "hr:0"}]}
    ],
}

METRIC_NAME = re.compile(r"tagbridge_[a-z_]+")


def _gateway() -> Gateway:
    return Gateway(parse_config(CONFIG), source_factory=lambda cfg: None)  # type: ignore[arg-type,return-value]


def _dashboard() -> dict:
    return json.loads(DASHBOARD.read_text(encoding="utf-8"))


def _panel_exprs(dashboard: dict) -> list[str]:
    return [
        target.get("expr", "")
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
    ]


def test_dashboard_is_valid_json_with_panels():
    dashboard = _dashboard()
    assert dashboard["title"] == "tagbridge"
    assert len(dashboard["panels"]) >= 6
    exprs = _panel_exprs(dashboard)
    assert exprs and all(exprs), "every panel needs at least one target expr"


def test_dashboard_uses_prometheus_datasource_input_and_device_variable():
    variables = {v["name"]: v for v in _dashboard()["templating"]["list"]}
    assert variables["DS_PROMETHEUS"]["type"] == "datasource"
    assert variables["DS_PROMETHEUS"]["query"] == "prometheus"
    assert variables["device"]["type"] == "query"
    assert "tagbridge_device_online" in variables["device"]["query"]
    # Every panel targets the Prometheus input, so switching it repoints the dashboard.
    for panel in _dashboard()["panels"]:
        assert panel["datasource"]["uid"] == "${DS_PROMETHEUS}"
        for target in panel.get("targets", []):
            assert target["datasource"]["uid"] == "${DS_PROMETHEUS}"


def test_every_metric_used_by_the_dashboard_is_rendered():
    rendered = render_metrics(_gateway())
    used = {name for expr in _panel_exprs(_dashboard()) for name in METRIC_NAME.findall(expr)}
    # The dashboard must actually show the headline metrics, not an empty regex.
    assert {
        "tagbridge_device_online",
        "tagbridge_mqtt_connected",
        "tagbridge_reads_total",
        "tagbridge_read_errors_total",
        "tagbridge_published_total",
        "tagbridge_writes_total",
        "tagbridge_write_errors_total",
    } <= used
    for name in used:
        assert (name + " ") in rendered or (name + "{") in rendered, f"{name} missing from /metrics"


def test_device_online_panel_filters_on_the_device_variable():
    exprs = _panel_exprs(_dashboard())
    assert any('device=~"$device"' in expr for expr in exprs)


def test_helm_chart_can_ship_the_dashboard_as_a_sidecar_configmap():
    values = yaml.safe_load(CHART_VALUES.read_text(encoding="utf-8"))
    assert values["grafanaDashboard"]["enabled"] is False  # off by default
    template = CHART_TEMPLATE.read_text(encoding="utf-8")
    assert ".Values.grafanaDashboard.enabled" in template
    assert 'grafana_dashboard: "1"' in template
    assert "kind: ConfigMap" in template
    # The chart embeds a copy of the dashboard; keep it byte-identical.
    assert CHART_COPY.read_bytes() == DASHBOARD.read_bytes()
