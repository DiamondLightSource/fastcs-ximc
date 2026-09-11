"""Tests driving a real libximc virtual device - no mocks."""

import asyncio

import libximc.highlevel as ximc
import pytest
from fastcs.attributes import AttrR
from fastcs.connections import Connections
from fastcs.controllers import ControllerRunner

from fastcs_ximc import (
    MotionInhibitedError,
    XimcConnection,
    XimcController,
)

pytestmark = pytest.mark.asyncio

EPICS_DESC_MAX_LENGTH = 40
"""Maximum length of an EPICS record DESC field."""


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    """Poll ``predicate`` until it is true, to avoid racing simulated motion."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"Condition not met within {timeout}s")


class TestAttributes:
    async def test_position_reads_zero_at_startup(self, controller):
        await controller.position.poll()
        assert controller.position.readback == 0

    async def test_speed_round_trips_through_hardware(self, controller):
        await controller.speed.set(600)
        await controller.speed.poll()
        assert controller.speed.readback == 600

    async def test_temperature_is_scaled_to_degrees(self, controller):
        raw = await controller.connection.read_field("status", "CurT")
        await controller.temperature.poll()
        assert controller.temperature.readback == pytest.approx(raw / 10, abs=0.5)

    async def test_power_voltage_is_scaled_to_volts(self, controller):
        """Upwr is in tens of mV. It drifts between reads, so allow tolerance."""
        raw = await controller.connection.read_field("status", "Upwr")
        await controller.power_voltage.poll()

        volts = controller.power_voltage.readback
        assert volts == pytest.approx(raw / 100, abs=0.5)
        assert 1.0 < volts < 100.0  # a voltage, not a raw count

    async def test_bit_attribute_masks_a_single_flag(self, controller):
        await controller.moving.poll()
        assert controller.moving.readback is False

    async def test_device_information_is_read(self, controller):
        assert controller.manufacturer.readback == "XIMC"

    async def test_device_uri_is_reported(self, controller, device_uri):
        assert controller.device_uri.readback == device_uri

    async def test_every_getter_resolves_to_a_real_libximc_field(self, controller):
        """Guards against typos in the (group, field) table."""
        for name, attr in controller.attributes.items():
            if isinstance(attr, AttrR) and attr.has_getter():
                assert await attr.poll() is not None, name


class TestMotion:
    async def test_absolute_move_changes_position(self, controller):
        await controller.position_demand.set(2000)

        await _wait_until(lambda: _position_is(controller, 2000))

    async def test_relative_move_changes_position(self, controller):
        await controller.position_demand.set(500)
        await _wait_until(lambda: _position_is(controller, 500))

        await controller.relative_move.set(250)
        await _wait_until(lambda: _position_is(controller, 750))

    async def test_moving_is_true_during_a_move(self, controller):
        await controller.position_demand.set(100000)

        async def is_moving() -> bool:
            await controller.moving.poll()
            return controller.moving.readback

        await _wait_until(is_moving)

    async def test_stop_halts_a_move(self, controller):
        await controller.position_demand.set(100000)
        await _wait_until(lambda: _position_exceeds(controller, 0))

        await controller.stop()

        async def stopped() -> bool:
            status = await controller.connection.call(lambda axis: axis.get_status())
            return not (status.MvCmdSts & ximc.MvcmdStatus.MVCMD_RUNNING)

        await _wait_until(stopped)

    async def test_zero_resets_the_position(self, controller):
        await controller.position_demand.set(1000)
        await _wait_until(lambda: _position_is(controller, 1000))

        await controller.zero()

        assert await controller.connection.read_field("position", "Position") == 0


class TestMarkedPosition:
    """A soft attribute and its commands - nothing here touches the device."""

    async def test_marked_position_starts_at_zero(self, controller):
        assert controller.marked_position.readback == 0

    async def test_mark_captures_the_current_position(self, controller):
        await controller.position_demand.set(1500)
        await _wait_until(lambda: _position_is(controller, 1500))

        await controller.mark_position()

        assert controller.marked_position.readback == 1500

    async def test_mark_does_not_move_the_stage(self, controller):
        await controller.mark_position()
        assert await controller.connection.read_field("position", "Position") == 0

    async def test_move_to_mark_returns_to_the_mark(self, controller):
        await controller.position_demand.set(800)
        await _wait_until(lambda: _position_is(controller, 800))
        await controller.mark_position()

        await controller.position_demand.set(0)
        await _wait_until(lambda: _position_is(controller, 0))
        await controller.move_to_mark()

        await _wait_until(lambda: _position_is(controller, 800))

    async def test_marked_position_can_be_written_directly(self, controller):
        """Autosave restores the mark by writing to it, without hardware."""
        await controller.marked_position.set(4242)
        assert controller.marked_position.readback == 4242


class TestBitFields:
    """Single flag bits of a settings struct are read and written in isolation."""

    async def test_bit_round_trips(self, controller):
        await controller.stop_at_low_limit.poll()
        original = controller.stop_at_low_limit.readback

        await controller.stop_at_low_limit.set(not original)
        await controller.stop_at_low_limit.poll()

        assert controller.stop_at_low_limit.readback is (not original)

    async def test_writing_one_bit_leaves_its_neighbours_alone(self, controller):
        """The two stop flags share the BorderFlags field."""
        await controller.stop_at_high_limit.set(True)
        await controller.stop_at_low_limit.set(False)

        await controller.stop_at_high_limit.poll()
        assert controller.stop_at_high_limit.readback is True

    async def test_writing_one_bit_leaves_the_rest_of_the_struct_alone(
        self, controller
    ):
        await controller.low_limit.set(-3000)

        await controller.limits_use_encoder.set(True)

        assert await controller.connection.read_field("edges", "LeftBorder") == -3000


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
        await attr.set(value)
        await attr.poll()
        assert attr.readback == value

    async def test_scaled_setting_round_trips_in_engineering_units(self, controller):
        """CriticalT is in tenths of a degree on the wire."""
        await controller.critical_temperature.set(75.0)

        assert await controller.connection.read_field("secure", "CriticalT") == 750
        await controller.critical_temperature.poll()
        assert controller.critical_temperature.readback == pytest.approx(75.0)

    async def test_home_flags_expose_both_raw_and_bit_views(self, controller):
        await controller.home_forward_first.set(True)

        await controller.home_flags.poll()
        assert controller.home_flags.readback & ximc.HomeFlags.HOME_DIR_FIRST.value


class TestEngineeringUnits:
    """The dial-to-user transform, held entirely in software."""

    async def test_defaults_are_a_one_to_one_step_mapping(self, controller):
        assert controller.egu.readback == "steps"
        assert controller.motor_resolution.readback == 1.0
        assert controller.user_offset.readback == 0.0
        assert controller.user_position.meta["units"] == "steps"
        assert controller.motor_resolution.meta["units"] == "steps/step"

    async def test_user_position_follows_the_dial_position(self, controller):
        await controller.motor_resolution.set(0.001)
        await controller.user_offset.set(5.0)

        await controller.position_demand.set(2000)
        await _wait_until(lambda: _position_is(controller, 2000))
        await controller.position.poll()

        assert controller.user_position.readback == pytest.approx(7.0)

    async def test_user_position_recomputes_when_the_transform_changes(
        self, controller
    ):
        await controller.position.poll()
        await controller.motor_resolution.set(0.01)
        await controller.user_offset.set(1.5)

        assert controller.user_position.readback == pytest.approx(1.5)

    async def test_user_demand_moves_in_engineering_units(self, controller):
        await controller.motor_resolution.set(0.001)
        await controller.user_offset.set(1.0)

        await controller.user_demand.set(2.5)

        await _wait_until(lambda: _position_is(controller, 1500))

    async def test_conversion_round_trips(self, controller):
        await controller.motor_resolution.set(0.002)
        await controller.user_offset.set(-3.0)

        assert controller.to_steps(controller.to_user(1234)) == 1234

    async def test_zero_resolution_is_rejected(self, controller):
        await controller.motor_resolution.set(0.0)

        with pytest.raises(ValueError, match="motor_resolution is zero"):
            controller.to_steps(1.0)

    async def test_egu_name_propagates_to_the_metadata(self, controller):
        await controller.egu.set("mm")

        assert controller.user_position.meta["units"] == "mm"
        assert controller.user_demand.meta["units"] == "mm"
        assert controller.user_offset.meta["units"] == "mm"
        assert controller.motor_resolution.meta["units"] == "mm/step"


class TestMotionInhibit:
    async def test_commands_are_rejected_while_inhibited(self, controller):
        await controller.motion_inhibit.set(True)

        with pytest.raises(MotionInhibitedError):
            await controller.move_to_mark()

        assert await controller.connection.read_field("position", "Position") == 0

    async def test_demands_are_rejected_while_inhibited(self, controller):
        """A setter that raises is logged by FastCS; the move does not happen."""
        await controller.motion_inhibit.set(True)

        await controller.position_demand.set(1000)

        await asyncio.sleep(0.2)
        assert await controller.connection.read_field("position", "Position") == 0

    async def test_stop_still_works_while_inhibited(self, controller):
        await controller.motion_inhibit.set(True)
        await controller.stop()

    async def test_clearing_the_inhibit_allows_motion_again(self, controller):
        await controller.motion_inhibit.set(True)
        await controller.motion_inhibit.set(False)

        await controller.position_demand.set(600)

        await _wait_until(lambda: _position_is(controller, 600))


class TestTweak:
    async def test_tweak_forward_moves_by_the_step(self, controller):
        await controller.tweak_step.set(300)

        await controller.tweak_forward()

        await _wait_until(lambda: _position_is(controller, 300))

    async def test_tweak_reverse_moves_back_by_the_step(self, controller):
        await controller.tweak_step.set(300)
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
            return await controller.connection.read_field("position", "Position") < 0

        await _wait_until(gone_negative)
        await controller.stop()


class TestFollowingError:
    async def test_following_error_tracks_the_encoder(self, controller):
        await controller.position.poll()
        await controller.encoder_position.poll()

        expected = controller.position.readback - controller.encoder_position.readback
        assert controller.following_error.readback == expected


class TestLifecycle:
    """Opening and closing the device is the runner's job, not the controller's."""

    async def test_the_runner_opens_and_closes_the_connection(self, settings, options):
        connections = Connections({"ximc": XimcConnection(settings)})
        controller = XimcController(connections, options)
        controller.set_path(["TEST"])
        assert not controller.connection.is_open

        runner = ControllerRunner(controller, connections)
        await runner.start()
        try:
            assert controller.connection.is_open
            assert controller.connected
        finally:
            await runner.stop()

        assert not controller.connection.is_open


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
        await controller.current_speed.poll()
        assert controller.current_speed.readback == 0

    async def test_current_speed_is_nonzero_while_moving(self, controller):
        """The readback tracks real motion, not the configured setpoint."""
        await controller.position_demand.set(100000)

        async def is_moving_at_speed() -> bool:
            await controller.current_speed.poll()
            return controller.current_speed.readback > 0

        await _wait_until(is_moving_at_speed)
        await controller.stop()

    async def test_limit_switches_are_inactive_on_a_free_axis(self, controller):
        await controller.at_low_limit.poll()
        await controller.at_high_limit.poll()
        assert controller.at_low_limit.readback is False
        assert controller.at_high_limit.readback is False

    async def test_usb_current_is_read(self, controller):
        await controller.usb_current.poll()
        assert controller.usb_current.readback > 0


class TestIdentity:
    """Identity is read once at startup, so each axis is distinguishable."""

    async def test_serial_number_is_populated(self, controller):
        assert isinstance(controller.serial_number.readback, int)

    async def test_firmware_version_is_populated(self, controller):
        version = controller.firmware_version.readback
        assert version.count(".") == 2, version

    async def test_controller_and_stage_names_are_readable(self, controller):
        assert isinstance(controller.controller_name.readback, str)
        assert isinstance(controller.stage_name.readback, str)


async def _position_is(controller: XimcController, expected: int) -> bool:
    return await controller.connection.read_field("position", "Position") == expected


async def _position_exceeds(controller: XimcController, threshold: int) -> bool:
    position = await controller.connection.read_field("position", "Position")
    assert isinstance(position, int)
    return position > threshold
