"""Tests driving a real libximc virtual device - no mocks."""

import asyncio

import libximc.highlevel as ximc
import pytest
from fastcs.connections import Connections
from fastcs.controllers import ControllerRunner

from fastcs_ximc import MotionInhibitedError, XimcConnection, XimcController

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


async def _position_is(controller: XimcController, expected: int) -> bool:
    return await controller.connection.read("position", "Position") == expected


class TestAttributes:
    async def test_position_reads_zero_at_startup(self, controller):
        await controller.position.poll()
        assert controller.position.readback == 0

    async def test_speed_round_trips_through_hardware(self, controller):
        await controller.speed.set(600)
        await controller.speed.poll()
        assert controller.speed.readback == 600

    async def test_temperature_is_scaled_to_degrees(self, controller):
        raw = await controller.connection.read("status", "CurT")
        await controller.temperature.poll()
        assert controller.temperature.readback == pytest.approx(raw / 10, abs=0.5)

    async def test_bit_attribute_masks_a_single_flag(self, controller):
        await controller.moving.poll()
        assert controller.moving.readback is False

    async def test_bit_round_trips_without_disturbing_the_struct(self, controller):
        """The other flags of the BorderFlags field must survive the write."""
        border = int(await controller.connection.read("edges", "BorderFlags"))

        await controller.stop_at_low_limit.set(True)
        await controller.stop_at_low_limit.poll()

        assert controller.stop_at_low_limit.readback is True
        expected = border | ximc.BorderFlags.BORDER_STOP_LEFT.value
        assert int(await controller.connection.read("edges", "BorderFlags")) == expected

    async def test_device_information_is_read_once_on_connect(self, controller):
        assert controller.manufacturer.readback == "XIMC"
        assert controller.firmware_version.readback.count(".") == 2


class TestMotion:
    async def test_absolute_move_changes_position(self, controller):
        await controller.position_demand.set(2000)

        await _wait_until(lambda: _position_is(controller, 2000))

    async def test_moving_is_true_during_a_move(self, controller):
        await controller.position_demand.set(100000)

        async def is_moving() -> bool:
            await controller.moving.poll()
            return controller.moving.readback

        await _wait_until(is_moving)

    async def test_stop_halts_a_move(self, controller):
        await controller.position_demand.set(100000)
        await controller.stop()

        async def stopped() -> bool:
            await controller.moving.poll()
            return not controller.moving.readback

        await _wait_until(stopped)


class TestEngineeringUnits:
    """The dial-to-user transform, held entirely in software."""

    async def test_defaults_are_a_one_to_one_step_mapping(self, controller):
        assert controller.egu.readback == "steps"
        assert controller.motor_resolution.readback == 1.0
        assert controller.user_position.meta["units"] == "steps"

    async def test_user_position_follows_the_dial_position(self, controller):
        await controller.motor_resolution.set(0.001)

        await controller.position_demand.set(2000)
        await _wait_until(lambda: _position_is(controller, 2000))
        await controller.position.poll()

        assert controller.user_position.readback == pytest.approx(2.0)

    async def test_egu_name_propagates_to_the_metadata(self, controller):
        await controller.egu.set("mm")

        assert controller.user_position.meta["units"] == "mm"
        assert controller.motor_resolution.meta["units"] == "mm/step"


class TestMotionInhibit:
    async def test_commands_are_rejected_while_inhibited(self, controller):
        await controller.motion_inhibit.set(True)

        with pytest.raises(MotionInhibitedError):
            await controller.jog_forward()

        assert await _position_is(controller, 0)

    async def test_demands_are_rejected_while_inhibited(self, controller):
        """A setter that raises is logged by FastCS; the move does not happen."""
        await controller.motion_inhibit.set(True)

        await controller.position_demand.set(1000)

        await asyncio.sleep(0.2)
        assert await _position_is(controller, 0)


class TestLifecycle:
    """Opening and closing the device is the runner's job, not the controller's."""

    async def test_the_runner_opens_and_closes_the_connection(self, settings, options):
        connections = Connections({"motor": XimcConnection(settings)})
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
