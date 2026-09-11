"""Shared fixtures backed by a libximc virtual (``xi-emu://``) device.

libximc's virtual device implements the full API including simulated motion,
so the tests drive a real device rather than mocks.
"""

from pathlib import Path

import pytest
import pytest_asyncio
from fastcs.connections import Connections
from fastcs.controllers import ControllerRunner

from fastcs_ximc import (
    XimcConnection,
    XimcConnectionSettings,
    XimcController,
    XimcOptions,
)


@pytest.fixture
def device_uri(tmp_path: Path) -> str:
    """A ``xi-emu://`` URI in a per-test directory."""
    return f"xi-emu://{tmp_path / 'sim' / 'axis.bin'}"


@pytest.fixture
def settings(device_uri: str) -> XimcConnectionSettings:
    return XimcConnectionSettings(uri=device_uri)


@pytest.fixture
def options() -> XimcOptions:
    return XimcOptions(poll_period=0.05)


@pytest_asyncio.fixture
async def connection(settings: XimcConnectionSettings):
    """An open `XimcConnection` backed by a virtual device."""
    connection = XimcConnection(settings)
    await connection.connect()
    yield connection
    await connection.close()


@pytest_asyncio.fixture
async def controller(settings: XimcConnectionSettings, options: XimcOptions):
    """A `XimcController` taken through the FastCS startup sequence."""
    connections = Connections({"motor": XimcConnection(settings)})
    controller = XimcController(connections, options)
    controller.set_path(["TEST"])
    runner = ControllerRunner(controller, connections)
    await runner.start()
    yield controller
    await runner.stop()
