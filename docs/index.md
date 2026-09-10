# fastcs-ximc

FastCS support for motion controllers driven by the
[libximc](https://libximc.xisupport.com/) library. One `XimcController`
instance drives one device.

## Configuration

Each entry under `controllers:` in `fastcs.yaml` configures one device. The
device is addressed by a `XimcConnection` declared in that entry's
`connections:` block, under the role name `ximc`:

```yaml
controllers:
  - id: AXIS1
    type: fastcs_ximc.XimcController
    poll_period: 0.2
    connections:
      ximc:
        type: fastcs_ximc.XimcConnection
        settings:
          uri: xi-com:///dev/ttyACM0
```

Connection `settings:`

| Field | Default | Description |
|---|---|---|
| `uri` | - | Full libximc URI, e.g. `xi-com:///dev/ttyACM0` |
| `port` | - | Serial device path; shorthand for `xi-com://<port>` |
| `port_env` | - | Environment variable holding the serial device path |

Exactly one of `uri`, `port` or `port_env` must be given. `reconnect_period`
and `reconnect_attempts` are FastCS `Connection` arguments and may be given
alongside `settings:`.

Controller options

| Field | Default | Description |
|---|---|---|
| `poll_period` | `0.2` | Seconds between reads of the device |

### URI schemes

| Scheme | Example | Use |
|---|---|---|
| `xi-com://` | `xi-com:///dev/ttyACM0` | Directly attached serial controller |
| `xi-net://` | `xi-net://192.168.0.1/00000001` | Controller behind an XiLab network server |
| `xi-udp://` | `xi-udp://192.168.0.1` | Controller reached over UDP |
| `xi-emu://` | `xi-emu:///tmp/sim/axis.bin` | libximc virtual device, no hardware needed |

For `xi-emu://`, libximc creates the backing `.bin` file with default settings
on first open; the driver creates its parent directory.

## Attributes

Read-only attributes are marked `R`, read-write `RW`, write-only `W`.

Attributes with a libximc struct behind them are read back at `poll_period` and
written straight through. **Soft attributes** - marked *soft* below - hold their
value in software only and never touch the device; they are all writable so an
autosave layer can restore them across a restart.

### Position

| Attribute | Mode | Description |
|---|---|---|
| `position` | R | Current position in steps |
| `position_microsteps` | R | Fractional part of the position, in microsteps |
| `encoder_position` | R | Position as reported by the encoder |
| `position_demand` | W | Move to this absolute position, in steps |
| `relative_move` | W | Move this many steps from the current position |
| `user_position` | R | *soft* - position in engineering units |
| `user_demand` | W | *soft* - move to this position in engineering units |
| `marked_position` | RW | *soft* - marked position, in steps |

The `mark_position` command copies the live position into `marked_position` and
`move_to_mark` moves back there. Nothing is written to the device.

### Units

Soft attributes implementing the EPICS motor record's dial-to-user transform.
The device's own step count is the dial position, and

```
user = dial * motor_resolution + user_offset
```

| Attribute | Mode | Default | Description |
|---|---|---|---|
| `egu` | RW | `steps` | Engineering unit name, e.g. `mm` |
| `motor_resolution` | RW | `1.0` | Engineering units per step (`MRES`) |
| `user_offset` | RW | `0.0` | Offset from dial to user position (`OFF`) |

`user_position` recomputes whenever the position, the resolution or the offset
changes, and writing `egu` pushes the new unit name onto the units metadata of
`user_position`, `user_demand`, `user_offset` and `motor_resolution`.

This is deliberately done in software rather than through libximc's `_calb`
calls: those need a calibration struct threaded through every getter, apply only
to the fields libximc chooses, and would leave the user offset to be implemented
here anyway.

### Motion

`speed`, `acceleration`, `deceleration` and `antiplay_speed` (RW) map onto the
libximc `move_settings_t` struct.

| Attribute | Mode | Description |
|---|---|---|
| `tweak_step` | RW | *soft* - step size for `tweak_forward`/`tweak_reverse` |
| `motion_inhibit` | RW | *soft* - reject every move request while set |

In motor record vocabulary, jog is the continuous move that runs until it is
stopped (`JOGF`/`JOGR`) and tweak is the single relative nudge (`TWF`/`TWR` of
`TWV` steps). CNC pendants use "jog" for both, so the command descriptions say
"until stopped" explicitly.

`motion_inhibit` is a software interlock. While it is set, every move - the
demands, the tweak and jog commands, `move_to_mark`, `loft` and both homing
commands - raises `MotionInhibitedError` and does not reach the device. FastCS
logs a setter that raises rather than propagating it, so a write to
`position_demand`, `relative_move` or `user_demand` is rejected silently from
the client's point of view; the commands raise to their caller.

### Limits

The `edges_settings_t` struct: soft travel limits and limit switch wiring. All
RW. The limits are in steps, or in encoder counts if `limits_use_encoder` is
set.

libximc names the two directions "left" and "right" (`LeftBorder`,
`command_left`). A stage may be vertical or rotary, so this driver names them
after the position count instead: **forward**/**high** for increasing steps,
**reverse**/**low** for decreasing. `switch_1`/`switch_2` keep libximc's
numbering, because they are properties of a physical input rather than of an end
of travel - `swap_limit_switches` changes which end each one guards.

| Attribute | Description |
|---|---|
| `low_limit` / `high_limit` | Soft limit in each direction |
| `stop_at_low_limit` / `stop_at_high_limit` | Stop when that soft limit is hit |
| `limits_use_encoder` | Soft limits are in encoder counts, not steps |
| `swap_limit_switches` | Limit switches are wired swapped |
| `switch_1_active_low` / `switch_2_active_low` | Polarity of each switch input |

### Homing

The `home_settings_t` struct, tuning the `home` and `home_and_zero` commands.
All RW.

| Attribute | Description |
|---|---|
| `home_fast_speed` | Speed of the first, fast homing move, steps/s |
| `home_slow_speed` | Speed of the second, precise homing move, steps/s |
| `home_offset` | Steps to move away after the switch is found |
| `home_forward_first` | First homing move goes forward rather than reverse |
| `home_second_move` | Do the second, precise move at all |
| `home_use_fast_algorithm` | Use libximc's fast homing algorithm |
| `home_flags` | Raw `HomeFlags` bitmask, for the bits not broken out above |

### Status

All read-only. The booleans are single bits of the device status words, and the
electrical readings are scaled from libximc's raw units.

| Attribute | Description |
|---|---|
| `moving` | A move command is running |
| `move_command_error` | The last move command failed |
| `current_speed` | Speed the motor is actually moving at, steps/s |
| `at_low_limit` / `at_high_limit` | That end's limit switch is active |
| `homed` | Device has completed a homing move |
| `alarm` | Device is in an alarm state |
| `status_flags` | Raw device state bitmask |
| `following_error` | *soft* - `position` minus `encoder_position`, in steps |
| `temperature` | Controller temperature, °C |
| `power_voltage` / `power_current` | Power supply, V and mA |
| `usb_voltage` / `usb_current` | USB supply, V and mA |

Note that `current_speed` is the measured speed, whereas `speed` under Motion is
the configured target.

### Engine

`microstep_mode`, `nominal_voltage`, `nominal_current` and `nominal_speed` (RW)
map onto the libximc `engine_settings_t` struct.

### Power

The `power_settings_t` struct, controlling what the windings do when the axis is
idle. All RW. `power_off` under Commands does the same thing immediately.

| Attribute | Description |
|---|---|
| `hold_current` | Holding current as a percentage of nominal |
| `current_reduction_enabled` | Drop to `hold_current` when idle |
| `current_reduction_delay` | Delay before that reduction, ms |
| `power_off_enabled` | Power the windings off entirely when idle |
| `power_off_delay` | Delay before powering off, s |
| `smooth_current_set` / `current_set_time` | Ramp current changes, and over how long |

### Feedback

The `feedback_settings_t` struct. Without these, `encoder_position` is an
uninterpretable count. All RW.

| Attribute | Description |
|---|---|
| `feedback_type` | Feedback source: 1 = encoder, 4 = EMF, 5 = none |
| `encoder_counts_per_turn` | Encoder counts per motor revolution |
| `steps_per_turn` | Motor steps per revolution (libximc `IPS`) |
| `encoder_reverse` | Encoder counts in the opposite sense to the motor |

### Protection

The `secure_settings_t` struct: the thresholds behind the `alarm` status bit.
All RW, and the voltages and the temperature are in engineering units, not
libximc's raw counts.

| Attribute | Description |
|---|---|
| `critical_temperature` | Alarm above this controller temperature, °C |
| `critical_power_voltage` | Alarm above this supply voltage, V |
| `low_power_voltage_off` | Power off below this supply voltage, V |
| `critical_power_current` | Alarm above this supply current, mA |
| `critical_usb_voltage` / `minimum_usb_voltage` | USB voltage window, V |
| `critical_usb_current` | Alarm above this USB current, mA |
| `alarm_on_overheat` | Raise an alarm when the driver overheats |
| `low_voltage_protection` | Power off on a low supply voltage |
| `h_bridge_alert` | Trip on an H-bridge fault |
| `alarm_on_limit_misset` | Alarm when the limit switches look swapped |
| `sticky_alarm` | Alarm flags latch until they are read |

### Device

Read once at startup rather than polled, except `axis_description`.

| Attribute | Mode | Description |
|---|---|---|
| `manufacturer` | R | Device manufacturer |
| `product_description` | R | Product description |
| `serial_number` | R | Controller serial number |
| `firmware_version` | R | Controller firmware version, `major.minor.release` |
| `controller_name` | R | User-assigned controller name, from the device |
| `stage_name` | R | User-assigned stage name, from the device |
| `device_uri` | R | libximc URI of this device |
| `axis_description` | RW | *soft* - label for this axis, like the motor record's `DESC` |

## Multiple axes

libximc has no multi-axis concept: `Axis(uri)` is its only device class, one URI
addresses exactly one axis, and every settings struct is per-axis. Multi-axis
hardware is several single-axis controller boards sharing an enclosure, and each
board enumerates as its own URI.

So an axis is a controller entry, and a multi-axis system is several of them:

```yaml
controllers:
  - id: STAGE-X
    type: fastcs_ximc.XimcController
    connections:
      ximc:
        type: fastcs_ximc.XimcConnection
        settings:
          uri: xi-com:///dev/ttyACM0
  - id: STAGE-Y
    type: fastcs_ximc.XimcController
    connections:
      ximc:
        type: fastcs_ximc.XimcConnection
        settings:
          uri: xi-com:///dev/ttyACM1
```

Connection role names are local to an entry, so both axes claim `ximc` and get
their own connection. Each entry gets its own device handle, its own lock, its
own reconnect budget and its own PV prefix. Since libximc offers no shared
handle, there is nothing for the axes to contend over.

The `Device` attributes above make each axis identifiable from the control
system, so a mis-ordered port shows up as the wrong serial number or stage name
against a PV prefix rather than as silently swapped axes.

## Commands

| Command | Description |
|---|---|
| `stop` | Stop immediately, ignoring the deceleration ramp |
| `soft_stop` | Stop using the configured deceleration ramp |
| `jog_forward` / `jog_reverse` | Move continuously until stopped |
| `tweak_forward` / `tweak_reverse` | Move by `tweak_step` in either direction |
| `loft` | Backlash compensation move |
| `home` | Move to the home position |
| `home_and_zero` | Home, then zero the resulting position |
| `zero` | Set the current position to zero |
| `mark_position` | Copy the current position into `marked_position` |
| `move_to_mark` | Move back to the marked position |
| `power_off` | Remove holding current from the windings |
| `save_settings_to_flash` | Persist current settings to controller flash |
| `read_settings_from_flash` | Reload settings from controller flash |

## Implementation notes

**Attributes carry their own IO.** Each attribute backed by the device gets a
getter, and a setter if it is writable, built by `XimcController._reader` and
`._writer` from a `(group, field)` pair: `("move", "Speed")` reads
`get_move_settings().Speed` and writes it back through `set_move_settings`. The
groups in `READ_ONLY_GROUPS` use `get_<group>()` instead. Writes are
read-modify-write because libximc rejects a partially populated struct.
`._flag_reader`/`._flag_writer` read and write one flag of a bitmask field,
leaving the other flags in the field untouched. A getter wrapped in `Polled` is
read at `poll_period`; a bare getter - the `Device` group - is read once, when
the connection opens.

**Soft attributes are attributes without a getter or setter.** A soft `AttrRW`
pushes a write straight to its own readback - which is exactly the marked
position, the unit transform, the tweak step and the inhibit. Derived values
(`user_position`, `following_error`) are `AttrR` recomputed from an
`add_readback_callback` on each of their inputs, so they follow the poll loop
without adding device traffic.

**The connection owns the device handle.** `XimcConnection` is a FastCS
`Connection`, so opening it, reopening it after a failure and closing it at
shutdown are the `ControllerRunner`'s job, not the controller's. Every libximc
call is a blocking ctypes call over a serial link, so the connection dispatches
them to a worker thread behind a lock — the device handle is not safe for
concurrent use.

**Only a dead link marks the connection down.** libximc raises `ConnectionError`
when the device must be reopened and `ValueError` when it rejects a parameter,
so `XimcConnection.call` calls `set_disconnected()` for the first and lets the
second through untouched. Scans on the controller are gated while the connection
is down, and the runner's reconnect task reopens it.

**Strict flag enums are patched at open.** libximc models bitmask fields as
`enum.Flag` with `STRICT` boundary, so a single undocumented bit from real
hardware makes the whole `get_*_settings` call raise. `patch_strict_flags()`
switches every flag class to `CONFORM`, which drops undefined bits instead.
`tests/test_utils.py::test_flags_reject_unknown_bits_without_patch` is a canary
for this being fixed upstream.

## Not yet exposed

libximc has around twenty settings structs; the ones above are the ones an axis
needs to run. Still unexposed, roughly in order of how likely they are to be
wanted: `brake_settings` (external brake timing), `ctp_settings` (position
slipping detection), `pid_settings` (DC and BLDC tuning), `entype_settings`
(motor and driver type), `sync_in_settings`/`sync_out_settings` (hardware
triggering), `extio_settings`, `joystick_settings`, `uart_settings`,
`emf_settings` and `engine_advanced_setup`. On the read side: `EncSts` and
`WindingStatus`, the `STATE_ERRC`/`ERRD`/`ERRV` error bits and the remaining
GPIO bits (brake, reverse sensor, sync in/out, buttons), plus `get_chart_data`
and `get_measurements` for current and voltage traces.

## Development

```
tox -p              # pre-commit, pyright and tests
pytest              # tests only - they run against a xi-emu:// virtual device
```

If the controller options or the connection settings change, regenerate the
config schema:

```
python -m fastcs_ximc schema > schema.json
```
