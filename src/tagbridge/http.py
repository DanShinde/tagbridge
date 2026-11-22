"""Tiny HTTP server for health checks, Prometheus metrics and a JSON tag snapshot."""

from __future__ import annotations

import asyncio
import json
import logging

from . import __version__
from .engine import Gateway

log = logging.getLogger(__name__)


def render_metrics(gateway: Gateway) -> str:
    lines = [
        "# HELP tagbridge_info Gateway version.",
        "# TYPE tagbridge_info gauge",
        f'tagbridge_info{{version="{__version__}"}} 1',
        "# HELP tagbridge_mqtt_connected 1 when connected to the MQTT broker.",
        "# TYPE tagbridge_mqtt_connected gauge",
        f"tagbridge_mqtt_connected {int(gateway.mqtt_connected)}",
        "# HELP tagbridge_device_online 1 when the device is connected and reading.",
        "# TYPE tagbridge_device_online gauge",
    ]
    for name, state in gateway.devices.items():
        lines.append(f'tagbridge_device_online{{device="{_escape(name)}"}} {int(state.online)}')
    counters = {
        "reads": "Successful device scans.",
        "read_errors": "Failed device scans.",
        "connect_errors": "Failed device connection attempts.",
        "changes": "Tag value changes detected.",
        "published": "MQTT messages published.",
        "writes": "Tag writes performed.",
        "write_errors": "Tag writes that failed.",
        "dropped": "Events dropped because the publish queue was full.",
    }
    for key, help_text in counters.items():
        lines += [
            f"# HELP tagbridge_{key}_total {help_text}",
            f"# TYPE tagbridge_{key}_total counter",
            f"tagbridge_{key}_total {gateway.stats.get(key, 0)}",
        ]
    return "\n".join(lines) + "\n"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def snapshot(gateway: Gateway) -> dict[str, object]:
    return {
        "version": __version__,
        "mqtt_connected": gateway.mqtt_connected,
        "devices": {
            name: {
                "online": state.online,
                "last_error": state.last_error,
                "tags": {
                    tag: {"value": r.value, "timestamp": r.timestamp, "good": r.good}
                    for tag, r in state.values.items()
                },
            }
            for name, state in gateway.devices.items()
        },
    }


async def serve(gateway: Gateway, host: str, port: int) -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await asyncio.wait_for(reader.readline(), timeout=5)
            while (await asyncio.wait_for(reader.readline(), timeout=5)) not in (b"\r\n", b""):
                pass
            parts = request.decode("latin-1").split()
            path = parts[1].split("?")[0] if len(parts) > 1 else "/"
            status, ctype, body = _route(gateway, path)
            head = (
                f"HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
            )
            writer.write(head.encode("latin-1") + body)
            await writer.drain()
        except (asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, host, port)
    log.info("HTTP endpoints on http://%s:%s (/healthz, /metrics, /api/tags)", host, port)
    async with server:
        await server.serve_forever()


def _route(gateway: Gateway, path: str) -> tuple[str, str, bytes]:
    if path in ("/healthz", "/livez"):
        return "200 OK", "text/plain", b"ok\n"
    if path == "/readyz":
        if gateway.mqtt_connected:
            return "200 OK", "text/plain", b"ready\n"
        return "503 Service Unavailable", "text/plain", b"mqtt not connected\n"
    if path == "/metrics":
        return "200 OK", "text/plain; version=0.0.4", render_metrics(gateway).encode()
    if path in ("/api/tags", "/"):
        body = json.dumps(snapshot(gateway), indent=2, default=str).encode()
        return "200 OK", "application/json", body
    return "404 Not Found", "text/plain", b"not found\n"
