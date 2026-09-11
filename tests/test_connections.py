"""Tests driving a real libximc virtual device - no mocks."""

from pathlib import Path

import pytest
from fastcs.connections import Connection
from pydantic import ValidationError

from fastcs_ximc import (
    XimcConnection,
    XimcConnectionSettings,
    XimcDRAConnection,
)
from fastcs_ximc.connections import DRANode, Recovery

pytestmark = pytest.mark.asyncio


def _raise(error: Exception):
    """Stand in for a libximc call that fails."""

    def fail(*args):
        raise error

    return fail


class TestXimcConnection:
    async def test_connect_creates_state_file(self, settings, device_uri):
        path = Path(device_uri.removeprefix("xi-emu://"))
        assert not path.exists()

        connection = XimcConnection(settings)
        await connection.connect()
        try:
            assert path.exists()
            assert connection.is_open
        finally:
            await connection.close()

        assert not connection.is_open

    async def test_reading_before_connect_raises(self, settings):
        with pytest.raises(ConnectionError, match="is not open"):
            await XimcConnection(settings).read("position", "Position")

    async def test_close_before_connect_is_a_no_op(self, settings):
        """The runner closes before every reconnect; that must be free."""
        connection = XimcConnection(settings)

        await connection.close()

        assert not connection.is_open

    async def test_reconnect_gets_a_fresh_handle(self, connection):
        """A reconnect attempt is a close followed by a connect."""
        handle = connection._axis

        await connection.close()
        await connection.connect()

        assert connection._axis is not handle

    async def test_read(self, connection):
        assert await connection.read("position", "Position") == 0
        assert await connection.read("move", "Speed") == 1000

    async def test_read_struct(self, connection):
        assert (await connection.read_struct("position")).Position == 0

    async def test_write_round_trips(self, connection):
        await connection.write("move", "Speed", 750)
        assert await connection.read("move", "Speed") == 750

    async def test_write_preserves_other_fields(self, connection):
        """A write must read-modify-write; libximc rejects partial structs."""
        accel = await connection.read("move", "Accel")

        await connection.write("move", "Speed", 321)

        assert await connection.read("move", "Accel") == accel
        assert await connection.read("move", "Speed") == 321

    async def test_command(self, connection):
        await connection.command("move", 500, 0)
        assert await connection.read("status", "MvCmdSts")

    async def test_a_dead_link_marks_the_connection_down(self, connection):
        """libximc raises ConnectionError when the device must be reopened."""
        connection._set_connected()
        connection._axis.command_stop = _raise(ConnectionError("device gone"))

        with pytest.raises(ConnectionError):
            await connection.command("stop")

        assert not connection.connected

    async def test_a_rejected_value_leaves_the_connection_up(self, connection):
        """libximc raises ValueError when the device rejects a parameter."""
        connection._set_connected()
        connection._axis.command_stop = _raise(ValueError("rejected"))

        with pytest.raises(ValueError, match="rejected"):
            await connection.command("stop")

        assert connection.connected

    async def test_missing_real_device_is_reported(self, monkeypatch):
        monkeypatch.setattr(
            "fastcs_ximc.utils.enumerate_device_uris",
            lambda: ["xi-com:///dev/ttyACM1"],
        )
        connection = XimcConnection(XimcConnectionSettings(port="/dev/ttyACM0"))

        with pytest.raises(Exception, match="ttyACM0"):
            await connection.connect()


class TestXimcDRAConnection:
    """A claimed device node is gone for good, so a missing one is terminal."""

    @pytest.fixture
    def connection(self, monkeypatch):
        monkeypatch.setenv("AXIS_PORT", "/dev/ttyACM0")
        return XimcDRAConnection(port_env="AXIS_PORT")

    async def test_the_node_is_what_port_env_resolved_to(self, connection):
        assert connection._node == "/dev/ttyACM0"
        assert connection.uri == "xi-com:///dev/ttyACM0"

    async def test_a_missing_device_node_is_terminal(self, connection):
        assert connection.is_terminal(FileNotFoundError())
        assert not connection.is_terminal(ConnectionError("device gone"))

    async def test_it_names_the_node_it_lost(self, connection):
        assert "/dev/ttyACM0" in connection.unrecoverable_reason()

    async def test_it_is_a_ximc_connection(self, connection):
        """So a controller claiming a `XimcConnection` accepts one."""
        assert isinstance(connection, XimcConnection)

    async def test_an_unset_variable_is_reported(self, monkeypatch):
        monkeypatch.delenv("MISSING_PORT", raising=False)

        with pytest.raises(ValidationError, match="MISSING_PORT"):
            XimcDRAConnection(port_env="MISSING_PORT")


class TestRecovery:
    """The policy is an object, so it is not tied to one transport."""

    async def test_the_default_retries_everything(self, settings):
        assert not XimcConnection(settings).is_terminal(FileNotFoundError())

    async def test_a_policy_can_be_given_to_any_connection(self):
        """No DRA subclass per transport: the same object does for all of them."""

        class OtherConnection(Connection):
            """Stands in for a serial or IP connection."""

            recovery = DRANode()

            async def connect(self) -> None: ...

            async def close(self) -> None: ...

            def is_terminal(self, exc: BaseException) -> bool:
                return self.recovery.is_terminal(exc)

        assert OtherConnection().is_terminal(FileNotFoundError())

    async def test_a_policy_can_be_swapped_on_an_instance(self, settings):
        connection = XimcConnection(settings)
        connection.recovery = DRANode()

        assert connection.is_terminal(FileNotFoundError())

    async def test_what_the_runner_would_do_with_it(self, settings):
        """FastCS does not consult `is_terminal` yet. This is what it is for."""
        connection = XimcConnection(settings)
        connection.recovery = DRANode()
        attempts = 0

        for error in (ConnectionError("busy"), FileNotFoundError("node gone")):
            attempts += 1
            if connection.is_terminal(error):
                break

        assert attempts == 2
        assert isinstance(connection.recovery, Recovery)
