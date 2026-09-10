"""Tests driving a real libximc virtual device - no mocks."""

import asyncio
from pathlib import Path

import libximc.highlevel as ximc
import pytest

from fastcs_ximc import (
    MotionInhibitedError,
    XimcController,
    XimcDevice,
    XimcOptions,
)

pytestmark = pytest.mark.asyncio

EPICS_DESC_MAX_LENGTH = 40
"""Maximum length of an EPICS record DESC field."""


async def _refresh(attr):
    """Trigger one IO read of an attribute, as the scan loop would."""
    await attr.bind_update_callback()()


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    """Poll ``predicate`` until it is true, to avoid racing simulated motion."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"Condition not met within {timeout}s")


class TestXimcDevice:
    async def test_open_creates_state_file(self, device_uri):
        path = Path(device_uri.removeprefix("xi-emu://"))
        assert not path.exists()

        device = XimcDevice(device_uri)
        await device.open()
        try:
            assert path.exists()
            assert device.is_open
        finally:
            await device.close()

        assert not device.is_open

    async def test_axis_raises_before_open(self, device_uri):
        with pytest.raises(RuntimeError, match="is not open"):
            _ = XimcDevice(device_uri).axis

    async def test_close_before_open_is_a_no_op(self, device_uri):
        """`XimcController.connect` closes first; that must be free when unopened."""
        device = XimcDevice(device_uri)

        await device.close()

        assert not device.is_open

    async def test_open_is_idempotent(self, device):
        handle = device.axis
        await device.open()
        assert device.axis is handle

    async def test_read_field(self, device):
        assert await device.read_field("position", "Position") == 0
        assert await device.read_field("move", "Speed") == 1000

    async def test_write_field_round_trips(self, device):
        await device.write_field("move", "Speed", 750)
        assert await device.read_field("move", "Speed") == 750

    async def test_write_field_preserves_other_fields(self, device):
        """A write must read-modify-write; libximc rejects partial structs."""
        accel = await device.read_field("move", "Accel")

        await device.write_field("move", "Speed", 321)

        assert await device.read_field("move", "Accel") == accel
        assert await device.read_field("move", "Speed") == 321

    async def test_call_runs_against_the_handle(self, device):
        position = await device.call(lambda axis: axis.get_position())
        assert position.Position == 0


class TestAttributes:
    async def test_position_reads_zero_at_startup(self, controller):
        await _refresh(controller.position)
        assert controller.position.get() == 0

    async def test_speed_round_trips_through_hardware(self, controller):
        await controller.speed.put(600)
        await _refresh(controller.speed)
        assert controller.speed.get() == 600

    async def test_temperature_is_scaled_to_degrees(self, controller):
        raw = await controller.device.read_field("status", "CurT")
        await _refresh(controller.temperature)
        assert controller.temperature.get() == pytest.approx(raw / 10)

    async def test_power_voltage_is_scaled_to_volts(self, controller):
        """Upwr is in tens of mV. It drifts between reads, so allow tolerance."""
        raw = await controller.device.read_field("status", "Upwr")
        await _refresh(controller.power_voltage)

        volts = controller.power_voltage.get()
        assert volts == pytest.approx(raw / 100, abs=0.5)
        assert 1.0 < volts < 100.0  # a voltage, not a raw count

    async def test_bit_attribute_masks_a_single_flag(self, controller):
        await _refresh(controller.moving)
        assert controller.moving.get() is False

    async def test_device_information_is_read(self, controller):
        await _refresh(controller.manufacturer)
        assert controller.manufacturer.get() == "XIMC"

    async def test_device_uri_is_reported(self, controller, device_uri):
        assert controller.device_uri.get() == device_uri


class TestMotion:
    async def test_absolute_move_changes_position(self, controller):
        await controller.position_demand.put(2000)

        async def arrived() -> bool:
            return await controller.device.read_field("position", "Position") == 2000

        await _wait_until(arrived)

    async def test_relative_move_changes_position(self, controller):
        await controller.position_demand.put(500)
        await _wait_until(
            lambda: _position_is(controller, 500),
        )

        await controller.relative_move.put(250)
        await _wait_until(
            lambda: _position_is(controller, 750),
        )

    async def test_moving_is_true_during_a_move(self, controller):
        await controller.position_demand.put(100000)

        async def is_moving() -> bool:
            await _refresh(controller.moving)
            return controller.moving.get()

        await _wait_until(is_moving)

    async def test_stop_halts_a_move(self, controller):
        await controller.position_demand.put(100000)
        await _wait_until(
            lambda: _position_exceeds(controller, 0),
        )

        await controller.stop()

        async def stopped() -> bool:
            status = await controller.device.call(lambda axis: axis.get_status())
            return not (status.MvCmdSts & ximc.MvcmdStatus.MVCMD_RUNNING)

        await _wait_until(stopped)

    async def test_zero_resets_the_position(self, controller):
        await controller.position_demand.put(1000)
        await _wait_until(lambda: _position_is(controller, 1000))

        await controller.zero()

        assert await controller.device.read_field("position", "Position") == 0


class TestMarkedPosition:
    """A soft attribute and its commands - nothing here touches the device."""

    async def test_marked_position_starts_at_zero(self, controller):
        assert controller.marked_position.get() == 0

    async def test_mark_captures_the_current_position(self, controller):
        await controller.position_demand.put(1500)
        await _wait_until(lambda: _position_is(controller, 1500))

        await controller.mark_position()

        assert controller.marked_position.get() == 1500

    async def test_mark_does_not_move_the_stage(self, controller):
        await controller.mark_position()
        assert await controller.device.read_field("position", "Position") == 0

    async def test_move_to_mark_returns_to_the_mark(self, controller):
        await controller.position_demand.put(800)
        await _wait_until(lambda: _position_is(controller, 800))
        await controller.mark_position()

        await controller.position_demand.put(0)
        await _wait_until(lambda: _position_is(controller, 0))
        await controller.move_to_mark()

        await _wait_until(lambda: _position_is(controller, 800))

    async def test_marked_position_can_be_written_directly(self, controller):
        """Autosave restores the mark by putting to it, without hardware."""
        await controller.marked_position.put(4242)
        assert controller.marked_position.get() == 4242


class TestBitFields:
    """Single flag bits of a settings struct are read and written in isolation."""

    async def test_bit_round_trips(self, controller):
        await _refresh(controller.stop_at_low_limit)
        original = controller.stop_at_low_limit.get()

        await controller.stop_at_low_limit.put(not original)
        await _refresh(controller.stop_at_low_limit)

        assert controller.stop_at_low_limit.get() is (not original)

    async def test_writing_one_bit_leaves_its_neighbours_alone(self, controller):
        """The two stop flags share the BorderFlags field."""
        await controller.stop_at_high_limit.put(True)
        await controller.stop_at_low_limit.put(False)

        await _refresh(controller.stop_at_high_limit)
        assert controller.stop_at_high_limit.get() is True

    async def test_writing_one_bit_leaves_the_rest_of_the_struct_alone(
        self, controller
    ):
        await controller.low_limit.put(-3000)

        await controller.limits_use_encoder.put(True)

        assert await controller.device.read_field("edges", "LeftBorder") == -3000


class TestSettingsGroups:
    """The settings structs added beyond position, move, status and engine."""

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("low_limit", -5000),
            ("high_limit", 5000),
            ("home_fast_speed", 900),
            ("home_slow_speed", 15),
            ("home_offset", 250),
            ("hold_current", 40),
            ("power_off_delay", 120),
            ("encoder_counts_per_turn", 4000),
            ("steps_per_turn", 200),
            ("critical_power_current", 2500),
        ],
    )
    async def test_setting_round_trips_through_hardware(self, controller, name, value):
        attr = getattr(controller, name)
        await attr.put(value)
        await _refresh(attr)
        assert attr.get() == value

    async def test_scaled_setting_round_trips_in_engineering_units(self, controller):
        """CriticalT is in tenths of a degree on the wire."""
        await controller.critical_temperature.put(75.0)

        assert await controller.device.read_field("secure", "CriticalT") == 750
        await _refresh(controller.critical_temperature)
        assert controller.critical_temperature.get() == pytest.approx(75.0)

    async def test_home_flags_expose_both_raw_and_bit_views(self, controller):
        await controller.home_forward_first.put(True)

        await _refresh(controller.home_flags)
        assert controller.home_flags.get() & ximc.HomeFlags.HOME_DIR_FIRST.value


class TestEngineeringUnits:
    """The dial-to-user transform, held entirely in software."""

    async def test_defaults_are_a_one_to_one_step_mapping(self, controller):
        assert controller.egu.get() == "steps"
        assert controller.motor_resolution.get() == 1.0
        assert controller.user_offset.get() == 0.0
        assert controller.user_position.datatype.units == "steps"
        assert controller.motor_resolution.datatype.units == "steps/step"

    async def test_user_position_follows_the_dial_position(self, controller):
        await controller.motor_resolution.put(0.001)
        await controller.user_offset.put(5.0)

        await controller.position_demand.put(2000)
        await _wait_until(lambda: _position_is(controller, 2000))
        await _refresh(controller.position)

        assert controller.user_position.get() == pytest.approx(7.0)

    async def test_user_position_recomputes_when_the_transform_changes(
        self, controller
    ):
        await _refresh(controller.position)
        await controller.motor_resolution.put(0.01)
        await controller.user_offset.put(1.5)

        assert controller.user_position.get() == pytest.approx(1.5)

    async def test_user_demand_moves_in_engineering_units(self, controller):
        await controller.motor_resolution.put(0.001)
        await controller.user_offset.put(1.0)

        await controller.user_demand.put(2.5)

        await _wait_until(lambda: _position_is(controller, 1500))

    async def test_conversion_round_trips(self, controller):
        await controller.motor_resolution.put(0.002)
        await controller.user_offset.put(-3.0)

        assert controller.to_steps(controller.to_user(1234)) == 1234

    async def test_zero_resolution_is_rejected(self, controller):
        await controller.motor_resolution.put(0.0)

        with pytest.raises(ValueError, match="motor_resolution is zero"):
            controller.to_steps(1.0)

    async def test_egu_name_propagates_to_the_datatypes(self, controller):
        await controller.egu.put("mm")

        assert controller.user_position.datatype.units == "mm"
        assert controller.user_demand.datatype.units == "mm"
        assert controller.user_offset.datatype.units == "mm"
        assert controller.motor_resolution.datatype.units == "mm/step"


class TestMotionInhibit:
    async def test_moves_are_rejected_while_inhibited(self, controller):
        await controller.motion_inhibit.put(True)

        with pytest.raises(MotionInhibitedError):
            await controller.move_to_mark()

        assert await controller.device.read_field("position", "Position") == 0

    async def test_demands_are_rejected_while_inhibited(self, controller):
        await controller.motion_inhibit.put(True)

        with pytest.raises(MotionInhibitedError):
            await controller.position_demand.put(1000)

        assert await controller.device.read_field("position", "Position") == 0

    async def test_stop_still_works_while_inhibited(self, controller):
        await controller.motion_inhibit.put(True)
        await controller.stop()

    async def test_clearing_the_inhibit_allows_motion_again(self, controller):
        await controller.motion_inhibit.put(True)
        await controller.motion_inhibit.put(False)

        await controller.position_demand.put(600)

        await _wait_until(lambda: _position_is(controller, 600))


class TestTweak:
    async def test_tweak_forward_moves_by_the_step(self, controller):
        await controller.tweak_step.put(300)

        await controller.tweak_forward()

        await _wait_until(lambda: _position_is(controller, 300))

    async def test_tweak_reverse_moves_back_by_the_step(self, controller):
        await controller.tweak_step.put(300)
        await controller.tweak_forward()
        await _wait_until(lambda: _position_is(controller, 300))

        await controller.tweak_reverse()

        await _wait_until(lambda: _position_is(controller, 0))


class TestDirection:
    """libximc's left is decreasing steps, its right increasing."""

    async def test_jog_forward_increases_the_position(self, controller):
        await controller.jog_forward()
        await _wait_until(lambda: _position_exceeds(controller, 0))
        await controller.stop()

    async def test_jog_reverse_decreases_the_position(self, controller):
        await controller.jog_reverse()

        async def gone_negative() -> bool:
            return await controller.device.read_field("position", "Position") < 0

        await _wait_until(gone_negative)
        await controller.stop()


class TestFollowingError:
    async def test_following_error_tracks_the_encoder(self, controller):
        await _refresh(controller.position)
        await _refresh(controller.encoder_position)

        expected = controller.position.get() - controller.encoder_position.get()
        assert controller.following_error.get() == expected


class TestLifecycle:
    async def test_scan_tasks_are_gated_on_a_successful_open(self, options):
        """_connected must only be set once the device is actually open."""
        controller = XimcController(options)
        await controller.initialise()
        controller.post_initialise()
        assert not controller._connected

        await controller.connect()

        assert controller._connected
        assert controller.device.is_open
        await controller.disconnect()

    async def test_connect_can_be_called_again(self, options):
        """A reconnect loop calls connect() repeatedly, not disconnect+connect."""
        controller = XimcController(options)
        await controller.connect()
        handle = controller.device.axis

        await controller.connect()

        assert controller.device.is_open
        assert controller.device.axis is not handle  # a fresh handle, not the stale one
        assert controller._connected
        await controller.disconnect()

    async def test_connect_survives_a_handle_that_will_not_close(self, options):
        """A dead link may fail to close; that must not block reopening."""
        controller = XimcController(options)
        await controller.connect()

        async def _raise() -> None:
            raise OSError("device went away")

        monkeypatched = controller.device.close
        controller.device.close = _raise  # type: ignore[method-assign]
        try:
            await controller.connect()
        finally:
            controller.device.close = monkeypatched  # type: ignore[method-assign]

        assert controller.device.is_open
        await controller.disconnect()

    async def test_identity_is_read_during_connect(self, options):
        controller = XimcController(options)

        await controller.connect()

        assert isinstance(controller.serial_number.get(), int)
        assert controller.firmware_version.get().count(".") == 2
        assert controller.device_uri.get() == options.device_uri
        await controller.disconnect()

    async def test_missing_real_device_is_reported_on_connect(self, monkeypatch):
        monkeypatch.setattr(
            "fastcs_ximc.utils.enumerate_device_uris",
            lambda: ["xi-com:///dev/ttyACM1"],
        )
        controller = XimcController(XimcOptions(port="/dev/ttyACM0"))

        with pytest.raises(Exception, match="ttyACM0"):
            await controller.connect()

    async def test_disconnect_closes_the_device(self, options):
        controller = XimcController(options)
        await controller.connect()
        assert controller.device.is_open

        await controller.disconnect()
        assert not controller.device.is_open

    async def test_every_io_ref_resolves_to_a_real_libximc_field(self, controller):
        """Guards against typos in the (group, field) table."""
        for name, attr in controller.attributes.items():
            if not attr.has_io_ref():
                continue
            ref = attr.io_ref
            value = await controller.device.read_field(ref.group, ref.field)
            assert value is not None, f"{name} -> {ref.group}.{ref.field}"


async def _position_is(controller: XimcController, expected: int) -> bool:
    return await controller.device.read_field("position", "Position") == expected


async def _position_exceeds(controller: XimcController, threshold: int) -> bool:
    position = await controller.device.read_field("position", "Position")
    assert isinstance(position, int)
    return position > threshold


class TestEpicsLimits:
    """EPICS record DESC fields are capped, and overflow only fails at IOC start."""

    async def test_attribute_descriptions_fit_epics_desc(self, controller):
        for name, attr in controller.attributes.items():
            if attr.description is not None:
                assert len(attr.description) <= EPICS_DESC_MAX_LENGTH, name

    async def test_command_docstrings_fit_epics_desc(self, controller):
        for name, method in controller.command_methods.items():
            if method.docstring is not None:
                assert len(method.docstring) <= EPICS_DESC_MAX_LENGTH, name


class TestStatusReadback:
    async def test_current_speed_is_zero_at_rest(self, controller):
        await _refresh(controller.current_speed)
        assert controller.current_speed.get() == 0

    async def test_current_speed_is_nonzero_while_moving(self, controller):
        """The readback tracks real motion, not the configured setpoint."""
        await controller.position_demand.put(100000)

        async def is_moving_at_speed() -> bool:
            await _refresh(controller.current_speed)
            return controller.current_speed.get() > 0

        await _wait_until(is_moving_at_speed)
        await controller.stop()

    async def test_limit_switches_are_inactive_on_a_free_axis(self, controller):
        await _refresh(controller.at_low_limit)
        await _refresh(controller.at_high_limit)
        assert controller.at_low_limit.get() is False
        assert controller.at_high_limit.get() is False

    async def test_usb_current_is_read(self, controller):
        await _refresh(controller.usb_current)
        assert controller.usb_current.get() > 0


class TestIdentity:
    """Identity is read once at startup, so each axis is distinguishable."""

    async def test_serial_number_is_populated(self, controller):
        assert isinstance(controller.serial_number.get(), int)

    async def test_firmware_version_is_populated(self, controller):
        version = controller.firmware_version.get()
        assert version.count(".") == 2, version

    async def test_controller_and_stage_names_are_readable(self, controller):
        await _refresh(controller.controller_name)
        await _refresh(controller.stage_name)
        assert isinstance(controller.controller_name.get(), str)
        assert isinstance(controller.stage_name.get(), str)
