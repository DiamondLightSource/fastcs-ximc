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

# Flags of the bitmask fields, named once so that the getter and the setter of
# one attribute cannot disagree about which bit they address.
STOP_LEFT = ximc.BorderFlags.BORDER_STOP_LEFT
STOP_RIGHT = ximc.BorderFlags.BORDER_STOP_RIGHT
BORDER_IS_ENCODER = ximc.BorderFlags.BORDER_IS_ENCODER
ENDER_SWAP = ximc.EnderFlags.ENDER_SWAP
ENDER_SW1_LOW = ximc.EnderFlags.ENDER_SW1_ACTIVE_LOW
ENDER_SW2_LOW = ximc.EnderFlags.ENDER_SW2_ACTIVE_LOW
HOME_DIR_FIRST = ximc.HomeFlags.HOME_DIR_FIRST
HOME_MV_SEC_EN = ximc.HomeFlags.HOME_MV_SEC_EN
HOME_USE_FAST = ximc.HomeFlags.HOME_USE_FAST
MVCMD_RUNNING = ximc.MvcmdStatus.MVCMD_RUNNING
MVCMD_ERROR = ximc.MvcmdStatus.MVCMD_ERROR
STATE_IS_HOMED = ximc.StateFlags.STATE_IS_HOMED
STATE_ALARM = ximc.StateFlags.STATE_ALARM
STATE_LEFT_EDGE = ximc.GPIOFlags.STATE_LEFT_EDGE
STATE_RIGHT_EDGE = ximc.GPIOFlags.STATE_RIGHT_EDGE
POWER_REDUCT_ENABLED = ximc.PowerFlags.POWER_REDUCT_ENABLED
POWER_OFF_ENABLED = ximc.PowerFlags.POWER_OFF_ENABLED
POWER_SMOOTH_CURRENT = ximc.PowerFlags.POWER_SMOOTH_CURRENT
ENC_REVERSE = ximc.FeedbackFlags.FEEDBACK_ENC_REVERSE
ALARM_ON_OVERHEAT = ximc.SecureFlags.ALARM_ON_DRIVER_OVERHEATING
LOW_UPWR_PROTECTION = ximc.SecureFlags.LOW_UPWR_PROTECTION
H_BRIDGE_ALERT = ximc.SecureFlags.H_BRIDGE_ALERT
ALARM_ON_MISSET = ximc.SecureFlags.ALARM_ON_BORDERS_SWAP_MISSET
ALARM_FLAGS_STICKING = ximc.SecureFlags.ALARM_FLAGS_STICKING


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
            getter=self._reader("position", "Position", int),
            units=STEPS,
            group="Position",
            description="Current position in steps",
        )
        self.position_microsteps = AttrR(
            int,
            getter=self._reader("position", "uPosition", int),
            group="Position",
            description="Position fraction in microsteps",
        )
        self.encoder_position = AttrR(
            int,
            getter=self._reader("position", "EncPosition", int),
            group="Position",
            description="Position reported by the encoder",
        )
        self.position_demand = AttrW(
            int,
            setter=self._move_absolute,
            units=STEPS,
            group="Position",
            description="Move to this absolute position in steps",
        )
        self.relative_move = AttrW(
            int,
            setter=self._move_relative,
            units=STEPS,
            group="Position",
            description="Relative move in steps",
        )

        # --- Engineering units ----------------------------------------------
        self.egu = AttrRW(
            str,
            group="Units",
            initial_value=DEFAULT_EGU,
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
            setter=self._move_user,
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
            getter=self._reader("move", "Speed", int),
            setter=self._writer("move", "Speed"),
            units=STEPS_PER_SECOND,
            group="Motion",
            description="Target speed in steps/s",
        )
        self.acceleration = AttrRW(
            int,
            getter=self._reader("move", "Accel", int),
            setter=self._writer("move", "Accel"),
            units="steps/s^2",
            group="Motion",
            description="Acceleration in steps/s^2",
        )
        self.deceleration = AttrRW(
            int,
            getter=self._reader("move", "Decel", int),
            setter=self._writer("move", "Decel"),
            units="steps/s^2",
            group="Motion",
            description="Deceleration in steps/s^2",
        )
        self.antiplay_speed = AttrRW(
            int,
            getter=self._reader("move", "AntiplaySpeed", int),
            setter=self._writer("move", "AntiplaySpeed"),
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
            getter=self._reader("edges", "LeftBorder", int),
            setter=self._writer("edges", "LeftBorder"),
            units=STEPS,
            group="Limits",
            description="Soft limit in the reverse direction",
        )
        self.high_limit = AttrRW(
            int,
            getter=self._reader("edges", "RightBorder", int),
            setter=self._writer("edges", "RightBorder"),
            units=STEPS,
            group="Limits",
            description="Soft limit in the forward direction",
        )
        self.stop_at_low_limit = AttrRW(
            bool,
            getter=self._flag_reader("edges", "BorderFlags", STOP_LEFT),
            setter=self._flag_writer("edges", "BorderFlags", STOP_LEFT),
            group="Limits",
            description="Stop when the low soft limit is hit",
        )
        self.stop_at_high_limit = AttrRW(
            bool,
            getter=self._flag_reader("edges", "BorderFlags", STOP_RIGHT),
            setter=self._flag_writer("edges", "BorderFlags", STOP_RIGHT),
            group="Limits",
            description="Stop when the high soft limit is hit",
        )
        self.limits_use_encoder = AttrRW(
            bool,
            getter=self._flag_reader("edges", "BorderFlags", BORDER_IS_ENCODER),
            setter=self._flag_writer("edges", "BorderFlags", BORDER_IS_ENCODER),
            group="Limits",
            description="Soft limits are in encoder counts",
        )
        self.swap_limit_switches = AttrRW(
            bool,
            getter=self._flag_reader("edges", "EnderFlags", ENDER_SWAP),
            setter=self._flag_writer("edges", "EnderFlags", ENDER_SWAP),
            group="Limits",
            description="Limit switches are wired swapped",
        )
        self.switch_1_active_low = AttrRW(
            bool,
            getter=self._flag_reader("edges", "EnderFlags", ENDER_SW1_LOW),
            setter=self._flag_writer("edges", "EnderFlags", ENDER_SW1_LOW),
            group="Limits",
            description="Limit switch input 1 is active low",
        )
        self.switch_2_active_low = AttrRW(
            bool,
            getter=self._flag_reader("edges", "EnderFlags", ENDER_SW2_LOW),
            setter=self._flag_writer("edges", "EnderFlags", ENDER_SW2_LOW),
            group="Limits",
            description="Limit switch input 2 is active low",
        )

        # --- Homing ---------------------------------------------------------
        self.home_fast_speed = AttrRW(
            int,
            getter=self._reader("home", "FastHome", int),
            setter=self._writer("home", "FastHome"),
            units=STEPS_PER_SECOND,
            group="Homing",
            description="Speed of the first homing move",
        )
        self.home_slow_speed = AttrRW(
            int,
            getter=self._reader("home", "SlowHome", int),
            setter=self._writer("home", "SlowHome"),
            units=STEPS_PER_SECOND,
            group="Homing",
            description="Speed of the precise homing move",
        )
        self.home_offset = AttrRW(
            int,
            getter=self._reader("home", "HomeDelta", int),
            setter=self._writer("home", "HomeDelta"),
            units=STEPS,
            group="Homing",
            description="Steps to move after the home switch",
        )
        self.home_forward_first = AttrRW(
            bool,
            getter=self._flag_reader("home", "HomeFlags", HOME_DIR_FIRST),
            setter=self._flag_writer("home", "HomeFlags", HOME_DIR_FIRST),
            group="Homing",
            description="First homing move goes forward",
        )
        self.home_second_move = AttrRW(
            bool,
            getter=self._flag_reader("home", "HomeFlags", HOME_MV_SEC_EN),
            setter=self._flag_writer("home", "HomeFlags", HOME_MV_SEC_EN),
            group="Homing",
            description="Do the second, precise homing move",
        )
        self.home_use_fast_algorithm = AttrRW(
            bool,
            getter=self._flag_reader("home", "HomeFlags", HOME_USE_FAST),
            setter=self._flag_writer("home", "HomeFlags", HOME_USE_FAST),
            group="Homing",
            description="Use the fast homing algorithm",
        )
        self.home_flags = AttrRW(
            int,
            getter=self._reader("home", "HomeFlags", int),
            setter=self._writer("home", "HomeFlags"),
            group="Homing",
            description="Raw homing behaviour bitmask",
        )

        # --- Status ---------------------------------------------------------
        self.moving = AttrR(
            bool,
            getter=self._flag_reader("status", "MvCmdSts", MVCMD_RUNNING),
            group="Status",
            description="A move command is running",
        )
        self.move_command_error = AttrR(
            bool,
            getter=self._flag_reader("status", "MvCmdSts", MVCMD_ERROR),
            group="Status",
            description="The last move command failed",
        )
        self.homed = AttrR(
            bool,
            getter=self._flag_reader("status", "Flags", STATE_IS_HOMED),
            group="Status",
            description="Device has completed a homing move",
        )
        self.alarm = AttrR(
            bool,
            getter=self._flag_reader("status", "Flags", STATE_ALARM),
            group="Status",
            description="Device is in an alarm state",
        )
        self.status_flags = AttrR(
            int,
            getter=self._reader("status", "Flags", int),
            group="Status",
            description="Raw device state bitmask",
        )
        self.temperature = AttrR(
            float,
            getter=self._reader("status", "CurT", float, scale=TENTHS_OF_DEGREE),
            precision=1,
            units="degC",
            group="Status",
            description="Controller temperature in degrees C",
        )
        self.power_voltage = AttrR(
            float,
            getter=self._reader("status", "Upwr", float, scale=TENS_OF_MILLIVOLTS),
            precision=2,
            units="V",
            group="Status",
            description="Power supply voltage in V",
        )
        self.power_current = AttrR(
            int,
            getter=self._reader("status", "Ipwr", int),
            units="mA",
            group="Status",
            description="Power supply current in mA",
        )
        self.usb_voltage = AttrR(
            float,
            getter=self._reader("status", "Uusb", float, scale=TENS_OF_MILLIVOLTS),
            precision=2,
            units="V",
            group="Status",
            description="USB supply voltage in V",
        )
        self.usb_current = AttrR(
            int,
            getter=self._reader("status", "Iusb", int),
            units="mA",
            group="Status",
            description="USB supply current in mA",
        )
        self.current_speed = AttrR(
            int,
            getter=self._reader("status", "CurSpeed", int),
            units=STEPS_PER_SECOND,
            group="Status",
            description="Speed the motor is actually moving at",
        )
        self.at_low_limit = AttrR(
            bool,
            getter=self._flag_reader("status", "GPIOFlags", STATE_LEFT_EDGE),
            group="Status",
            description="Low-end limit switch is active",
        )
        self.at_high_limit = AttrR(
            bool,
            getter=self._flag_reader("status", "GPIOFlags", STATE_RIGHT_EDGE),
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
            getter=self._reader("engine", "MicrostepMode", int),
            setter=self._writer("engine", "MicrostepMode"),
            group="Engine",
            description="Microstep division mode",
        )
        self.nominal_voltage = AttrRW(
            int,
            getter=self._reader("engine", "NomVoltage", int),
            setter=self._writer("engine", "NomVoltage"),
            group="Engine",
            description="Nominal motor voltage in tens of mV",
        )
        self.nominal_current = AttrRW(
            int,
            getter=self._reader("engine", "NomCurrent", int),
            setter=self._writer("engine", "NomCurrent"),
            units="mA",
            group="Engine",
            description="Nominal motor current in mA",
        )
        self.nominal_speed = AttrRW(
            int,
            getter=self._reader("engine", "NomSpeed", int),
            setter=self._writer("engine", "NomSpeed"),
            units=STEPS_PER_SECOND,
            group="Engine",
            description="Nominal motor speed in steps/s",
        )

        # --- Power ----------------------------------------------------------
        self.hold_current = AttrRW(
            int,
            getter=self._reader("power", "HoldCurrent", int),
            setter=self._writer("power", "HoldCurrent"),
            units="%",
            group="Power",
            description="Holding current, % of nominal",
        )
        self.current_reduction_delay = AttrRW(
            int,
            getter=self._reader("power", "CurrReductDelay", int),
            setter=self._writer("power", "CurrReductDelay"),
            units="ms",
            group="Power",
            description="Delay before reducing to hold current",
        )
        self.power_off_delay = AttrRW(
            int,
            getter=self._reader("power", "PowerOffDelay", int),
            setter=self._writer("power", "PowerOffDelay"),
            units="s",
            group="Power",
            description="Delay before powering the windings off",
        )
        self.current_set_time = AttrRW(
            int,
            getter=self._reader("power", "CurrentSetTime", int),
            setter=self._writer("power", "CurrentSetTime"),
            units="ms",
            group="Power",
            description="Ramp time when changing current",
        )
        self.current_reduction_enabled = AttrRW(
            bool,
            getter=self._flag_reader("power", "PowerFlags", POWER_REDUCT_ENABLED),
            setter=self._flag_writer("power", "PowerFlags", POWER_REDUCT_ENABLED),
            group="Power",
            description="Reduce to hold current when idle",
        )
        self.power_off_enabled = AttrRW(
            bool,
            getter=self._flag_reader("power", "PowerFlags", POWER_OFF_ENABLED),
            setter=self._flag_writer("power", "PowerFlags", POWER_OFF_ENABLED),
            group="Power",
            description="Power the windings off when idle",
        )
        self.smooth_current_set = AttrRW(
            bool,
            getter=self._flag_reader("power", "PowerFlags", POWER_SMOOTH_CURRENT),
            setter=self._flag_writer("power", "PowerFlags", POWER_SMOOTH_CURRENT),
            group="Power",
            description="Ramp current changes smoothly",
        )

        # --- Feedback -------------------------------------------------------
        self.feedback_type = AttrRW(
            int,
            getter=self._reader("feedback", "FeedbackType", int),
            setter=self._writer("feedback", "FeedbackType"),
            group="Feedback",
            description="Feedback source, 1=encoder 4=EMF 5=none",
        )
        self.encoder_counts_per_turn = AttrRW(
            int,
            getter=self._reader("feedback", "CountsPerTurn", int),
            setter=self._writer("feedback", "CountsPerTurn"),
            group="Feedback",
            description="Encoder counts per motor revolution",
        )
        self.steps_per_turn = AttrRW(
            int,
            getter=self._reader("feedback", "IPS", int),
            setter=self._writer("feedback", "IPS"),
            units=STEPS,
            group="Feedback",
            description="Motor steps per revolution",
        )
        self.encoder_reverse = AttrRW(
            bool,
            getter=self._flag_reader("feedback", "FeedbackFlags", ENC_REVERSE),
            setter=self._flag_writer("feedback", "FeedbackFlags", ENC_REVERSE),
            group="Feedback",
            description="Encoder counts the other way round",
        )

        # --- Protection -----------------------------------------------------
        self.critical_temperature = AttrRW(
            float,
            getter=self._reader("secure", "CriticalT", float, scale=TENTHS_OF_DEGREE),
            setter=self._writer("secure", "CriticalT", scale=TENTHS_OF_DEGREE),
            precision=1,
            units="degC",
            group="Protection",
            description="Alarm above this temperature",
        )
        self.critical_power_voltage = AttrRW(
            float,
            getter=self._reader(
                "secure", "CriticalUpwr", float, scale=TENS_OF_MILLIVOLTS
            ),
            setter=self._writer("secure", "CriticalUpwr", scale=TENS_OF_MILLIVOLTS),
            precision=2,
            units="V",
            group="Protection",
            description="Alarm above this supply voltage",
        )
        self.low_power_voltage_off = AttrRW(
            float,
            getter=self._reader(
                "secure", "LowUpwrOff", float, scale=TENS_OF_MILLIVOLTS
            ),
            setter=self._writer("secure", "LowUpwrOff", scale=TENS_OF_MILLIVOLTS),
            precision=2,
            units="V",
            group="Protection",
            description="Power off below this supply voltage",
        )
        self.critical_power_current = AttrRW(
            int,
            getter=self._reader("secure", "CriticalIpwr", int),
            setter=self._writer("secure", "CriticalIpwr"),
            units="mA",
            group="Protection",
            description="Alarm above this supply current",
        )
        self.critical_usb_voltage = AttrRW(
            float,
            getter=self._reader(
                "secure", "CriticalUusb", float, scale=TENS_OF_MILLIVOLTS
            ),
            setter=self._writer("secure", "CriticalUusb", scale=TENS_OF_MILLIVOLTS),
            precision=2,
            units="V",
            group="Protection",
            description="Alarm above this USB voltage",
        )
        self.minimum_usb_voltage = AttrRW(
            float,
            getter=self._reader(
                "secure", "MinimumUusb", float, scale=TENS_OF_MILLIVOLTS
            ),
            setter=self._writer("secure", "MinimumUusb", scale=TENS_OF_MILLIVOLTS),
            precision=2,
            units="V",
            group="Protection",
            description="Alarm below this USB voltage",
        )
        self.critical_usb_current = AttrRW(
            int,
            getter=self._reader("secure", "CriticalIusb", int),
            setter=self._writer("secure", "CriticalIusb"),
            units="mA",
            group="Protection",
            description="Alarm above this USB current",
        )
        self.alarm_on_overheat = AttrRW(
            bool,
            getter=self._flag_reader("secure", "Flags", ALARM_ON_OVERHEAT),
            setter=self._flag_writer("secure", "Flags", ALARM_ON_OVERHEAT),
            group="Protection",
            description="Alarm when the driver overheats",
        )
        self.low_voltage_protection = AttrRW(
            bool,
            getter=self._flag_reader("secure", "Flags", LOW_UPWR_PROTECTION),
            setter=self._flag_writer("secure", "Flags", LOW_UPWR_PROTECTION),
            group="Protection",
            description="Power off on low supply voltage",
        )
        self.h_bridge_alert = AttrRW(
            bool,
            getter=self._flag_reader("secure", "Flags", H_BRIDGE_ALERT),
            setter=self._flag_writer("secure", "Flags", H_BRIDGE_ALERT),
            group="Protection",
            description="Trip on an H-bridge fault",
        )
        self.alarm_on_limit_misset = AttrRW(
            bool,
            getter=self._flag_reader("secure", "Flags", ALARM_ON_MISSET),
            setter=self._flag_writer("secure", "Flags", ALARM_ON_MISSET),
            group="Protection",
            description="Alarm on swapped limit switches",
        )
        self.sticky_alarm = AttrRW(
            bool,
            getter=self._flag_reader("secure", "Flags", ALARM_FLAGS_STICKING),
            setter=self._flag_writer("secure", "Flags", ALARM_FLAGS_STICKING),
            group="Protection",
            description="Alarm flags latch until cleared",
        )

        # --- Device information ---------------------------------------------
        # A getter with no schedule is read once, when the connection opens,
        # which is all these need - none of them can change.
        self.manufacturer = AttrR(
            str,
            getter=self._reader("device_information", "Manufacturer", str, once=True),
            group="Device",
        )
        self.product_description = AttrR(
            str,
            getter=self._reader(
                "device_information", "ProductDescription", str, once=True
            ),
            group="Device",
        )
        self.controller_name = AttrR(
            str,
            getter=self._reader("controller_name", "ControllerName", str, once=True),
            group="Device",
            description="User-assigned controller name",
        )
        self.stage_name = AttrR(
            str,
            getter=self._reader("stage_name", "PositionerName", str, once=True),
            group="Device",
            description="User-assigned stage name",
        )
        self.serial_number = AttrR(
            int,
            getter=self._read_serial_number,
            group="Device",
            description="Controller serial number",
        )
        self.firmware_version = AttrR(
            str,
            getter=self._read_firmware_version,
            group="Device",
            description="Controller firmware version",
        )
        self.device_uri = AttrR(
            str,
            getter=self._read_device_uri,
            group="Device",
            description="libximc URI of this device",
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

    # --- Attribute IO -------------------------------------------------------

    def _reader(
        self,
        group: str,
        field: str,
        dtype: type,
        *,
        scale: float = 1.0,
        once: bool = False,
    ) -> Getter[Any] | Polled[Any]:
        """A getter for one field of one libximc struct.

        ``group`` is the libximc struct name without its ``_settings_t`` suffix,
        so ``("move", "Speed")`` reads ``get_move_settings().Speed``. The
        read-only groups in `READ_ONLY_GROUPS` are read with ``get_<group>()``.

        Args:
            group: The libximc struct to read
            field: The field of that struct
            dtype: The datatype of the attribute being read into
            scale: Divisor applied on read, for raw device units
            once: Whether to read once on connect rather than at the poll period

        """

        async def read() -> Any:
            raw = await self.connection.read_field(group, field)
            return _to_datatype(raw, dtype, scale)

        return read if once else Polled(read, period=self._period)

    def _writer(self, group: str, field: str, *, scale: float = 1.0) -> Setter[Any]:
        """A setter for one field of one libximc settings struct.

        Args:
            group: The libximc struct to write
            field: The field of that struct
            scale: Multiplier applied on write, for raw device units

        """

        async def write(value: Any) -> None:
            await self.connection.write_field(group, field, _to_device(value, scale))

        return write

    def _flag_reader(self, group: str, field: str, bit: enum.Flag) -> Polled[bool]:
        """A getter for a single flag of a bitmask field, read alone."""
        mask = _bit_mask(bit)

        async def read() -> bool:
            raw = await self.connection.read_field(group, field)
            return bool(int(raw) & mask)

        return Polled(read, period=self._period)

    def _flag_writer(self, group: str, field: str, bit: enum.Flag) -> Setter[bool]:
        """A setter for a single flag of a bitmask field, written alone."""
        mask = _bit_mask(bit)

        async def write(value: bool) -> None:
            await self.connection.write_bit(group, field, mask, bool(value))

        return write

    async def _read_serial_number(self) -> int:
        return await self.connection.call(lambda axis: axis.get_serial_number())

    async def _read_firmware_version(self) -> str:
        info = await self.connection.call(lambda axis: axis.get_device_information())
        return f"{info.Major}.{info.Minor}.{info.Release}"

    async def _read_device_uri(self) -> str:
        return self.connection.uri

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


def _to_datatype(raw: Any, dtype: type, scale: float) -> Any:
    """Coerce a raw libximc value to the attribute's datatype.

    libximc returns ``IntEnum`` and ``Flag`` members for bitmask and mode
    fields; ``int()``/``float()`` reduce those to their numeric value.
    """
    if dtype is bool:
        return bool(raw)
    if dtype is str:
        return str(raw)
    if dtype is int:
        return int(int(raw) / scale) if scale != 1.0 else int(raw)
    if dtype is float:
        return float(raw) / scale
    return raw


def _to_device(value: Any, scale: float) -> Any:
    """Convert an attribute value back into raw device units."""
    if scale != 1.0 and isinstance(value, int | float):
        return int(round(value * scale))
    return value


def _bit_mask(bit: enum.Flag) -> int:
    """Reduce a libximc flag member to its raw mask.

    libximc's flags derive from a ``StrictIntFlag`` that is not an ``int``
    subclass, so the underlying value has to be taken explicitly.
    """
    return int(bit.value)
