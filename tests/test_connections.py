"""Tests driving a real libximc virtual device - no mocks."""

from pathlib import Path

import pytest

from fastcs_ximc import XimcConnection, XimcConnectionSettings, XimcDRAConnection

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

    async def test_a_missing_device_node_is_terminal(self):
        connection = XimcDRAConnection(XimcConnectionSettings(port="/dev/ttyACM0"))

        assert connection.is_terminal(FileNotFoundError())
        assert connection._node_path == "/dev/ttyACM0"

    async def test_other_failures_are_not_terminal(self):
        connection = XimcDRAConnection(XimcConnectionSettings(port="/dev/ttyACM0"))

        assert not connection.is_terminal(ConnectionError("device gone"))

    async def test_it_is_a_ximc_connection(self, settings):
        """So a controller claiming a `XimcConnection` accepts one."""
        connection = XimcDRAConnection(settings)
        await connection.connect()
        try:
            assert await connection.read("position", "Position") == 0
        finally:
            await connection.close()
