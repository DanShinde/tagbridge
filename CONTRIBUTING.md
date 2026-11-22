# Contributing

Thanks for helping. Bug reports with a config snippet and the device or server you are talking to
are the most useful thing you can send.

## Setup

```bash
uv sync --all-extras
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

The end-to-end test needs an MQTT broker:

```bash
docker run -d -p 1883:1883 eclipse-mosquitto:1.6
TAGBRIDGE_TEST_MQTT=localhost:1883 uv run pytest tests/test_integration.py
```

`uv run tagbridge demo-server` starts a local Modbus TCP (5020) and OPC UA (4840) simulator that
works with `examples/demo.yaml`.

## Adding a driver

Drivers live in `src/tagbridge/sources/` and implement `Source` (`connect`, `read`, `write`,
`close`). `read` returns raw values for every tag; scaling, deadband and publishing are handled by
the engine. Add a config model in `config.py` and register it in `sources/__init__.py`.

## Pull requests

Keep changes focused, add tests, and update `CHANGELOG.md`.
