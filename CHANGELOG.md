# Changelog

## 0.1.0 (unreleased)

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
