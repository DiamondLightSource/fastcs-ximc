"""Shared fixtures backed by a libximc virtual (``xi-emu://``) device.

libximc's virtual device implements the full API including simulated motion,
so the tests drive a real device rather than mocks.
"""

from pathlib import Path

import pytest
import pytest_asyncio

from fastcs_ximc import XimcController, XimcDevice, XimcOptions


@pytest.fixture
def device_uri(tmp_path: Path) -> str:
    """A ``xi-emu://`` URI in a per-test directory."""
    return f"xi-emu://{tmp_path / 'sim' / 'axis.bin'}"


@pytest.fixture
def options(device_uri: str) -> XimcOptions:
    return XimcOptions(uri=device_uri, poll_period=0.05)


@pytest_asyncio.fixture
async def device(device_uri: str):
    """An open `XimcDevice` backed by a virtual device."""
    device = XimcDevice(device_uri)
    await device.open()
    yield device
    await device.close()


@pytest_asyncio.fixture
async def controller(options: XimcOptions):
    """A `XimcController` taken through the FastCS startup sequence."""
    controller = XimcController(options)
    await controller.initialise()
    controller.post_initialise()
    await controller.connect()
    yield controller
    await controller.disconnect()
