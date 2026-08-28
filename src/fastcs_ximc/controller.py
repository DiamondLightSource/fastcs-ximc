"""FastCS controller for a single libximc motion controller."""

from __future__ import annotations

import enum
import logging
from typing import Any

import libximc.highlevel as ximc
from fastcs import ONCE
from fastcs.attributes import AttrR, AttrRW, AttrW
from fastcs.controllers import Controller
from fastcs.datatypes import Bool, Float, Int, String
from fastcs.methods import command

from .config import XimcOptions
from .device import XimcDevice
from .io import XimcSettingsIO, XimcSettingsIORef

logger = logging.getLogger(__name__)

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

    Attributes with an ``io_ref`` are backed by a libximc struct field. The rest
    are soft attributes held only in software - the ``Units`` group, the marked
    position, the tweak step and the motion inhibit - and are writable so that a
    control system autosave layer can restore them across a restart.
    """

    def __init__(self, options: XimcOptions) -> None:
        self._options = options
        self._device = XimcDevice(options.device_uri)

        super().__init__(ios=[XimcSettingsIO(self._device)])

        period = options.poll_period

        # --- Position -------------------------------------------------------
        self.position = AttrR(
            Int(units=STEPS),
            io_ref=_ref("position", "Position", period),
            group="Position",
            description="Current position in steps",
        )
        self.position_microsteps = AttrR(
            Int(),
            io_ref=_ref("position", "uPosition", period),
            group="Position",
            description="Position fraction in microsteps",
        )
        self.encoder_position = AttrR(
            Int(),
            io_ref=_ref("position", "EncPosition", period),
            group="Position",
            description="Position reported by the encoder",
        )
        self.position_demand = AttrW(
            Int(units=STEPS),
            group="Position",
            description="Move to this absolute position in steps",
        )
        self.relative_move = AttrW(
            Int(units=STEPS),
            group="Position",
            description="Relative move in steps",
        )

        # --- Engineering units ----------------------------------------------
        self.egu = AttrRW(
            String(),
            group="Units",
            initial_value=DEFAULT_EGU,
            description="Engineering unit name",
        )
        self.motor_resolution = AttrRW(
            _egu_per_step(DEFAULT_EGU),
            group="Units",
            initial_value=1.0,
            description="Engineering units per step",
        )
        self.user_offset = AttrRW(
            _egu(DEFAULT_EGU),
            group="Units",
            initial_value=0.0,
            description="Offset from dial to user position",
        )
        self.user_position = AttrR(
            _egu(DEFAULT_EGU),
            group="Position",
            description="Current position in engineering units",
        )
        self.user_demand = AttrW(
            _egu(DEFAULT_EGU),
            group="Position",
            description="Move to this position in EGU",
        )

        # --- Mark -----------------------------------------------------------
        self.marked_position = AttrRW(
            Int(units=STEPS),
            group="Position",
            description="Marked position in steps",
        )

        # --- Motion ---------------------------------------------------------
        self.speed = AttrRW(
            Int(units=STEPS_PER_SECOND),
            io_ref=_ref("move", "Speed", period),
            group="Motion",
            description="Target speed in steps/s",
        )
        self.acceleration = AttrRW(
            Int(units="steps/s^2"),
            io_ref=_ref("move", "Accel", period),
            group="Motion",
            description="Acceleration in steps/s^2",
        )
        self.deceleration = AttrRW(
            Int(units="steps/s^2"),
            io_ref=_ref("move", "Decel", period),
            group="Motion",
            description="Deceleration in steps/s^2",
        )
        self.antiplay_speed = AttrRW(
            Int(units=STEPS_PER_SECOND),
            io_ref=_ref("move", "AntiplaySpeed", period),
            group="Motion",
            description="Backlash compensation speed, steps/s",
        )
        self.tweak_step = AttrRW(
            Int(units=STEPS),
            group="Motion",
            initial_value=1,
            description="Step size for a tweak move",
        )
        self.motion_inhibit = AttrRW(
            Bool(),
            group="Motion",
            description="Reject all move requests while set",
        )

        # --- Limits ---------------------------------------------------------
        self.low_limit = AttrRW(
            Int(units=STEPS),
            io_ref=_ref("edges", "LeftBorder", period),
            group="Limits",
            description="Soft limit in the reverse direction",
        )
        self.high_limit = AttrRW(
            Int(units=STEPS),
            io_ref=_ref("edges", "RightBorder", period),
            group="Limits",
            description="Soft limit in the forward direction",
        )
        self.stop_at_low_limit = AttrRW(
            Bool(),
            io_ref=_ref(
                "edges", "BorderFlags", period, bit=ximc.BorderFlags.BORDER_STOP_LEFT
            ),
            group="Limits",
            description="Stop when the low soft limit is hit",
        )
        self.stop_at_high_limit = AttrRW(
            Bool(),
            io_ref=_ref(
                "edges", "BorderFlags", period, bit=ximc.BorderFlags.BORDER_STOP_RIGHT
            ),
            group="Limits",
            description="Stop when the high soft limit is hit",
        )
        self.limits_use_encoder = AttrRW(
            Bool(),
            io_ref=_ref(
                "edges", "BorderFlags", period, bit=ximc.BorderFlags.BORDER_IS_ENCODER
            ),
            group="Limits",
            description="Soft limits are in encoder counts",
        )
        self.swap_limit_switches = AttrRW(
            Bool(),
            io_ref=_ref("edges", "EnderFlags", period, bit=ximc.EnderFlags.ENDER_SWAP),
            group="Limits",
            description="Limit switches are wired swapped",
        )
        self.switch_1_active_low = AttrRW(
            Bool(),
            io_ref=_ref(
                "edges", "EnderFlags", period, bit=ximc.EnderFlags.ENDER_SW1_ACTIVE_LOW
            ),
            group="Limits",
            description="Limit switch input 1 is active low",
        )
        self.switch_2_active_low = AttrRW(
            Bool(),
            io_ref=_ref(
                "edges", "EnderFlags", period, bit=ximc.EnderFlags.ENDER_SW2_ACTIVE_LOW
            ),
            group="Limits",
            description="Limit switch input 2 is active low",
        )

        # --- Homing ---------------------------------------------------------
        self.home_fast_speed = AttrRW(
            Int(units=STEPS_PER_SECOND),
            io_ref=_ref("home", "FastHome", period),
            group="Homing",
            description="Speed of the first homing move",
        )
        self.home_slow_speed = AttrRW(
            Int(units=STEPS_PER_SECOND),
            io_ref=_ref("home", "SlowHome", period),
            group="Homing",
            description="Speed of the precise homing move",
        )
        self.home_offset = AttrRW(
            Int(units=STEPS),
            io_ref=_ref("home", "HomeDelta", period),
            group="Homing",
            description="Steps to move after the home switch",
        )
        self.home_forward_first = AttrRW(
            Bool(),
            io_ref=_ref("home", "HomeFlags", period, bit=ximc.HomeFlags.HOME_DIR_FIRST),
            group="Homing",
            description="First homing move goes forward",
        )
        self.home_second_move = AttrRW(
            Bool(),
            io_ref=_ref("home", "HomeFlags", period, bit=ximc.HomeFlags.HOME_MV_SEC_EN),
            group="Homing",
            description="Do the second, precise homing move",
        )
        self.home_use_fast_algorithm = AttrRW(
            Bool(),
            io_ref=_ref("home", "HomeFlags", period, bit=ximc.HomeFlags.HOME_USE_FAST),
            group="Homing",
            description="Use the fast homing algorithm",
        )
        self.home_flags = AttrRW(
            Int(),
            io_ref=_ref("home", "HomeFlags", period),
            group="Homing",
            description="Raw homing behaviour bitmask",
        )

        # --- Status ---------------------------------------------------------
        self.moving = AttrR(
            Bool(),
            io_ref=_ref(
                "status", "MvCmdSts", period, bit=ximc.MvcmdStatus.MVCMD_RUNNING
            ),
            group="Status",
            description="A move command is running",
        )
        self.move_command_error = AttrR(
            Bool(),
            io_ref=_ref("status", "MvCmdSts", period, bit=ximc.MvcmdStatus.MVCMD_ERROR),
            group="Status",
            description="The last move command failed",
        )
        self.homed = AttrR(
            Bool(),
            io_ref=_ref("status", "Flags", period, bit=ximc.StateFlags.STATE_IS_HOMED),
            group="Status",
            description="Device has completed a homing move",
        )
        self.alarm = AttrR(
            Bool(),
            io_ref=_ref("status", "Flags", period, bit=ximc.StateFlags.STATE_ALARM),
            group="Status",
            description="Device is in an alarm state",
        )
        self.status_flags = AttrR(
            Int(),
            io_ref=_ref("status", "Flags", period),
            group="Status",
            description="Raw device state bitmask",
        )
        self.temperature = AttrR(
            Float(prec=1, units="degC"),
            io_ref=_ref("status", "CurT", period, scale=TENTHS_OF_DEGREE),
            group="Status",
            description="Controller temperature in degrees C",
        )
        self.power_voltage = AttrR(
            Float(prec=2, units="V"),
            io_ref=_ref("status", "Upwr", period, scale=TENS_OF_MILLIVOLTS),
            group="Status",
            description="Power supply voltage in V",
        )
        self.power_current = AttrR(
            Int(units="mA"),
            io_ref=_ref("status", "Ipwr", period),
            group="Status",
            description="Power supply current in mA",
        )
        self.usb_voltage = AttrR(
            Float(prec=2, units="V"),
            io_ref=_ref("status", "Uusb", period, scale=TENS_OF_MILLIVOLTS),
            group="Status",
            description="USB supply voltage in V",
        )
        self.usb_current = AttrR(
            Int(units="mA"),
            io_ref=_ref("status", "Iusb", period),
            group="Status",
            description="USB supply current in mA",
        )
        self.current_speed = AttrR(
            Int(units=STEPS_PER_SECOND),
            io_ref=_ref("status", "CurSpeed", period),
            group="Status",
            description="Speed the motor is actually moving at",
        )
        self.at_low_limit = AttrR(
            Bool(),
            io_ref=_ref(
                "status", "GPIOFlags", period, bit=ximc.GPIOFlags.STATE_LEFT_EDGE
            ),
            group="Status",
            description="Low-end limit switch is active",
        )
        self.at_high_limit = AttrR(
            Bool(),
            io_ref=_ref(
                "status", "GPIOFlags", period, bit=ximc.GPIOFlags.STATE_RIGHT_EDGE
            ),
            group="Status",
            description="High-end limit switch is active",
        )
        self.following_error = AttrR(
            Int(units=STEPS),
            group="Status",
            description="Step count minus encoder position",
        )

        # --- Engine ---------------------------------------------------------
        self.microstep_mode = AttrRW(
            Int(),
            io_ref=_ref("engine", "MicrostepMode", period),
            group="Engine",
            description="Microstep division mode",
        )
        self.nominal_voltage = AttrRW(
            Int(),
            io_ref=_ref("engine", "NomVoltage", period),
            group="Engine",
            description="Nominal motor voltage in tens of mV",
        )
        self.nominal_current = AttrRW(
            Int(units="mA"),
            io_ref=_ref("engine", "NomCurrent", period),
            group="Engine",
            description="Nominal motor current in mA",
        )
        self.nominal_speed = AttrRW(
            Int(units=STEPS_PER_SECOND),
            io_ref=_ref("engine", "NomSpeed", period),
            group="Engine",
            description="Nominal motor speed in steps/s",
        )

        # --- Power ----------------------------------------------------------
        self.hold_current = AttrRW(
            Int(units="%"),
            io_ref=_ref("power", "HoldCurrent", period),
            group="Power",
            description="Holding current, % of nominal",
        )
        self.current_reduction_delay = AttrRW(
            Int(units="ms"),
            io_ref=_ref("power", "CurrReductDelay", period),
            group="Power",
            description="Delay before reducing to hold current",
        )
        self.power_off_delay = AttrRW(
            Int(units="s"),
            io_ref=_ref("power", "PowerOffDelay", period),
            group="Power",
            description="Delay before powering the windings off",
        )
        self.current_set_time = AttrRW(
            Int(units="ms"),
            io_ref=_ref("power", "CurrentSetTime", period),
            group="Power",
            description="Ramp time when changing current",
        )
        self.current_reduction_enabled = AttrRW(
            Bool(),
            io_ref=_ref(
                "power", "PowerFlags", period, bit=ximc.PowerFlags.POWER_REDUCT_ENABLED
            ),
            group="Power",
            description="Reduce to hold current when idle",
        )
        self.power_off_enabled = AttrRW(
            Bool(),
            io_ref=_ref(
                "power", "PowerFlags", period, bit=ximc.PowerFlags.POWER_OFF_ENABLED
            ),
            group="Power",
            description="Power the windings off when idle",
        )
        self.smooth_current_set = AttrRW(
            Bool(),
            io_ref=_ref(
                "power", "PowerFlags", period, bit=ximc.PowerFlags.POWER_SMOOTH_CURRENT
            ),
            group="Power",
            description="Ramp current changes smoothly",
        )

        # --- Feedback -------------------------------------------------------
        self.feedback_type = AttrRW(
            Int(),
            io_ref=_ref("feedback", "FeedbackType", period),
            group="Feedback",
            description="Feedback source, 1=encoder 4=EMF 5=none",
        )
        self.encoder_counts_per_turn = AttrRW(
            Int(),
            io_ref=_ref("feedback", "CountsPerTurn", period),
            group="Feedback",
            description="Encoder counts per motor revolution",
        )
        self.steps_per_turn = AttrRW(
            Int(units=STEPS),
            io_ref=_ref("feedback", "IPS", period),
            group="Feedback",
            description="Motor steps per revolution",
        )
        self.encoder_reverse = AttrRW(
            Bool(),
            io_ref=_ref(
                "feedback",
                "FeedbackFlags",
                period,
                bit=ximc.FeedbackFlags.FEEDBACK_ENC_REVERSE,
            ),
            group="Feedback",
            description="Encoder counts the other way round",
        )

        # --- Protection -----------------------------------------------------
        self.critical_temperature = AttrRW(
            Float(prec=1, units="degC"),
            io_ref=_ref("secure", "CriticalT", period, scale=TENTHS_OF_DEGREE),
            group="Protection",
            description="Alarm above this temperature",
        )
        self.critical_power_voltage = AttrRW(
            Float(prec=2, units="V"),
            io_ref=_ref("secure", "CriticalUpwr", period, scale=TENS_OF_MILLIVOLTS),
            group="Protection",
            description="Alarm above this supply voltage",
        )
        self.low_power_voltage_off = AttrRW(
            Float(prec=2, units="V"),
            io_ref=_ref("secure", "LowUpwrOff", period, scale=TENS_OF_MILLIVOLTS),
            group="Protection",
            description="Power off below this supply voltage",
        )
        self.critical_power_current = AttrRW(
            Int(units="mA"),
            io_ref=_ref("secure", "CriticalIpwr", period),
            group="Protection",
            description="Alarm above this supply current",
        )
        self.critical_usb_voltage = AttrRW(
            Float(prec=2, units="V"),
            io_ref=_ref("secure", "CriticalUusb", period, scale=TENS_OF_MILLIVOLTS),
            group="Protection",
            description="Alarm above this USB voltage",
        )
        self.minimum_usb_voltage = AttrRW(
            Float(prec=2, units="V"),
            io_ref=_ref("secure", "MinimumUusb", period, scale=TENS_OF_MILLIVOLTS),
            group="Protection",
            description="Alarm below this USB voltage",
        )
        self.critical_usb_current = AttrRW(
            Int(units="mA"),
            io_ref=_ref("secure", "CriticalIusb", period),
            group="Protection",
            description="Alarm above this USB current",
        )
        self.alarm_on_overheat = AttrRW(
            Bool(),
            io_ref=_ref(
                "secure",
                "Flags",
                period,
                bit=ximc.SecureFlags.ALARM_ON_DRIVER_OVERHEATING,
            ),
            group="Protection",
            description="Alarm when the driver overheats",
        )
        self.low_voltage_protection = AttrRW(
            Bool(),
            io_ref=_ref(
                "secure", "Flags", period, bit=ximc.SecureFlags.LOW_UPWR_PROTECTION
            ),
            group="Protection",
            description="Power off on low supply voltage",
        )
        self.h_bridge_alert = AttrRW(
            Bool(),
            io_ref=_ref("secure", "Flags", period, bit=ximc.SecureFlags.H_BRIDGE_ALERT),
            group="Protection",
            description="Trip on an H-bridge fault",
        )
        self.alarm_on_limit_misset = AttrRW(
            Bool(),
            io_ref=_ref(
                "secure",
                "Flags",
                period,
                bit=ximc.SecureFlags.ALARM_ON_BORDERS_SWAP_MISSET,
            ),
            group="Protection",
            description="Alarm on swapped limit switches",
        )
        self.sticky_alarm = AttrRW(
            Bool(),
            io_ref=_ref(
                "secure", "Flags", period, bit=ximc.SecureFlags.ALARM_FLAGS_STICKING
            ),
            group="Protection",
            description="Alarm flags latch until cleared",
        )

        # --- Device information ---------------------------------------------
        self.manufacturer = AttrR(
            String(),
            io_ref=_ref("device_information", "Manufacturer", ONCE),
            group="Device",
        )
        self.product_description = AttrR(
            String(),
            io_ref=_ref("device_information", "ProductDescription", ONCE),
            group="Device",
        )
        self.controller_name = AttrR(
            String(),
            io_ref=_ref("controller_name", "ControllerName", ONCE),
            group="Device",
            description="User-assigned controller name",
        )
        self.stage_name = AttrR(
            String(),
            io_ref=_ref("stage_name", "PositionerName", ONCE),
            group="Device",
            description="User-assigned stage name",
        )
        self.serial_number = AttrR(
            Int(), group="Device", description="Controller serial number"
        )
        self.firmware_version = AttrR(
            String(), group="Device", description="Controller firmware version"
        )
        self.device_uri = AttrR(
            String(), group="Device", description="libximc URI of this device"
        )
        self.axis_description = AttrRW(
            String(),
            group="Device",
            description="Label for this axis, set by the user",
        )

        # Demands are actioned by commands rather than a settings struct, so
        # they are wired up by hand instead of through an io_ref.
        self.position_demand.set_on_put_callback(self._put_position_demand)
        self.relative_move.set_on_put_callback(self._put_relative_move)
        self.user_demand.set_on_put_callback(self._put_user_demand)

        # Derived soft attributes, recomputed whenever an input changes.
        self.position.add_on_update_callback(self._refresh_user_position)
        self.motor_resolution.add_on_update_callback(self._refresh_user_position)
        self.user_offset.add_on_update_callback(self._refresh_user_position)
        self.position.add_on_update_callback(self._refresh_following_error)
        self.encoder_position.add_on_update_callback(self._refresh_following_error)
        self.egu.add_on_update_callback(self._refresh_units)

    @property
    def device(self) -> XimcDevice:
        """The underlying async device wrapper."""
        return self._device

    async def connect(self) -> None:
        """Open the device and read the identity that cannot change."""

        try:
            await self._device.close()
        except Exception:
            logger.warning("Ignoring failure to close a stale handle for %s", self.path)

        await self._device.open()

        await self.device_uri.update(self._device.uri)
        await self.serial_number.update(
            await self._device.call(lambda axis: axis.get_serial_number())
        )
        info = await self._device.call(lambda axis: axis.get_device_information())
        await self.firmware_version.update(f"{info.Major}.{info.Minor}.{info.Release}")

        await super().connect()

    async def disconnect(self) -> None:
        await self._device.close()
        await super().disconnect()

    # --- Unit conversion ----------------------------------------------------

    def to_user(self, steps: int) -> float:
        """Convert a step count to a user position in engineering units."""
        return steps * self.motor_resolution.get() + self.user_offset.get()

    def to_steps(self, user: float) -> int:
        """Convert a user position in engineering units to a step count."""
        resolution = self.motor_resolution.get()
        if resolution == 0.0:
            raise ValueError("motor_resolution is zero - cannot convert from EGU")
        return round((user - self.user_offset.get()) / resolution)

    async def _refresh_user_position(self, _: Any = None) -> None:
        await self.user_position.update(self.to_user(self.position.get()))

    async def _refresh_following_error(self, _: Any = None) -> None:
        await self.following_error.update(
            self.position.get() - self.encoder_position.get()
        )

    async def _refresh_units(self, egu: str) -> None:
        """Push a new engineering unit name onto the attributes that carry it."""
        self.user_position.update_datatype(_egu(egu))
        self.user_demand.update_datatype(_egu(egu))
        self.user_offset.update_datatype(_egu(egu))
        self.motor_resolution.update_datatype(_egu_per_step(egu))

    # --- Move handlers ------------------------------------------------------

    def _check_motion_allowed(self) -> None:
        if self.motion_inhibit.get():
            raise MotionInhibitedError(f"Motion is inhibited on {self.path}")

    async def _move_absolute(self, value: int) -> None:
        self._check_motion_allowed()
        logger.info("Moving %s to absolute position %d", self.path, value)
        await self._device.call(lambda axis: axis.command_move(value, 0))

    async def _move_relative(self, value: int) -> None:
        self._check_motion_allowed()
        logger.info("Moving %s by %d steps", self.path, value)
        await self._device.call(lambda axis: axis.command_movr(value, 0))

    async def _put_position_demand(self, attr: AttrW[int, Any], value: int) -> None:
        await self._move_absolute(value)

    async def _put_relative_move(self, attr: AttrW[int, Any], value: int) -> None:
        await self._move_relative(value)

    async def _put_user_demand(self, attr: AttrW[float, Any], value: float) -> None:
        await self._move_absolute(self.to_steps(value))

    # --- Commands -----------------------------------------------------------

    @command(group="Motion")
    async def stop(self) -> None:
        """Stop immediately, ignoring the ramp."""
        await self._device.call(lambda axis: axis.command_stop())

    @command(group="Motion")
    async def soft_stop(self) -> None:
        """Stop using the deceleration ramp."""
        await self._device.call(lambda axis: axis.command_sstp())

    @command(group="Motion")
    async def jog_forward(self) -> None:
        """Jog forward until stopped."""
        self._check_motion_allowed()
        await self._device.call(lambda axis: axis.command_right())

    @command(group="Motion")
    async def jog_reverse(self) -> None:
        """Jog reverse until stopped."""
        self._check_motion_allowed()
        await self._device.call(lambda axis: axis.command_left())

    @command(group="Motion")
    async def tweak_forward(self) -> None:
        """Move forward by one tweak step."""
        await self._move_relative(self.tweak_step.get())

    @command(group="Motion")
    async def tweak_reverse(self) -> None:
        """Move back by one tweak step."""
        await self._move_relative(-self.tweak_step.get())

    @command(group="Motion")
    async def loft(self) -> None:
        """Perform a backlash compensation move."""
        self._check_motion_allowed()
        await self._device.call(lambda axis: axis.command_loft())

    @command(group="Position")
    async def mark_position(self) -> None:
        """Mark the current position."""
        position = int(await self._device.read_field("position", "Position"))
        logger.info("Marking position %d for %s", position, self.path)
        await self.marked_position.put(position, sync_setpoint=True)

    @command(group="Position")
    async def move_to_mark(self) -> None:
        """Move back to the marked position."""
        await self._move_absolute(self.marked_position.get())

    @command(group="Homing")
    async def home(self) -> None:
        """Move to the home position."""
        self._check_motion_allowed()
        await self._device.call(lambda axis: axis.command_home())

    @command(group="Homing")
    async def home_and_zero(self) -> None:
        """Home, then zero the position."""
        self._check_motion_allowed()
        await self._device.call(lambda axis: axis.command_homezero())

    @command(group="Homing")
    async def zero(self) -> None:
        """Set the current position to zero."""
        await self._device.call(lambda axis: axis.command_zero())

    @command(group="Device")
    async def power_off(self) -> None:
        """Remove holding current from windings."""
        await self._device.call(lambda axis: axis.command_power_off())

    @command(group="Device")
    async def save_settings_to_flash(self) -> None:
        """Save settings to controller flash."""
        await self._device.call(lambda axis: axis.command_save_settings())

    @command(group="Device")
    async def read_settings_from_flash(self) -> None:
        """Load settings from controller flash."""
        await self._device.call(lambda axis: axis.command_read_settings())


def _egu(egu: str) -> Float:
    """Datatype for a position or offset in engineering units."""
    return Float(prec=4, units=egu)


def _egu_per_step(egu: str) -> Float:
    """Datatype for a scale factor from steps to engineering units."""
    return Float(prec=6, units=f"{egu}/step")


def _ref(
    group: str,
    field: str,
    update_period: float | None,
    *,
    scale: float = 1.0,
    bit: int | enum.Flag | None = None,
) -> XimcSettingsIORef:
    """Shorthand for a `XimcSettingsIORef`, accepting libximc flag members."""
    return XimcSettingsIORef(
        group,
        field,
        scale=scale,
        bit=_bit_mask(bit),
        update_period=update_period,
    )


def _bit_mask(bit: int | enum.Flag | None) -> int | None:
    """Reduce a libximc flag member to its raw mask.

    libximc's flags derive from a ``StrictIntFlag`` that is not an ``int``
    subclass, so the underlying value has to be taken explicitly.
    """
    if bit is None or isinstance(bit, int):
        return bit
    return int(bit.value)
