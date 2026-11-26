"""Tests driving a real libximc virtual device - no mocks."""

import asyncio

import libximc.highlevel as ximc
import pytest
from fastcs.logging import logger

from fastcs_ximc import MotionInhibitedError, XimcController

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


async def _position_is(controller: XimcController, expected: int) -> bool:
    return await controller.device.read_field("position", "Position") == expected


async def _position_exceeds(controller: XimcController, threshold: int) -> bool:
    position = await controller.device.read_field("position", "Position")
    assert isinstance(position, int)
    return position > threshold


class TestSettings:
    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("speed", 600),
            ("low_limit", -5000),
            ("home_fast_speed", 900),
            ("nominal_current", 400),
            ("hold_current", 40),
            ("steps_per_turn", 200),
            ("critical_power_current", 2500),
        ],
    )
    async def test_round_trips_through_each_struct(self, controller, name, value):
        attr = getattr(controller, name)
        await attr.put(value)
        await _refresh(attr)
        assert attr.get() == value

    async def test_write_preserves_other_fields(self, controller):
        """A write must read-modify-write; libximc rejects partial structs."""
        accel = await controller.device.read_field("move", "Accel")

        await controller.speed.put(321)

        assert await controller.device.read_field("move", "Accel") == accel

    async def test_scaled_read(self, controller):
        raw = await controller.device.read_field("status", "CurT")
        await _refresh(controller.temperature)
        assert controller.temperature.get() == pytest.approx(raw / 10)

    async def test_scaled_write(self, controller):
        await controller.critical_temperature.put(75.0)
        assert await controller.device.read_field("secure", "CriticalT") == 750

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

    async def test_every_io_ref_resolves_to_a_real_libximc_field(self, controller):
        """Guards against typos in the (group, field) table."""
        for name, attr in controller.attributes.items():
            if not attr.has_io_ref():
                continue
            ref = attr.io_ref
            value = await controller.device.read_field(ref.group, ref.field)
            assert value is not None, f"{name} -> {ref.group}.{ref.field}"


class TestMotion:
    async def test_relative_move_changes_position(self, controller):
        await controller.position_demand.put(500)
        await _wait_until(lambda: _position_is(controller, 500))

        await controller.relative_move.put(250)
        await _wait_until(lambda: _position_is(controller, 750))

    async def test_moving_is_true_during_a_move(self, controller):
        await controller.position_demand.put(100000)

        async def is_moving() -> bool:
            await _refresh(controller.moving)
            return controller.moving.get()

        await _wait_until(is_moving)

    async def test_stop_halts_a_move(self, controller):
        await controller.position_demand.put(100000)
        await _wait_until(lambda: _position_exceeds(controller, 0))

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

    async def test_move_to_mark_returns_to_the_mark(self, controller):
        await controller.position_demand.put(800)
        await _wait_until(lambda: _position_is(controller, 800))
        await controller.mark_position()

        await controller.position_demand.put(0)
        await _wait_until(lambda: _position_is(controller, 0))
        await controller.move_to_mark()

        await _wait_until(lambda: _position_is(controller, 800))

    async def test_tweak_moves_by_the_step(self, controller):
        await controller.tweak_step.put(300)
        await controller.tweak_forward()
        await _wait_until(lambda: _position_is(controller, 300))

        await controller.tweak_reverse()

        await _wait_until(lambda: _position_is(controller, 0))

    async def test_jog_forward_increases_the_position(self, controller):
        """libximc's right is increasing steps."""
        await controller.jog_forward()
        await _wait_until(lambda: _position_exceeds(controller, 0))
        await controller.stop()

    async def test_jog_reverse_decreases_the_position(self, controller):
        await controller.jog_reverse()

        async def gone_negative() -> bool:
            return await controller.device.read_field("position", "Position") < 0

        await _wait_until(gone_negative)
        await controller.stop()


class TestEngineeringUnits:
    """The dial-to-user transform, held entirely in software."""

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

    async def test_following_error_tracks_the_encoder(self, controller):
        await _refresh(controller.position)
        await _refresh(controller.encoder_position)

        expected = controller.position.get() - controller.encoder_position.get()
        assert controller.following_error.get() == expected


class TestMotionInhibit:
    async def test_demands_are_rejected_while_inhibited(self, controller):
        await controller.motion_inhibit.put(True)

        with pytest.raises(MotionInhibitedError):
            await controller.position_demand.put(1000)

        assert await controller.device.read_field("position", "Position") == 0

    async def test_stop_still_works_while_inhibited(self, controller):
        await controller.motion_inhibit.put(True)
        await controller.stop()


class TestLifecycle:
    async def test_identity_is_read(self, controller):
        await _refresh(controller.manufacturer)

        assert controller.manufacturer.get() == "XIMC"
        assert isinstance(controller.serial_number.get(), int)
        assert controller.firmware_version.get().count(".") == 2
        assert controller.device_uri.get() == controller.device.uri

    async def test_connect_can_be_called_again(self, options):
        """A reconnect loop calls connect() repeatedly, not disconnect+connect."""
        controller = XimcController(options)
        await controller.connect()
        handle = controller.device.axis

        await controller.connect()

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

    async def test_disconnect_closes_the_device(self, options):
        controller = XimcController(options)
        await controller.connect()

        await controller.disconnect()

        assert not controller.device.is_open

    async def test_logs_reach_the_fastcs_logger(self, options):
        records = []
        sink = logger.add(lambda m: records.append(m.record))
        controller = XimcController(options)
        try:
            await controller.connect()
            await controller.disconnect()
        finally:
            logger.remove(sink)

        opened = [r for r in records if r["message"] == "Opened device"]
        assert opened[0]["extra"]["uri"] == options.device_uri


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
