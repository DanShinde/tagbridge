"""Device drivers."""

from __future__ import annotations

from ..config import ModbusDeviceConfig, OpcUaDeviceConfig
from .base import Source


def create_source(config: ModbusDeviceConfig | OpcUaDeviceConfig) -> Source:
    if isinstance(config, ModbusDeviceConfig):
        from .modbus import ModbusSource

        return ModbusSource(config)
    from .opcua import OpcUaSource

    return OpcUaSource(config)


__all__ = ["Source", "create_source"]
