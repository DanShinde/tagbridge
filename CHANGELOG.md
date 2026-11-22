# Changelog

## Unreleased

- Ship a Grafana dashboard for `/metrics` at `deploy/grafana/tagbridge-dashboard.json` (device
  online, MQTT connected, read/read-error/connect-error rates, published messages, writes, drops),
  with a `${DS_PROMETHEUS}` datasource input and a `device` template variable. The Helm chart can
  ship it as a sidecar ConfigMap via `grafanaDashboard.enabled: true`.
- `tests/test_grafana_dashboard.py` keeps the dashboard's metric names in sync with
  `render_metrics`.

## 0.1.1 (2026-11-22)

- New `tagbridge watch` command: prints MQTT traffic and decodes Sparkplug B payloads, resolving
  aliases from births.
- Fix: a device that came online while the MQTT session was starting could get two DBIRTHs.
- Fix: `demo-server` now closes client connections and exits promptly on shutdown.
- CI checks Sparkplug output with the Eclipse Tahu reference decoder (`interop/tahu`).
- Demo GIF and Unified Namespace example in the README; more PyPI keywords.

## 0.1.0 (2026-11-22)

First release.

- Modbus TCP/RTU source with block-optimised reads, bit addressing, 16/32/64-bit integers,
  floats and strings, byte and word order, scale and offset.
- OPC UA source with subscriptions or polling, authentication and security strings.
- Sparkplug B edge node: NBIRTH/DBIRTH with aliases, DDATA by exception, DDEATH, NDEATH will with
  bdSeq, rebirth, DCMD writes.
- Unified Namespace JSON topics with status topics and optional `/set` writes.
- Per-tag deadband, reconnect with backoff, clean shutdown messages.
- `run`, `validate`, `read`, `browse` and `demo-server` commands.
- HTTP `/healthz`, `/readyz`, `/metrics` and `/api/tags`.
- Docker image, docker compose demo and Helm chart.
