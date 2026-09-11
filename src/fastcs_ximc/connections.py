"""The libximc device handle as `Logic` given to a `DeviceConnection`.

The composition spelling of `XimcConnection`: everything libximc lives in a
`XimcLogic` with no framework base class, and a generic connection holds one.
`DeviceConnection` stands in for the concrete, non-abstract `Connection` this
would need from FastCS, whose own `Connection` is still an ABC.
"""

from __future__ import annotations

import asyncio
from typing import Any, Generic, Protocol, TypeVar

import libximc.highlevel as ximc
from fastcs.connections import Connection, DRANode
from fastcs.logging import logger

from .config import XimcConnectionSettings
from .utils import check_device_present, patch_strict_flags, prepare_virtual_device

READ_ONLY_GROUPS = ("position", "status", "device_information")
"""Groups read with ``get_<group>()`` rather than ``get_<group>_settings()``."""


class Logic(Protocol):
    """What a `DeviceConnection` needs of the thing that knows the device.

    The framework half of the split: a connection can open, close and name
    whatever it is given, and knows nothing else about it.
    """

    connection: Connection | None
    """Set by the connection that holds it, so IO can report a dead link."""

    @property
    def label(self) -> str: ...

    async def open(self) -> None: ...

    async def close(self) -> None: ...


LogicT = TypeVar("LogicT", bound=Logic)


class DeviceConnection(Connection, Generic[LogicT]):
    """A connection that owns no device knowledge: it holds a `Logic`.

    Stands in for the concrete, non-abstract `Connection` this spelling needs
    from FastCS. It owns the health state, the retry settings and the recovery
    policy; the logic owns the handle and the protocol.
    """

    def __init__(self, logic: LogicT, **kwargs) -> None:
        super().__init__(**kwargs)
        self.logic = logic
        # The back-reference the split needs: failure is detected in the IO,
        # which now lives in an object that does not own the health state.
        logic.connection = self

    @property
    def label(self) -> str:
        return self.logic.label

    async def connect(self) -> None:
        await self.logic.open()

    async def close(self) -> None:
        await self.logic.close()


class XimcLogic:
    """Serialised, non-blocking access to one libximc ``Axis``.

    libximc calls block, and the handle is not safe for concurrent use, so they
    go to a worker thread one at a time. Opening and reopening it is the
    runner's job, through the connection holding this.
    """

    def __init__(self, settings: XimcConnectionSettings) -> None:
        self._settings = settings
        self._axis: ximc.Axis | None = None
        self._lock = asyncio.Lock()
        self.connection: Connection | None = None

    @property
    def uri(self) -> str:
        return self._settings.device_uri

    @property
    def is_open(self) -> bool:
        return self._axis is not None

    @property
    def label(self) -> str:
        """The device node this addresses, to name it in a failure."""
        return self.uri.split("://", 1)[1]

    async def open(self) -> None:
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
            # be reopened, and ValueError when it rejects a parameter. Reported
            # through the connection, which owns the health state.
            if self.connection is not None:
                self.connection.set_disconnected()
            raise


class XimcConnection(DeviceConnection[XimcLogic]):
    """A libximc device. Carries the config shape and the logic type, nothing else."""

    def __init__(self, settings: XimcConnectionSettings, **kwargs) -> None:
        super().__init__(XimcLogic(settings), **kwargs)


class XimcDRAConnection(XimcConnection):
    """A XIMC device whose node comes from a Kubernetes DRA claim.

    The claim names the node at runtime, so the only thing that can be
    configured is the variable holding it - hence ``port_env`` rather than the
    settings its parent takes. Subclassed for that config shape alone: the
    behaviour is `DRANode`, which it holds.
    """

    recovery = DRANode()

    def __init__(self, port_env: str, **kwargs) -> None:
        super().__init__(XimcConnectionSettings(port_env=port_env), **kwargs)
