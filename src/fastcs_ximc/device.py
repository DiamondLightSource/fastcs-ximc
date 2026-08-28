"""Async wrapper around a single libximc device handle."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any, TypeVar

import libximc.highlevel as ximc

from .utils import check_device_present, patch_strict_flags, prepare_virtual_device

logger = logging.getLogger(__name__)

T = TypeVar("T")


class XimcDevice:
    """Serialised, non-blocking access to one libximc ``Axis``.

    Every libximc call is a blocking ctypes call over a serial link, so calls
    are dispatched to a worker thread. The device handle is not safe for
    concurrent use, so a lock serialises them.
    """

    def __init__(self, uri: str) -> None:
        self._uri = uri
        self._axis: ximc.Axis | None = None
        self._lock = asyncio.Lock()

    @property
    def uri(self) -> str:
        return self._uri

    @property
    def is_open(self) -> bool:
        return self._axis is not None

    @property
    def axis(self) -> ximc.Axis:
        """The underlying libximc handle. Prefer `call` over using this directly."""
        if self._axis is None:
            raise RuntimeError(f"Device at '{self._uri}' is not open")
        return self._axis

    async def open(self) -> None:
        """Open the device, creating virtual device state files if needed."""
        if self._axis is not None:
            return

        # Real hardware reports bits libximc's strict Flag enums reject, which
        # would make every get_*_settings call raise. Patch before opening.
        patch_strict_flags()

        prepare_virtual_device(self._uri)
        check_device_present(self._uri)

        axis = ximc.Axis(self._uri)
        await asyncio.to_thread(axis.open_device)
        self._axis = axis
        logger.info("Opened libximc device at %s", self._uri)

    async def close(self) -> None:
        """Close the device if it is open."""
        if self._axis is None:
            return

        axis, self._axis = self._axis, None
        await asyncio.to_thread(axis.close_device)
        logger.info("Closed libximc device at %s", self._uri)

    async def call(self, func: Callable[[ximc.Axis], T]) -> T:
        """Run ``func`` against the device handle in a worker thread.

        Usage::

            position = await device.call(lambda axis: axis.get_position())
        """
        async with self._lock:
            return await asyncio.to_thread(func, self.axis)

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


READ_ONLY_GROUPS = (
    "position",
    "status",
    "device_information",
    "controller_name",
    "stage_name",
)
"""Groups read with ``get_<group>()`` rather than ``get_<group>_settings()``."""


def _get_struct(axis: ximc.Axis, group: str) -> Any:
    if group in READ_ONLY_GROUPS:
        return getattr(axis, f"get_{group}")()
    return getattr(axis, f"get_{group}_settings")()
