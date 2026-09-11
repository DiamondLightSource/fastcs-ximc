"""A libximc device handle as a FastCS `Connection`."""

from __future__ import annotations

import asyncio
from typing import Any

import libximc.highlevel as ximc
from fastcs.connections import Connection
from fastcs.logging import logger

from .config import XimcConnectionSettings
from .utils import check_device_present, patch_strict_flags, prepare_virtual_device

READ_ONLY_GROUPS = ("position", "status", "device_information")
"""Groups read with ``get_<group>()`` rather than ``get_<group>_settings()``."""


class XimcConnection(Connection):
    """Serialised, non-blocking access to one libximc ``Axis``.

    Every libximc call is a blocking ctypes call over a serial link, so calls
    are dispatched to a worker thread. The device handle is not safe for
    concurrent use, so a lock serialises them.

    Opening, reopening and closing it are the runner's job, so there is no
    reconnect logic here - only the report that the link has gone.
    """

    def __init__(self, settings: XimcConnectionSettings, **kwargs) -> None:
        super().__init__(**kwargs)
        self._settings = settings
        self._axis: ximc.Axis | None = None
        self._lock = asyncio.Lock()

    @property
    def uri(self) -> str:
        return self._settings.device_uri

    @property
    def is_open(self) -> bool:
        return self._axis is not None

    async def connect(self) -> None:
        """Open the device, creating virtual device state files if needed."""
        # Real hardware reports bits libximc's strict Flag enums reject, which
        # would make every get_*_settings call raise. Patch before opening.
        patch_strict_flags()

        prepare_virtual_device(self.uri)
        check_device_present(self.uri)

        axis = ximc.Axis(self.uri)
        await asyncio.to_thread(axis.open_device)
        self._axis = axis
        logger.info("Opened libximc device", uri=self.uri)

    async def close(self) -> None:
        """Close the device if it is open, dropping the handle either way."""
        if self._axis is None:
            return

        axis, self._axis = self._axis, None
        await asyncio.to_thread(axis.close_device)
        logger.info("Closed libximc device", uri=self.uri)

    async def read_struct(self, group: str) -> Any:
        """Read a whole settings or state struct."""
        async with self._lock:
            suffix = "" if group in READ_ONLY_GROUPS else "_settings"
            return await self._call(f"get_{group}{suffix}")

    async def read(self, group: str, field: str) -> Any:
        """Read one field of a settings or state struct."""
        return getattr(await self.read_struct(group), field)

    async def write(
        self, group: str, field: str, value: Any, bit: int | None = None
    ) -> None:
        """Write one field of a settings struct, or one bit of one.

        libximc rejects a partially populated struct, so the whole struct is
        read back, mutated and written out again, under one lock.
        """
        async with self._lock:
            struct = await self._call(f"get_{group}_settings")
            if bit is not None:
                raw = int(getattr(struct, field))
                value = raw | bit if value else raw & ~bit
            setattr(struct, field, value)
            await self._call(f"set_{group}_settings", struct)

    async def command(self, name: str, *args: Any) -> None:
        """Send a libximc ``command_<name>``, e.g. ``command("move", 100, 0)``."""
        async with self._lock:
            await self._call(f"command_{name}", *args)

    async def _call(self, method: str, *args: Any) -> Any:
        """Call one method of the handle in a worker thread. Lock held by caller."""
        if self._axis is None:
            raise ConnectionError(f"Device at '{self.uri}' is not open")

        try:
            return await asyncio.to_thread(getattr(self._axis, method), *args)
        except OSError:
            # libximc raises ConnectionError - an OSError - when the device must
            # be reopened, and ValueError when it rejects a parameter.
            self.set_disconnected()
            raise
