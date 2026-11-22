"""Command line interface."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import signal
import sys
from typing import Any

import yaml

from . import __version__
from .config import ConfigError, GatewayConfig, ModbusDeviceConfig, load_config
from .sources.base import SourceError

log = logging.getLogger("tagbridge")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    for noisy in ("asyncua", "pymodbus"):
        logging.getLogger(noisy).setLevel(max(logging.ERROR, getattr(logging, level)))


def _load(path: str) -> GatewayConfig:
    try:
        return load_config(path)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


async def _run(config: GatewayConfig) -> None:
    from .engine import Gateway
    from .http import serve
    from .sinks.mqtt import MqttBridge

    gateway = Gateway(config)
    bridge = MqttBridge(config, gateway)
    extra: list[Any] = [bridge.run]
    if config.http.enabled:
        extra.append(lambda: serve(gateway, config.http.host, config.http.port))
    log.info(
        "tagbridge %s starting: %d device(s), sparkplug=%s, uns=%s",
        __version__,
        len(gateway.devices),
        config.sparkplug.enabled,
        config.uns.enabled,
    )
    await gateway.run(extra)


def _run_forever(coro: Any) -> None:
    async def main() -> None:
        task = asyncio.ensure_future(coro)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, task.cancel)
        with contextlib.suppress(asyncio.CancelledError):
            await task

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())


def cmd_run(args: argparse.Namespace) -> int:
    config = _load(args.config)
    _setup_logging(args.log_level or config.log_level)
    _run_forever(_run(config))
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    config = _load(args.config)
    from .sources import create_source

    print(f"{args.config}: OK")
    targets = []
    if config.sparkplug.enabled:
        sp = config.sparkplug
        targets.append(f"Sparkplug B spBv1.0/{sp.group_id}/+/{sp.edge_node_id}")
    if config.uns.enabled:
        targets.append(f"UNS {config.uns.topic_prefix}/<device>/<tag>")
    print(f"  mqtt: {config.mqtt.host}:{config.mqtt.port} -> {', '.join(targets)}")
    for device in config.devices:
        try:
            source = create_source(device)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        state = "" if device.enabled else " (disabled)"
        target = (
            (device.host or device.serial_port)
            if isinstance(device, ModbusDeviceConfig)
            else device.endpoint
        )
        print(
            f"  {device.name}{state}: {device.type} {target}, {len(device.tags)} tags, "
            f"scan {device.scan_ms} ms"
        )
        blocks = getattr(source, "blocks", None)
        if blocks:
            print(
                f"    {len(blocks)} read request(s) per scan: "
                + ", ".join(f"{b.area}:{b.start}+{b.count}" for b in blocks)
            )
    return 0


async def _read_once(config: GatewayConfig, only: str | None) -> list[tuple[str, str, Any, bool]]:
    from .model import apply_scaling
    from .sources import create_source

    rows: list[tuple[str, str, Any, bool]] = []
    for device in config.active_devices:
        if only and device.name != only:
            continue
        source = create_source(device)
        try:
            await source.connect()
            readings = await source.read()
        except SourceError as exc:
            rows.append((device.name, "<error>", str(exc), False))
            continue
        finally:
            await source.close()
        for tag in device.tags:
            r = readings.get(tag.name)
            if r is None:
                rows.append((device.name, tag.name, None, False))
            else:
                rows.append((device.name, tag.name, apply_scaling(tag, r.value), r.good))
    return rows


def cmd_read(args: argparse.Namespace) -> int:
    config = _load(args.config)
    _setup_logging("WARNING")
    rows = asyncio.run(_read_once(config, args.device))
    if args.json:
        print(
            json.dumps(
                [dict(zip(("device", "tag", "value", "good"), r, strict=True)) for r in rows],
                indent=2,
                default=str,
            )
        )
    else:
        width = max([len(f"{d}/{t}") for d, t, _, _ in rows] + [4])
        for device, tag, value, good in rows:
            flag = "" if good else "  (bad)"
            print(f"{device + '/' + tag:<{width}}  {value!r}{flag}")
    return 0 if all(r[3] for r in rows) else 1


def cmd_browse(args: argparse.Namespace) -> int:
    from .browse import browse_opcua

    _setup_logging("WARNING")
    try:
        tags = asyncio.run(
            browse_opcua(args.endpoint, args.root, args.depth, args.username, args.password)
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    device = {
        "name": args.name,
        "type": "opcua",
        "endpoint": args.endpoint,
        "tags": tags,
    }
    print(f"# {len(tags)} variables found under {args.root}")
    print(yaml.safe_dump({"devices": [device]}, sort_keys=False, allow_unicode=True), end="")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    from .demo import run_demo

    _setup_logging("INFO")
    _run_forever(
        run_demo(args.host, args.modbus_port or None, args.opcua_port or None, args.interval)
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tagbridge",
        description="Modbus and OPC UA to MQTT gateway with Sparkplug B support.",
    )
    parser.add_argument("--version", action="version", version=f"tagbridge {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the gateway")
    run.add_argument("config", help="path to the YAML config file")
    run.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    run.set_defaults(func=cmd_run)

    validate = sub.add_parser("validate", help="check a config file and show the read plan")
    validate.add_argument("config")
    validate.set_defaults(func=cmd_validate)

    read = sub.add_parser("read", help="read every tag once and print the values")
    read.add_argument("config")
    read.add_argument("--device", help="only read this device")
    read.add_argument("--json", action="store_true", help="print JSON")
    read.set_defaults(func=cmd_read)

    browse = sub.add_parser("browse", help="browse an OPC UA server and print a device config")
    browse.add_argument("endpoint", help="for example opc.tcp://localhost:4840")
    browse.add_argument("--root", default="i=85", help="node to start from (default Objects)")
    browse.add_argument("--depth", type=int, default=6)
    browse.add_argument("--name", default="opcua1", help="device name to use in the output")
    browse.add_argument("--username")
    browse.add_argument("--password")
    browse.set_defaults(func=cmd_browse)

    demo = sub.add_parser("demo-server", help="start a demo Modbus TCP and OPC UA server")
    demo.add_argument("--host", default="0.0.0.0")
    demo.add_argument("--modbus-port", type=int, default=5020, help="0 to disable")
    demo.add_argument("--opcua-port", type=int, default=4840, help="0 to disable")
    demo.add_argument("--interval", type=float, default=1.0)
    demo.set_defaults(func=cmd_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
