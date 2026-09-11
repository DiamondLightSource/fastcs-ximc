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

# Raw device units per engineering unit, for scaled attributes
TENTHS_OF_DEGREE = 10.0
TENS_OF_MILLIVOLTS = 100.0

STEPS = "steps"
STEPS_PER_SECOND = "steps/s"
DEFAULT_EGU = "steps"


class MotionInhibitedError(RuntimeError):
    """Raised when a move is requested while ``motion_inhibit`` is set."""


class XimcController(Controller):
    """A single motion controller driven through the libximc library.

    Exposes the position, motion, status and engine settings that every libximc
    device supports. The device is addressed by a full libximc URI, so real
    (``xi-com://``, ``xi-net://``, ``xi-udp://``) and virtual (``xi-emu://``)
    devices are driven identically.

    libximc calls the two directions "left" and "right"; since a stage may be
    vertical or rotary, they are named after the position count instead -
    forward and high for increasing steps, reverse and low for decreasing.

    Attributes with a getter are backed by a libximc struct field. The rest are
    soft attributes held only in software - the ``Units`` group, the marked
    position, the tweak step and the motion inhibit - and are writable so that a
    control system autosave layer can restore them across a restart.
    """

    connection: XimcConnection

    def __init__(self, connections: Connections, options: XimcOptions) -> None:
        self.connection = connections.get("ximc", XimcConnection)
        self._period = options.poll_period

        super().__init__()

        # --- Position -------------------------------------------------------
        self.position = AttrR(
            int,
            self._read("position", "Position"),
            units=STEPS,
            group="Position",
            description="Current position in steps",
        )
        self.position_microsteps = AttrR(
            int,
            self._read("position", "uPosition"),
            group="Position",
            description="Position fraction in microsteps",
        )
        self.encoder_position = AttrR(
            int,
            self._read("position", "EncPosition"),
            group="Position",
            description="Position reported by the encoder",
        )
        self.position_demand = AttrW(
            int,
            self._move_absolute,
            units=STEPS,
            group="Position",
            description="Move to this absolute position in steps",
        )
        self.relative_move = AttrW(
            int,
            self._move_relative,
            units=STEPS,
            group="Position",
            description="Relative move in steps",
        )

        # --- Engineering units ----------------------------------------------
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
        self.user_offset = AttrRW(
            float,
            initial_value=0.0,
            precision=4,
            units=DEFAULT_EGU,
            group="Units",
            description="Offset from dial to user position",
        )
        self.user_position = AttrR(
            float,
            precision=4,
            units=DEFAULT_EGU,
            group="Position",
            description="Current position in engineering units",
        )
        self.user_demand = AttrW(
            float,
            self._move_user,
            precision=4,
            units=DEFAULT_EGU,
            group="Position",
            description="Move to this position in EGU",
        )

        # --- Mark -----------------------------------------------------------
        self.marked_position = AttrRW(
            int,
            units=STEPS,
            group="Position",
            description="Marked position in steps",
        )

        # --- Motion ---------------------------------------------------------
        self.speed = AttrRW(
            int,
            *self._field("move", "Speed"),
            units=STEPS_PER_SECOND,
            group="Motion",
            description="Target speed in steps/s",
        )
        self.acceleration = AttrRW(
            int,
            *self._field("move", "Accel"),
            units="steps/s^2",
            group="Motion",
            description="Acceleration in steps/s^2",
        )
        self.deceleration = AttrRW(
            int,
            *self._field("move", "Decel"),
            units="steps/s^2",
            group="Motion",
            description="Deceleration in steps/s^2",
        )
        self.antiplay_speed = AttrRW(
            int,
            *self._field("move", "AntiplaySpeed"),
            units=STEPS_PER_SECOND,
            group="Motion",
            description="Backlash compensation speed, steps/s",
        )
        self.tweak_step = AttrRW(
            int,
            initial_value=1,
            units=STEPS,
            group="Motion",
            description="Step size for a tweak move",
        )
        self.motion_inhibit = AttrRW(
            bool,
            group="Motion",
            description="Reject all move requests while set",
        )

        # --- Limits ---------------------------------------------------------
        self.low_limit = AttrRW(
            int,
            *self._field("edges", "LeftBorder"),
            units=STEPS,
            group="Limits",
            description="Soft limit in the reverse direction",
        )
        self.high_limit = AttrRW(
            int,
            *self._field("edges", "RightBorder"),
            units=STEPS,
            group="Limits",
            description="Soft limit in the forward direction",
        )
        self.stop_at_low_limit = AttrRW(
            bool,
            *self._field("edges", "BorderFlags", ximc.BorderFlags.BORDER_STOP_LEFT),
            group="Limits",
            description="Stop when the low soft limit is hit",
        )
        self.stop_at_high_limit = AttrRW(
            bool,
            *self._field("edges", "BorderFlags", ximc.BorderFlags.BORDER_STOP_RIGHT),
            group="Limits",
            description="Stop when the high soft limit is hit",
        )
        self.limits_use_encoder = AttrRW(
            bool,
            *self._field("edges", "BorderFlags", ximc.BorderFlags.BORDER_IS_ENCODER),
            group="Limits",
            description="Soft limits are in encoder counts",
        )
        self.swap_limit_switches = AttrRW(
            bool,
            *self._field("edges", "EnderFlags", ximc.EnderFlags.ENDER_SWAP),
            group="Limits",
            description="Limit switches are wired swapped",
        )
        self.switch_1_active_low = AttrRW(
            bool,
            *self._field("edges", "EnderFlags", ximc.EnderFlags.ENDER_SW1_ACTIVE_LOW),
            group="Limits",
            description="Limit switch input 1 is active low",
        )
        self.switch_2_active_low = AttrRW(
            bool,
            *self._field("edges", "EnderFlags", ximc.EnderFlags.ENDER_SW2_ACTIVE_LOW),
            group="Limits",
            description="Limit switch input 2 is active low",
        )

        # --- Homing ---------------------------------------------------------
        self.home_fast_speed = AttrRW(
            int,
            *self._field("home", "FastHome"),
            units=STEPS_PER_SECOND,
            group="Homing",
            description="Speed of the first homing move",
        )
        self.home_slow_speed = AttrRW(
            int,
            *self._field("home", "SlowHome"),
            units=STEPS_PER_SECOND,
            group="Homing",
            description="Speed of the precise homing move",
        )
        self.home_offset = AttrRW(
            int,
            *self._field("home", "HomeDelta"),
            units=STEPS,
            group="Homing",
            description="Steps to move after the home switch",
        )
        self.home_forward_first = AttrRW(
            bool,
            *self._field("home", "HomeFlags", ximc.HomeFlags.HOME_DIR_FIRST),
            group="Homing",
            description="First homing move goes forward",
        )
        self.home_second_move = AttrRW(
            bool,
            *self._field("home", "HomeFlags", ximc.HomeFlags.HOME_MV_SEC_EN),
            group="Homing",
            description="Do the second, precise homing move",
        )
        self.home_use_fast_algorithm = AttrRW(
            bool,
            *self._field("home", "HomeFlags", ximc.HomeFlags.HOME_USE_FAST),
            group="Homing",
            description="Use the fast homing algorithm",
        )
        self.home_flags = AttrRW(
            int,
            *self._field("home", "HomeFlags"),
            group="Homing",
            description="Raw homing behaviour bitmask",
        )

        # --- Status ---------------------------------------------------------
        self.moving = AttrR(
            bool,
            self._read("status", "MvCmdSts", ximc.MvcmdStatus.MVCMD_RUNNING),
            group="Status",
            description="A move command is running",
        )
        self.move_command_error = AttrR(
            bool,
            self._read("status", "MvCmdSts", ximc.MvcmdStatus.MVCMD_ERROR),
            group="Status",
            description="The last move command failed",
        )
        self.homed = AttrR(
            bool,
            self._read("status", "Flags", ximc.StateFlags.STATE_IS_HOMED),
            group="Status",
            description="Device has completed a homing move",
        )
        self.alarm = AttrR(
            bool,
            self._read("status", "Flags", ximc.StateFlags.STATE_ALARM),
            group="Status",
            description="Device is in an alarm state",
        )
        self.status_flags = AttrR(
            int,
            self._read("status", "Flags"),
            group="Status",
            description="Raw device state bitmask",
        )
        self.temperature = AttrR(
            float,
            self._read("status", "CurT", scale=TENTHS_OF_DEGREE),
            precision=1,
            units="degC",
            group="Status",
            description="Controller temperature in degrees C",
        )
        self.power_voltage = AttrR(
            float,
            self._read("status", "Upwr", scale=TENS_OF_MILLIVOLTS),
            precision=2,
            units="V",
            group="Status",
            description="Power supply voltage in V",
        )
        self.power_current = AttrR(
            int,
            self._read("status", "Ipwr"),
            units="mA",
            group="Status",
            description="Power supply current in mA",
        )
        self.usb_voltage = AttrR(
            float,
            self._read("status", "Uusb", scale=TENS_OF_MILLIVOLTS),
            precision=2,
            units="V",
            group="Status",
            description="USB supply voltage in V",
        )
        self.usb_current = AttrR(
            int,
            self._read("status", "Iusb"),
            units="mA",
            group="Status",
            description="USB supply current in mA",
        )
        self.current_speed = AttrR(
            int,
            self._read("status", "CurSpeed"),
            units=STEPS_PER_SECOND,
            group="Status",
            description="Speed the motor is actually moving at",
        )
        self.at_low_limit = AttrR(
            bool,
            self._read("status", "GPIOFlags", ximc.GPIOFlags.STATE_LEFT_EDGE),
            group="Status",
            description="Low-end limit switch is active",
        )
        self.at_high_limit = AttrR(
            bool,
            self._read("status", "GPIOFlags", ximc.GPIOFlags.STATE_RIGHT_EDGE),
            group="Status",
            description="High-end limit switch is active",
        )
        self.following_error = AttrR(
            int,
            units=STEPS,
            group="Status",
            description="Step count minus encoder position",
        )

        # --- Engine ---------------------------------------------------------
        self.microstep_mode = AttrRW(
            int,
            *self._field("engine", "MicrostepMode"),
            group="Engine",
            description="Microstep division mode",
        )
        self.nominal_voltage = AttrRW(
            int,
            *self._field("engine", "NomVoltage"),
            group="Engine",
            description="Nominal motor voltage in tens of mV",
        )
        self.nominal_current = AttrRW(
            int,
            *self._field("engine", "NomCurrent"),
            units="mA",
            group="Engine",
            description="Nominal motor current in mA",
        )
        self.nominal_speed = AttrRW(
            int,
            *self._field("engine", "NomSpeed"),
            units=STEPS_PER_SECOND,
            group="Engine",
            description="Nominal motor speed in steps/s",
        )

        # --- Power ----------------------------------------------------------
        self.hold_current = AttrRW(
            int,
            *self._field("power", "HoldCurrent"),
            units="%",
            group="Power",
            description="Holding current, % of nominal",
        )
        self.current_reduction_delay = AttrRW(
            int,
            *self._field("power", "CurrReductDelay"),
            units="ms",
            group="Power",
            description="Delay before reducing to hold current",
        )
        self.power_off_delay = AttrRW(
            int,
            *self._field("power", "PowerOffDelay"),
            units="s",
            group="Power",
            description="Delay before powering the windings off",
        )
        self.current_set_time = AttrRW(
            int,
            *self._field("power", "CurrentSetTime"),
            units="ms",
            group="Power",
            description="Ramp time when changing current",
        )
        self.current_reduction_enabled = AttrRW(
            bool,
            *self._field("power", "PowerFlags", ximc.PowerFlags.POWER_REDUCT_ENABLED),
            group="Power",
            description="Reduce to hold current when idle",
        )
        self.power_off_enabled = AttrRW(
            bool,
            *self._field("power", "PowerFlags", ximc.PowerFlags.POWER_OFF_ENABLED),
            group="Power",
            description="Power the windings off when idle",
        )
        self.smooth_current_set = AttrRW(
            bool,
            *self._field("power", "PowerFlags", ximc.PowerFlags.POWER_SMOOTH_CURRENT),
            group="Power",
            description="Ramp current changes smoothly",
        )

        # --- Feedback -------------------------------------------------------
        self.feedback_type = AttrRW(
            int,
            *self._field("feedback", "FeedbackType"),
            group="Feedback",
            description="Feedback source, 1=encoder 4=EMF 5=none",
        )
        self.encoder_counts_per_turn = AttrRW(
            int,
            *self._field("feedback", "CountsPerTurn"),
            group="Feedback",
            description="Encoder counts per motor revolution",
        )
        self.steps_per_turn = AttrRW(
            int,
            *self._field("feedback", "IPS"),
            units=STEPS,
            group="Feedback",
            description="Motor steps per revolution",
        )
        self.encoder_reverse = AttrRW(
            bool,
            *self._field(
                "feedback", "FeedbackFlags", ximc.FeedbackFlags.FEEDBACK_ENC_REVERSE
            ),
            group="Feedback",
            description="Encoder counts the other way round",
        )

        # --- Protection -----------------------------------------------------
        self.critical_temperature = AttrRW(
            float,
            *self._field("secure", "CriticalT", scale=TENTHS_OF_DEGREE),
            precision=1,
            units="degC",
            group="Protection",
            description="Alarm above this temperature",
        )
        self.critical_power_voltage = AttrRW(
            float,
            *self._field("secure", "CriticalUpwr", scale=TENS_OF_MILLIVOLTS),
            precision=2,
            units="V",
            group="Protection",
            description="Alarm above this supply voltage",
        )
        self.low_power_voltage_off = AttrRW(
            float,
            *self._field("secure", "LowUpwrOff", scale=TENS_OF_MILLIVOLTS),
            precision=2,
            units="V",
            group="Protection",
            description="Power off below this supply voltage",
        )
        self.critical_power_current = AttrRW(
            int,
            *self._field("secure", "CriticalIpwr"),
            units="mA",
            group="Protection",
            description="Alarm above this supply current",
        )
        self.critical_usb_voltage = AttrRW(
            float,
            *self._field("secure", "CriticalUusb", scale=TENS_OF_MILLIVOLTS),
            precision=2,
            units="V",
            group="Protection",
            description="Alarm above this USB voltage",
        )
        self.minimum_usb_voltage = AttrRW(
            float,
            *self._field("secure", "MinimumUusb", scale=TENS_OF_MILLIVOLTS),
            precision=2,
            units="V",
            group="Protection",
            description="Alarm below this USB voltage",
        )
        self.critical_usb_current = AttrRW(
            int,
            *self._field("secure", "CriticalIusb"),
            units="mA",
            group="Protection",
            description="Alarm above this USB current",
        )
        self.alarm_on_overheat = AttrRW(
            bool,
            *self._field(
                "secure", "Flags", ximc.SecureFlags.ALARM_ON_DRIVER_OVERHEATING
            ),
            group="Protection",
            description="Alarm when the driver overheats",
        )
        self.low_voltage_protection = AttrRW(
            bool,
            *self._field("secure", "Flags", ximc.SecureFlags.LOW_UPWR_PROTECTION),
            group="Protection",
            description="Power off on low supply voltage",
        )
        self.h_bridge_alert = AttrRW(
            bool,
            *self._field("secure", "Flags", ximc.SecureFlags.H_BRIDGE_ALERT),
            group="Protection",
            description="Trip on an H-bridge fault",
        )
        self.alarm_on_limit_misset = AttrRW(
            bool,
            *self._field(
                "secure", "Flags", ximc.SecureFlags.ALARM_ON_BORDERS_SWAP_MISSET
            ),
            group="Protection",
            description="Alarm on swapped limit switches",
        )
        self.sticky_alarm = AttrRW(
            bool,
            *self._field("secure", "Flags", ximc.SecureFlags.ALARM_FLAGS_STICKING),
            group="Protection",
            description="Alarm flags latch until cleared",
        )

        # --- Device information ---------------------------------------------
        # A getter with no schedule is read once, when the connection opens.
        self.manufacturer = AttrR(
            str,
            self._read("device_information", "Manufacturer", once=True),
            group="Device",
        )
        self.product_description = AttrR(
            str,
            self._read("device_information", "ProductDescription", once=True),
            group="Device",
        )
        self.controller_name = AttrR(
            str,
            self._read("controller_name", "ControllerName", once=True),
            group="Device",
            description="User-assigned controller name",
        )
        self.stage_name = AttrR(
            str,
            self._read("stage_name", "PositionerName", once=True),
            group="Device",
            description="User-assigned stage name",
        )
        self.serial_number = AttrR(
            int, group="Device", description="Controller serial number"
        )
        self.firmware_version = AttrR(
            str, group="Device", description="Controller firmware version"
        )
        self.device_uri = AttrR(
            str, group="Device", description="libximc URI of this device"
        )
        self.axis_description = AttrRW(
            str,
            group="Device",
            description="Label for this axis, set by the user",
        )

        # Derived soft attributes, recomputed whenever an input changes.
        self.position.add_readback_callback(self._refresh_user_position)
        self.motor_resolution.add_readback_callback(self._refresh_user_position)
        self.user_offset.add_readback_callback(self._refresh_user_position)
        self.position.add_readback_callback(self._refresh_following_error)
        self.encoder_position.add_readback_callback(self._refresh_following_error)
        self.egu.add_readback_callback(self._refresh_units)

    async def setup(self) -> None:
        """Read the identity that cannot change."""
        await self.device_uri.update(self.connection.uri)
        await self.serial_number.update(
            await self.connection.call(lambda axis: axis.get_serial_number())
        )
        info = await self.connection.call(lambda axis: axis.get_device_information())
        await self.firmware_version.update(f"{info.Major}.{info.Minor}.{info.Release}")

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
            raw = await self.connection.read_field(group, field)
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
                await self.connection.write_bit(group, field, int(bit.value), value)
            else:
                await self.connection.write_field(
                    group, field, _to_device(value, scale)
                )

        return self._read(group, field, bit, scale=scale), write

    # --- Unit conversion ----------------------------------------------------

    def to_user(self, steps: int) -> float:
        """Convert a step count to a user position in engineering units."""
        return steps * self.motor_resolution.readback + self.user_offset.readback

    def to_steps(self, user: float) -> int:
        """Convert a user position in engineering units to a step count."""
        resolution = self.motor_resolution.readback
        if resolution == 0.0:
            raise ValueError("motor_resolution is zero - cannot convert from EGU")
        return round((user - self.user_offset.readback) / resolution)

    async def _refresh_user_position(self, _: Any = None) -> None:
        await self.user_position.update(self.to_user(self.position.readback))

    async def _refresh_following_error(self, _: Any = None) -> None:
        await self.following_error.update(
            self.position.readback - self.encoder_position.readback
        )

    async def _refresh_units(self, egu: str) -> None:
        """Push a new engineering unit name onto the attributes that carry it."""
        for attribute in (self.user_position, self.user_demand, self.user_offset):
            attribute.update_meta({**attribute.meta, "units": egu})
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
        await self.connection.call(lambda axis: axis.command_move(value, 0))

    async def _move_relative(self, value: int) -> None:
        self._check_motion_allowed()
        logger.info("Moving relative", path=self.path, steps=value)
        await self.connection.call(lambda axis: axis.command_movr(value, 0))

    async def _move_user(self, value: float) -> None:
        await self._move_absolute(self.to_steps(value))

    # --- Commands -----------------------------------------------------------

    @command(group="Motion")
    async def stop(self) -> None:
        """Stop immediately, ignoring the ramp."""
        await self.connection.call(lambda axis: axis.command_stop())

    @command(group="Motion")
    async def soft_stop(self) -> None:
        """Stop using the deceleration ramp."""
        await self.connection.call(lambda axis: axis.command_sstp())

    @command(group="Motion")
    async def jog_forward(self) -> None:
        """Jog forward until stopped."""
        self._check_motion_allowed()
        await self.connection.call(lambda axis: axis.command_right())

    @command(group="Motion")
    async def jog_reverse(self) -> None:
        """Jog reverse until stopped."""
        self._check_motion_allowed()
        await self.connection.call(lambda axis: axis.command_left())

    @command(group="Motion")
    async def tweak_forward(self) -> None:
        """Move forward by one tweak step."""
        await self._move_relative(self.tweak_step.readback)

    @command(group="Motion")
    async def tweak_reverse(self) -> None:
        """Move back by one tweak step."""
        await self._move_relative(-self.tweak_step.readback)

    @command(group="Motion")
    async def loft(self) -> None:
        """Perform a backlash compensation move."""
        self._check_motion_allowed()
        await self.connection.call(lambda axis: axis.command_loft())

    @command(group="Position")
    async def mark_position(self) -> None:
        """Mark the current position."""
        position = int(await self.connection.read_field("position", "Position"))
        logger.info("Marking position", path=self.path, steps=position)
        await self.marked_position.set(position)

    @command(group="Position")
    async def move_to_mark(self) -> None:
        """Move back to the marked position."""
        await self._move_absolute(self.marked_position.readback)

    @command(group="Homing")
    async def home(self) -> None:
        """Move to the home position."""
        self._check_motion_allowed()
        await self.connection.call(lambda axis: axis.command_home())

    @command(group="Homing")
    async def home_and_zero(self) -> None:
        """Home, then zero the position."""
        self._check_motion_allowed()
        await self.connection.call(lambda axis: axis.command_homezero())

    @command(group="Homing")
    async def zero(self) -> None:
        """Set the current position to zero."""
        await self.connection.call(lambda axis: axis.command_zero())

    @command(group="Device")
    async def power_off(self) -> None:
        """Remove holding current from windings."""
        await self.connection.call(lambda axis: axis.command_power_off())

    @command(group="Device")
    async def save_settings_to_flash(self) -> None:
        """Save settings to controller flash."""
        await self.connection.call(lambda axis: axis.command_save_settings())

    @command(group="Device")
    async def read_settings_from_flash(self) -> None:
        """Load settings from controller flash."""
        await self.connection.call(lambda axis: axis.command_read_settings())


def _to_device(value: Any, scale: float) -> Any:
    """Convert an attribute value back into raw device units."""
    if scale != 1.0 and isinstance(value, int | float):
        return int(round(value * scale))
    return value
