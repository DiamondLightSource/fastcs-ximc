"""A libximc device handle as a FastCS `Connection`."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, TypeVar

import libximc.highlevel as ximc
from fastcs.connections import Connection
from fastcs.logging import logger

from .config import XimcConnectionSettings
from .utils import check_device_present, patch_strict_flags, prepare_virtual_device

T = TypeVar("T")

READ_ONLY_GROUPS = (
    "position",
    "status",
    "device_information",
    "controller_name",
    "stage_name",
)
"""Groups read with ``get_<group>()`` rather than ``get_<group>_settings()``."""


class XimcConnection(Connection):
    """Serialised, non-blocking access to one libximc ``Axis``.

    Every libximc call is a blocking ctypes call over a serial link, so calls are
    dispatched to a worker thread. The device handle is not safe for concurrent
    use, so a lock serialises them.

    libximc raises `ConnectionError` when the link has gone and the device must
    be reopened, and `ValueError` when the device rejects a parameter, so `call`
    can tell a dead link from a device complaint and mark only the first down.

    Args:
        settings: Which device to open
        kwargs: Passed to `Connection` - ``depends_on``, ``reconnect_period``,
            ``reconnect_attempts``

    """

    def __init__(self, settings: XimcConnectionSettings, **kwargs) -> None:
        super().__init__(**kwargs)
        self._settings = settings
        self._axis: ximc.Axis | None = None
        self._lock = asyncio.Lock()

    @property
    def uri(self) -> str:
        """The resolved libximc URI of this device."""
        return self._settings.device_uri

    @property
    def is_open(self) -> bool:
        return self._axis is not None

    @property
    def axis(self) -> ximc.Axis:
        """The underlying libximc handle. Prefer `call` over using this directly."""
        if self._axis is None:
            raise ConnectionError(f"Device at '{self.uri}' is not open")
        return self._axis

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
        """Close the device if it is open.

        The handle is dropped before the close is attempted, so a dead link that
        will not close does not block the next attempt to open one.
        """
        if self._axis is None:
            return

        axis, self._axis = self._axis, None
        await asyncio.to_thread(axis.close_device)
        logger.info("Closed libximc device", uri=self.uri)

    async def call(self, func: Callable[[ximc.Axis], T]) -> T:
        """Run ``func`` against the device handle in a worker thread.

        Usage::

            position = await connection.call(lambda axis: axis.get_position())
        """
        async with self._lock:
            try:
                return await asyncio.to_thread(func, self.axis)
            except OSError:
                # libximc's "reopen the device" is a `ConnectionError`, and so an
                # `OSError`. The transport is gone, rather than the device
                # complaining, so everything holding this connection is now down.
                self.set_disconnected()
                raise

    async def read_field(self, group: str, field: str) -> Any:
        """Read one field of a settings/state struct."""
        return await self.call(lambda axis: getattr(_get_struct(axis, group), field))

    async def write_field(self, group: str, field: str, value: Any) -> None:
        """Read-modify-write one field of a settings struct.

        libximc rejects a partially populated struct, so the whole struct must
        be read back, mutated and written out again.
        """

        def _write(axis: ximc.Axis) -> None:
            struct = _get_struct(axis, group)
            setattr(struct, field, value)
            getattr(axis, f"set_{group}_settings")(struct)

        await self.call(_write)

    async def write_bit(self, group: str, field: str, mask: int, value: bool) -> None:
        """Read-modify-write a single bit of a bitmask field of a settings struct."""

        def _write(axis: ximc.Axis) -> None:
            struct = _get_struct(axis, group)
            raw = int(getattr(struct, field))
            setattr(struct, field, raw | mask if value else raw & ~mask)
            getattr(axis, f"set_{group}_settings")(struct)

        await self.call(_write)


def _get_struct(axis: ximc.Axis, group: str) -> Any:
    if group in READ_ONLY_GROUPS:
        return getattr(axis, f"get_{group}")()
    return getattr(axis, f"get_{group}_settings")()
