"""Tests driving a real libximc virtual device - no mocks."""

from pathlib import Path

import pytest

from fastcs_ximc import XimcConnection, XimcConnectionSettings

pytestmark = pytest.mark.asyncio


class TestOpenClose:
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

    async def test_axis_raises_before_connect(self, settings):
        with pytest.raises(ConnectionError, match="is not open"):
            _ = XimcConnection(settings).axis

    async def test_close_before_connect_is_a_no_op(self, settings):
        """The runner closes before every reconnect; that must be free."""
        connection = XimcConnection(settings)

        await connection.close()

        assert not connection.is_open

    async def test_reconnect_gets_a_fresh_handle(self, settings):
        """A reconnect attempt is a close followed by a connect."""
        connection = XimcConnection(settings)
        await connection.connect()
        handle = connection.axis

        await connection.close()
        await connection.connect()

        try:
            assert connection.axis is not handle
        finally:
            await connection.close()

    async def test_close_drops_a_handle_that_will_not_close(self, connection):
        """A dead link may fail to close; that must not block reopening."""
        axis = connection.axis
        close_device = axis.close_device

        def _raise() -> None:
            raise OSError("device went away")

        axis.close_device = _raise

        with pytest.raises(OSError, match="device went away"):
            await connection.close()

        assert not connection.is_open
        axis.close_device = close_device
        close_device()

    async def test_missing_real_device_is_reported(self, monkeypatch):
        monkeypatch.setattr(
            "fastcs_ximc.utils.enumerate_device_uris",
            lambda: ["xi-com:///dev/ttyACM1"],
        )
        connection = XimcConnection(XimcConnectionSettings(port="/dev/ttyACM0"))

        with pytest.raises(Exception, match="ttyACM0"):
            await connection.connect()


class TestFieldAccess:
    async def test_read_field(self, connection):
        assert await connection.read_field("position", "Position") == 0
        assert await connection.read_field("move", "Speed") == 1000

    async def test_write_field_round_trips(self, connection):
        await connection.write_field("move", "Speed", 750)
        assert await connection.read_field("move", "Speed") == 750

    async def test_write_field_preserves_other_fields(self, connection):
        """A write must read-modify-write; libximc rejects partial structs."""
        accel = await connection.read_field("move", "Accel")

        await connection.write_field("move", "Speed", 321)

        assert await connection.read_field("move", "Accel") == accel
        assert await connection.read_field("move", "Speed") == 321

    async def test_call_runs_against_the_handle(self, connection):
        position = await connection.call(lambda axis: axis.get_position())
        assert position.Position == 0


class TestHealth:
    """Only a dead transport marks the connection down."""

    async def test_transport_failure_marks_the_connection_down(self, connection):
        connection._set_connected()

        def _gone(axis):
            raise ConnectionError("Cannot send command to the device")

        with pytest.raises(ConnectionError):
            await connection.call(_gone)

        assert not connection.connected

    async def test_a_device_complaint_leaves_the_connection_up(self, connection):
        connection._set_connected()

        def _rejected(axis):
            raise ValueError("The input was rejected by the device")

        with pytest.raises(ValueError, match="rejected"):
            await connection.call(_rejected)

        assert connection.connected
