"""Base class for device drivers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..config import TagConfig
from ..model import Reading


class SourceError(Exception):
    """A device could not be reached or returned an error."""


class Source(ABC):
    """A connection to one device that can read all configured tags and write single tags.

    Values returned by :meth:`read` are raw device values; scaling is applied by the engine.
    """

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def read(self) -> dict[str, Reading]:
        """Read every configured tag. Raise :class:`SourceError` if the device is unreachable."""

    @abstractmethod
    async def write(self, tag: TagConfig, value: Any) -> None: ...
