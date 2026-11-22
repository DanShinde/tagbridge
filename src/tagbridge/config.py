"""Configuration models and YAML loading."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

DataType = Literal[
    "auto",
    "bool",
    "int16",
    "uint16",
    "int32",
    "uint32",
    "int64",
    "uint64",
    "float32",
    "float64",
    "string",
]

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")
_NAME_PATTERN = re.compile(r"^[^/+#\s][^+#]*$")


class ConfigError(ValueError):
    """Raised when a configuration file cannot be loaded or is invalid."""


def substitute_env(text: str, env: dict[str, str] | None = None) -> str:
    """Replace ``${VAR}`` and ``${VAR:-default}`` with environment values."""
    source = os.environ if env is None else env

    def replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        if name in source and source[name] != "":
            return source[name]
        if default is not None:
            return default
        raise ConfigError(f"environment variable {name} is not set and has no default")

    return _ENV_PATTERN.sub(replace, text)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class TlsConfig(_Model):
    enabled: bool = False
    ca_file: str | None = None
    cert_file: str | None = None
    key_file: str | None = None
    insecure: bool = False


class MqttConfig(_Model):
    host: str = "localhost"
    port: int = 1883
    username: str | None = None
    password: str | None = None
    client_id: str | None = None
    keepalive: int = Field(default=30, ge=5)
    tls: TlsConfig = TlsConfig()
    qos: Literal[0, 1] = 0
    reconnect_min_s: float = Field(default=1.0, gt=0)
    reconnect_max_s: float = Field(default=30.0, gt=0)

    @field_validator("username", "password", "client_id", mode="before")
    @classmethod
    def _empty_is_none(cls, value: object) -> object:
        return None if value == "" else value


class SparkplugConfig(_Model):
    enabled: bool = True
    group_id: str = "tagbridge"
    edge_node_id: str = "edge-01"
    use_aliases: bool = True
    allow_writes: bool = True

    @field_validator("group_id", "edge_node_id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        if not value or any(c in value for c in "/+#"):
            raise ValueError("Sparkplug ids must be non-empty and must not contain / + or #")
        return value


class UnsConfig(_Model):
    enabled: bool = False
    topic_prefix: str = "tagbridge"
    payload: Literal["json", "value"] = "json"
    retain: bool = True
    allow_writes: bool = False

    @field_validator("topic_prefix")
    @classmethod
    def _valid_prefix(cls, value: str) -> str:
        value = value.strip("/")
        if not value or "+" in value or "#" in value:
            raise ValueError("topic_prefix must be non-empty and must not contain + or #")
        return value


class HttpConfig(_Model):
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8080


class TagConfig(_Model):
    name: str
    address: str = Field(validation_alias=AliasChoices("address", "node"))
    datatype: DataType = "auto"
    length: int | None = Field(default=None, ge=1, description="String length in characters")
    scale: float | None = None
    offset: float | None = None
    deadband: float | None = Field(default=None, ge=0)
    writable: bool = False
    unit: str | None = None
    description: str | None = None
    low: float | None = None
    high: float | None = None

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        value = value.strip()
        if not _NAME_PATTERN.match(value) or value.endswith("/"):
            raise ValueError(
                f"invalid tag name {value!r}: use letters, digits, _ . - and / as a separator"
            )
        return value


class _DeviceBase(_Model):
    name: str
    enabled: bool = True
    scan_ms: int = Field(default=1000, ge=10)
    reconnect_min_s: float = Field(default=1.0, gt=0)
    reconnect_max_s: float = Field(default=30.0, gt=0)
    tags: list[TagConfig] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not value or any(c in value for c in "/+#"):
            raise ValueError("device names must be non-empty and must not contain / + or #")
        return value

    @model_validator(mode="after")
    def _unique_tags(self) -> _DeviceBase:
        seen: set[str] = set()
        for tag in self.tags:
            if tag.name in seen:
                raise ValueError(f"duplicate tag name {tag.name!r} in device {self.name!r}")
            seen.add(tag.name)
        return self


class ModbusDeviceConfig(_DeviceBase):
    type: Literal["modbus"]
    host: str | None = None
    port: int = 502
    serial_port: str | None = None
    baudrate: int = 9600
    parity: Literal["N", "E", "O"] = "N"
    stopbits: Literal[1, 2] = 1
    unit_id: int = Field(default=1, ge=0, le=255, validation_alias=AliasChoices("unit_id", "slave"))
    timeout_s: float = Field(default=3.0, gt=0)
    byte_order: Literal["big", "little"] = "big"
    word_order: Literal["big", "little"] = "big"
    max_registers_per_read: int = Field(default=100, ge=1, le=125)
    max_gap: int = Field(default=10, ge=0, description="Registers to read across to merge blocks")

    @model_validator(mode="after")
    def _transport(self) -> ModbusDeviceConfig:
        if bool(self.host) == bool(self.serial_port):
            raise ValueError(f"modbus device {self.name!r}: set exactly one of host or serial_port")
        return self


class OpcUaDeviceConfig(_DeviceBase):
    type: Literal["opcua"]
    endpoint: str
    username: str | None = None
    password: str | None = None
    security: str | None = Field(
        default=None,
        description="asyncua security string, e.g. Basic256Sha256,SignAndEncrypt,cert.der,key.pem",
    )
    mode: Literal["subscribe", "poll"] = "subscribe"
    timeout_s: float = Field(default=4.0, gt=0)


DeviceConfig = Annotated[ModbusDeviceConfig | OpcUaDeviceConfig, Field(discriminator="type")]


class GatewayConfig(_Model):
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    mqtt: MqttConfig = MqttConfig()
    sparkplug: SparkplugConfig = SparkplugConfig()
    uns: UnsConfig = UnsConfig()
    http: HttpConfig = HttpConfig()
    devices: list[DeviceConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _checks(self) -> GatewayConfig:
        if not self.sparkplug.enabled and not self.uns.enabled:
            raise ValueError("enable at least one of sparkplug or uns")
        names = [d.name for d in self.devices]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate device names: {', '.join(sorted(dupes))}")
        return self

    @property
    def active_devices(self) -> list[ModbusDeviceConfig | OpcUaDeviceConfig]:
        return [d for d in self.devices if d.enabled]


def parse_config(data: dict[str, Any]) -> GatewayConfig:
    try:
        return GatewayConfig.model_validate(data)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def load_config(path: str | Path, env: dict[str, str] | None = None) -> GatewayConfig:
    """Load a YAML config file, substituting environment variables first."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        data = yaml.safe_load(substitute_env(text, env)) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    return parse_config(data)
