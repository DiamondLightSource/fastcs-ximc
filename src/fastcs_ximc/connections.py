"""The libximc device handle as FastCS `Connection` s."""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

import libximc.highlevel as ximc
from fastcs.connections import Connection
from fastcs.logging import logger

from .config import XimcConnectionSettings
from .utils import check_device_present, patch_strict_flags, prepare_virtual_device

READ_ONLY_GROUPS = ("position", "status", "device_information")
"""Groups read with ``get_<group>()`` rather than ``get_<group>_settings()``."""


# These move to `fastcs.connections` when the recovery PR lands; delete them
# here and import them instead. Nothing else changes: the names, the signatures
# and the semantics are the ones that PR provides.


class Labelled(Protocol):
    """All a policy needs of a connection: something to call it in a message."""

    @property
    def label(self) -> str: ...


class Recovery:
    """Whether a connection failure is worth retrying. The default: always.

    Held by a connection rather than inherited into one, so the transport and
    what to do when it fails are chosen separately - one policy can be given to
    any connection, and a connection can be given any policy. Stateless, so one
    instance is shared by every connection that uses it.
    """

    is_fatal: bool = False
    """Whether a terminal failure should bring the application down."""

    def is_terminal(self, exc: BaseException) -> bool:
        """Whether a failed `Connection.connect` can never succeed in this process."""
        return False

    def reason(self, connection: Labelled) -> str:
        """Why it cannot recover, for the log line that ends the retry loop."""
        return f"{connection.label} cannot recover from this failure."


class DRANode(Recovery):
    """A device node injected by a Kubernetes DRA claim.

    The claim is established when the pod starts, so a node that has gone will
    not reappear in it. Retrying cannot help and a restart can, so this is both
    terminal and fatal.
    """

    is_fatal = True

    def is_terminal(self, exc: BaseException) -> bool:
        return isinstance(exc, FileNotFoundError)

    def reason(self, connection: Labelled) -> str:
        return (
            f"Device node {connection.label} has gone away. It comes from a "
            "Kubernetes DRA claim and will not reappear in this pod. Restart "
            "the pod to re-establish the claim."
        )


class XimcConnection(Connection):
    """Serialised, non-blocking access to one libximc ``Axis``.

    libximc calls block, and the handle is not safe for concurrent use, so they
    go to a worker thread one at a time. Opening and reopening it is the
    runner's job.
    """

    recovery: Recovery = Recovery()
    """What to do when this connection fails. Assign one to change it."""

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

    @property
    def label(self) -> str:
        """The device node this connection addresses, to name it in a failure."""
        return self.uri.split("://", 1)[1]

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
