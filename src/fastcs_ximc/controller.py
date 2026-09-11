"""FastCS controller for a single libximc motion controller."""

from __future__ import annotations

import enum
from typing import Any

import libximc.highlevel as ximc
from fastcs.attributes import AttrR, AttrRW, AttrW, Getter, Polled, Setter
from fastcs.connections import Connections
from fastcs.controllers import Controller
from fastcs.logging import logger
from fastcs.methods import command

from .config import XimcOptions
from .connection import XimcConnection

TENTHS_OF_DEGREE = 10.0  # raw device units per engineering unit
STEPS = "steps"
DEFAULT_EGU = "steps"


class MotionInhibitedError(RuntimeError):
    """Raised when a move is requested while ``motion_inhibit`` is set."""


class XimcController(Controller):
    """A single motion controller driven through the libximc library.

    A preview: one attribute of each shape the full driver uses, with a comment
    in each group saying what the rest of it is made of.
    """

    connection: XimcConnection

    def __init__(self, connections: Connections, options: XimcOptions) -> None:
        self.connection = connections.get("motor", XimcConnection)
        self._period = options.poll_period

        super().__init__()

        # --- Position: a polled field, and a write actioned by a command -----
        self.position = AttrR(
            int,
            self._read("position", "Position"),
            units=STEPS,
            group="Position",
            description="Current position in steps",
        )
        self.position_demand = AttrW(
            int,
            self._move_absolute,
            units=STEPS,
            group="Position",
            description="Move to this absolute position in steps",
        )
        # position_microsteps and encoder_position are `position` with another
        # field name; relative_move is `position_demand` with another handler.

        # --- Units: soft attributes, and one derived from them ---------------
        self.egu = AttrRW(
            str,
            initial_value=DEFAULT_EGU,
            group="Units",
            description="Engineering unit name",
        )
        self.motor_resolution = AttrRW(
            float,
            initial_value=1.0,
            precision=6,
            units=f"{DEFAULT_EGU}/step",
            group="Units",
            description="Engineering units per step",
        )
        self.user_position = AttrR(
            float,
            precision=4,
            units=DEFAULT_EGU,
            group="Position",
            description="Current position in engineering units",
        )
        # user_offset extends the transform, user_demand writes through it, and
        # following_error is a second attribute derived like user_position.

        # --- Motion: a field read and written, and a soft interlock ----------
        self.speed = AttrRW(
            int,
            *self._field("move", "Speed"),
            units="steps/s",
            group="Motion",
            description="Target speed in steps/s",
        )
        self.motion_inhibit = AttrRW(
            bool,
            group="Motion",
            description="Reject all move requests while set",
        )
        # acceleration, deceleration and antiplay_speed are `speed` with
        # another field of the same struct; tweak_step is a soft int.

        # --- Limits: one bit of a bitmask field, read and written ------------
        self.stop_at_low_limit = AttrRW(
            bool,
            *self._field("edges", "BorderFlags", ximc.BorderFlags.BORDER_STOP_LEFT),
            group="Limits",
            description="Stop when the low soft limit is hit",
        )
        # low_limit and high_limit are plain fields of the same struct; the
        # other BorderFlags and EnderFlags bits differ only in the mask.

        # --- Status: a status bit, and a reading in raw units ----------------
        self.moving = AttrR(
            bool,
            self._read("status", "MvCmdSts", ximc.MvcmdStatus.MVCMD_RUNNING),
            group="Status",
            description="A move command is running",
        )
        self.temperature = AttrR(
            float,
            self._read("status", "CurT", scale=TENTHS_OF_DEGREE),
            precision=1,
            units="degC",
            group="Status",
            description="Controller temperature in degrees C",
        )
        # homed, alarm and the limit switch inputs are `moving` with another
        # mask; the voltages and currents are `temperature` with another scale.

        # --- Device: read once on connect, the second from its own getter ----
        self.manufacturer = AttrR(
            str,
            self._read("device_information", "Manufacturer", once=True),
            group="Device",
        )
        self.firmware_version = AttrR(
            str,
            self._read_firmware_version,
            group="Device",
            description="Controller firmware version",
        )
        # serial_number, controller_name and stage_name are read the same way,
        # and axis_description is a soft label for the axis.

        # Derived soft attributes, recomputed whenever an input changes.
        self.position.add_readback_callback(self._refresh_user_position)
        self.motor_resolution.add_readback_callback(self._refresh_user_position)
        self.egu.add_readback_callback(self._refresh_units)

    # --- Attribute IO -------------------------------------------------------

    def _read(
        self,
        group: str,
        field: str,
        bit: enum.Flag | None = None,
        *,
        scale: float = 1.0,
        once: bool = False,
    ) -> Getter[Any] | Polled[Any]:
        """A getter for one field of one libximc struct, or one bit of one.

        ``group`` is the libximc struct name without its ``_settings_t`` suffix,
        so ``("move", "Speed")`` reads ``get_move_settings().Speed``.
        """

        async def read() -> Any:
            raw = await self.connection.read(group, field)
            if bit is not None:
                return bool(int(raw) & int(bit.value))
            return raw / scale if scale != 1.0 else raw

        return read if once else Polled(read, period=self._period)

    def _field(
        self,
        group: str,
        field: str,
        bit: enum.Flag | None = None,
        *,
        scale: float = 1.0,
    ) -> tuple[Getter[Any] | Polled[Any], Setter[Any]]:
        """The getter and setter for one field of a libximc settings struct."""

        async def write(value: Any) -> None:
            if bit is not None:
                await self.connection.write(group, field, value, int(bit.value))
            else:
                await self.connection.write(group, field, _to_device(value, scale))

        return self._read(group, field, bit, scale=scale), write

    async def _read_firmware_version(self) -> str:
        """A getter that is not one field: three of them, as one string."""
        info = await self.connection.read_struct("device_information")
        return f"{info.Major}.{info.Minor}.{info.Release}"

    # --- Unit conversion ----------------------------------------------------

    def to_user(self, steps: int) -> float:
        """Convert a step count to a user position in engineering units."""
        return steps * self.motor_resolution.readback

    async def _refresh_user_position(self, _: Any = None) -> None:
        await self.user_position.update(self.to_user(self.position.readback))

    async def _refresh_units(self, egu: str) -> None:
        """Push a new engineering unit name onto the attributes that carry it."""
        self.user_position.update_meta({**self.user_position.meta, "units": egu})
        self.motor_resolution.update_meta(
            {**self.motor_resolution.meta, "units": f"{egu}/step"}
        )

    # --- Move handlers ------------------------------------------------------

    def _check_motion_allowed(self) -> None:
        if self.motion_inhibit.readback:
            raise MotionInhibitedError(f"Motion is inhibited on {self.path}")

    async def _move_absolute(self, value: int) -> None:
        self._check_motion_allowed()
        logger.info("Moving to absolute position", path=self.path, steps=value)
        await self.connection.command("move", value, 0)

    # --- Commands -----------------------------------------------------------

    @command(group="Motion")
    async def stop(self) -> None:
        """Stop immediately, ignoring the ramp."""
        await self.connection.command("stop")

    @command(group="Motion")
    async def jog_forward(self) -> None:
        """Jog forward until stopped."""
        self._check_motion_allowed()
        # libximc's "right" is increasing steps, which this driver calls forward
        await self.connection.command("right")

    # soft_stop, jog_reverse, loft, home, home_and_zero, zero, power_off and
    # the two flash commands are these two shapes with another libximc command.


def _to_device(value: Any, scale: float) -> Any:
    """Convert an attribute value back into raw device units."""
    if scale != 1.0 and isinstance(value, int | float):
        return int(round(value * scale))
    return value
