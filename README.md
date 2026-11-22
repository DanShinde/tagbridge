# tagbridge

**A small, config-driven gateway that reads tags from Modbus and OPC UA devices and publishes them to MQTT as Sparkplug B and/or a plain JSON Unified Namespace.**

[![CI](https://github.com/DanShinde/tagbridge/actions/workflows/ci.yml/badge.svg)](https://github.com/DanShinde/tagbridge/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/tagbridge.svg)](https://pypi.org/project/tagbridge/)
[![Python](https://img.shields.io/pypi/pyversions/tagbridge.svg)](https://pypi.org/project/tagbridge/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

```
 PLCs / drives / meters                 tagbridge                         MQTT broker
┌──────────────────────┐      ┌──────────────────────────────┐      ┌──────────────────────────┐
│ Modbus TCP / RTU     │─────▶│ block reads, scaling,        │─────▶│ spBv1.0/Plant1/DDATA/... │
│ OPC UA (subscribe or │◀─────│ deadband, report by exception│◀─────│ acme/plant1/press1/temp  │
│ poll)                │writes│ births, deaths, rebirths     │ cmds │                          │
└──────────────────────┘      └──────────────────────────────┘      └──────────────────────────┘
```

One YAML file, one process, one Docker image. No flow editor, no JVM, no licence server.

## Features

- **Modbus TCP and RTU**: `hr:100`, `holding 100`, `40101` or `hr:20.3` (bit) addresses; int16 to
  uint64, float32/64, strings, bits; byte and word order per device; scale and offset.
  Tags are merged into as few block reads as possible (`tagbridge validate` shows the plan).
- **OPC UA**: subscriptions (default) or polling, username/password and security policies,
  data types detected from the server. `tagbridge browse` writes the tag list for you.
- **Sparkplug B**: each device becomes a Sparkplug device under one edge node. NBIRTH/DBIRTH with
  metric aliases, DDATA by exception, DDEATH when a PLC drops, NDEATH as MQTT will with `bdSeq`,
  `Node Control/Rebirth`, and DCMD writes. Payload encoder has no protobuf dependency.
- **Unified Namespace / plain MQTT**: retained `prefix/device/tag` topics with JSON
  `{"value", "timestamp", "quality", "unit"}` or raw values, online/offline status topics and
  optional `.../set` write topics. Use it alongside Sparkplug or on its own.
- **Report by exception** with per-tag deadband, reconnect with backoff for every device and the
  broker, last known values re-published on reconnect.
- **Operations**: `/healthz`, `/readyz`, Prometheus `/metrics` and a JSON `/api/tags` snapshot;
  `${ENV_VAR:-default}` substitution for secrets; non-root multi-arch image; Helm chart.

## Try it in one minute

```bash
git clone https://github.com/DanShinde/tagbridge && cd tagbridge
docker compose up
```

This starts Mosquitto, a simulated PLC (Modbus TCP on 5020 and OPC UA on 4840) and tagbridge with
[examples/demo.yaml](examples/demo.yaml). Point [MQTT Explorer](https://mqtt-explorer.com/) at
`localhost:1883`, or:

```bash
curl localhost:8080/api/tags                                   # live values
mosquitto_pub -t acme/plant1/press1/setpoint/set -m 65         # write a setpoint
```

## Install

```bash
pip install "tagbridge[all]"          # Modbus (incl. serial) + OPC UA
pip install "tagbridge[modbus]"       # Modbus TCP only
pip install "tagbridge[opcua]"        # OPC UA only
```

Or use the image: `docker run -v $PWD/tagbridge.yaml:/config/tagbridge.yaml ghcr.io/danshinde/tagbridge`.

## Configuration

```yaml
mqtt:
  host: broker.local
  port: 1883
  username: ${MQTT_USERNAME:-}
  password: ${MQTT_PASSWORD:-}
  # tls: {enabled: true, ca_file: /certs/ca.pem}

sparkplug:
  enabled: true
  group_id: Plant1
  edge_node_id: line1-gateway

uns:                      # optional plain-JSON topics
  enabled: true
  topic_prefix: acme/plant1
  allow_writes: false

devices:
  - name: press1
    type: modbus
    host: 192.168.1.20      # or serial_port: /dev/ttyUSB0 for RTU
    unit_id: 1
    scan_ms: 500
    word_order: big         # "little" for word-swapped 32-bit values
    tags:
      - { name: temperature, address: "hr:0", datatype: float32, unit: degC, deadband: 0.2 }
      - { name: pressure, address: "40003", datatype: uint16, scale: 0.1, unit: bar }
      - { name: alarms/overtemp, address: "hr:20.0" }
      - { name: running, address: "co:0", writable: true }

  - name: filler
    type: opcua
    endpoint: opc.tcp://192.168.1.30:4840
    mode: subscribe
    tags:
      - { name: speed, node: "ns=3;s=Filler.Speed", deadband: 0.5 }
      - { name: target, node: "ns=3;s=Filler.Target", writable: true }
```

| Tag field | Meaning |
|---|---|
| `name` | Metric / topic name. `/` builds a hierarchy (`alarms/overtemp`). |
| `address` / `node` | Modbus address or OPC UA node id. |
| `datatype` | `auto`, `bool`, `int16`, `uint16`, `int32`, `uint32`, `int64`, `uint64`, `float32`, `float64`, `string` |
| `length` | Characters, for Modbus strings. |
| `scale`, `offset` | Published value = raw × scale + offset (reversed for writes). |
| `deadband` | Absolute change needed before a new value is published. |
| `writable` | Accept writes from Sparkplug DCMD or `.../set` topics. |
| `unit`, `description`, `low`, `high` | Metadata. `unit` is included in UNS payloads. |

Modbus addresses: `co:`, `di:`, `hr:`, `ir:` (or `coil`, `discrete`, `holding`, `input`) take
zero-based offsets. Five and six digit Modicon numbers (`40001`, `300001`) are one-based.

Check a config and see the Modbus request plan without connecting:

```console
$ tagbridge validate examples/demo.yaml
examples/demo.yaml: OK
  mqtt: localhost:1883 -> Sparkplug B spBv1.0/Plant1/+/gateway-01, UNS acme/plant1/<device>/<tag>
  press1: modbus localhost, 6 tags, scan 1000 ms
    2 read request(s) per scan: co:0+1, hr:0+21
  line1: opcua opc.tcp://localhost:4840/tagbridge/demo, 4 tags, scan 500 ms
```

Read every tag once, which helps when commissioning a new device:

```console
$ tagbridge read examples/demo.yaml
press1/temperature              70.0
press1/pressure                 42.0
...
```

Generate tags from an OPC UA server:

```bash
tagbridge browse opc.tcp://192.168.1.30:4840 --name filler > filler.yaml
```

## Sparkplug B details

| Message | When |
|---|---|
| `NBIRTH` | On every MQTT connect and on `Node Control/Rebirth`. Carries `bdSeq`, `Node Control/Rebirth` and version info. |
| `DBIRTH` | When a device connects, after NBIRTH, and on rebirth. Every tag with name, alias, type and value. |
| `DDATA` | Changed tags only, by alias. Bad quality is sent as a null metric. |
| `DDEATH` | When a device stops answering. |
| `NDEATH` | Registered as the MQTT will (QoS 1) and published on clean shutdown. |
| `NCMD` / `DCMD` | Rebirth requests and writes to `writable` tags, by name or alias. |

Not in v0.1 yet: primary host `STATE` awareness, store and forward while the broker is down,
Sparkplug templates and datasets.

## Writes

Writes are off unless a tag is `writable: true`.

- Sparkplug: send a `DCMD` to `spBv1.0/<group>/DCMD/<edge node>/<device>` with the metric name
  or alias (`sparkplug.allow_writes: false` turns this off).
- UNS: publish to `<prefix>/<device>/<tag>/set` with `42`, `true`, `"text"` or
  `{"value": 42}` (needs `uns.allow_writes: true`).

## Kubernetes

```bash
helm install line1 oci://ghcr.io/danshinde/charts/tagbridge -f my-values.yaml
```

The chart mounts `config:` from your values as the config file, wires `/healthz` and `/readyz`
probes, runs as non-root with a read-only root filesystem, and can create a Prometheus
`ServiceMonitor`. See [deploy/helm/tagbridge/values.yaml](deploy/helm/tagbridge/values.yaml).

## Monitoring

`GET /metrics` exposes `tagbridge_device_online{device}`, `tagbridge_mqtt_connected`, and
counters for reads, read errors, changes, published messages and writes.

## Related projects

- [fakeplc](https://github.com/DanShinde/fakeplc): a scriptable fake PLC (Modbus TCP + OPC UA)
  for testing gateways and HMIs without hardware.
- [TagScope](https://github.com/DanShinde/tagscope): log OPC UA tags to a database and view them
  on a dashboard.

## Development

```bash
uv sync --all-extras
uv run pytest              # set TAGBRIDGE_TEST_MQTT=host:port to run the broker test
uv run ruff check . && uv run mypy
uv run tagbridge demo-server   # local Modbus + OPC UA simulator
```

Contributions are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT
