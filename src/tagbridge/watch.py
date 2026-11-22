"""Print MQTT traffic, decoding Sparkplug B payloads."""

from __future__ import annotations

import datetime as dt
import sys
from typing import Any, TextIO

import aiomqtt

from .sparkplug import NAMESPACE, Payload, decode_payload

_COLORS = {
    "NBIRTH": "\033[1;32m",
    "DBIRTH": "\033[32m",
    "NDEATH": "\033[1;31m",
    "DDEATH": "\033[31m",
    "DDATA": "\033[36m",
    "NDATA": "\033[36m",
    "NCMD": "\033[1;35m",
    "DCMD": "\033[35m",
}
_RESET = "\033[0m"
_DIM = "\033[2m"


def _value(metric: Any) -> str:
    if metric.is_null:
        return "null"
    if isinstance(metric.value, float):
        return f"{metric.value:.6g}"
    return repr(metric.value) if isinstance(metric.value, str) else str(metric.value)


def format_sparkplug(topic: str, payload: Payload, color: bool) -> list[str]:
    parts = topic.split("/")
    kind = parts[2] if len(parts) > 2 else "?"
    where = "/".join(parts[3:])
    start, end = (_COLORS.get(kind, ""), _RESET) if color else ("", "")
    seq = "" if payload.seq is None else f" seq={payload.seq}"
    lines = [f"{start}{kind:<6}{end} {where}{seq}"]
    for metric in payload.metrics:
        label = metric.name if metric.name is not None else f"#{metric.alias}"
        alias = f" #{metric.alias}" if metric.name is not None and metric.alias is not None else ""
        dtype = f"{_DIM}{metric.datatype.name.lower()}{_RESET}" if color else metric.datatype.name
        lines.append(f"         {label}{alias} = {_value(metric)}  {dtype}")
    return lines


async def watch(
    host: str,
    port: int,
    topics: list[str],
    username: str | None = None,
    password: str | None = None,
    out: TextIO = sys.stdout,
    color: bool | None = None,
    limit: int | None = None,
) -> None:
    use_color = out.isatty() if color is None else color
    names: dict[tuple[str, int], str] = {}
    count = 0
    async with aiomqtt.Client(host, port, username=username, password=password) as client:
        for topic in topics:
            await client.subscribe(topic)
        async for message in client.messages:
            topic = message.topic.value
            raw = message.payload if isinstance(message.payload, bytes) else b""
            stamp = dt.datetime.now().strftime("%H:%M:%S")
            prefix = f"{_DIM}{stamp}{_RESET} " if use_color else f"{stamp} "
            if topic.startswith(NAMESPACE + "/") and "/STATE/" not in topic:
                try:
                    payload = decode_payload(raw)
                except ValueError as exc:
                    print(f"{prefix}{topic}: undecodable payload ({exc})", file=out)
                    continue
                parts = topic.split("/")
                device = "/".join(parts[3:])
                for metric in payload.metrics:
                    if metric.name is not None and metric.alias is not None:
                        names[(device, metric.alias)] = metric.name
                    elif metric.name is None and metric.alias is not None:
                        metric.name = names.get((device, metric.alias))
                lines = format_sparkplug(topic, payload, use_color)
                print(prefix + "\n".join(lines), file=out, flush=True)
            else:
                text = raw.decode("utf-8", "replace")
                flag = " (retained)" if message.retain else ""
                print(f"{prefix}{topic} {text}{flag}", file=out, flush=True)
            count += 1
            if limit and count >= limit:
                return
