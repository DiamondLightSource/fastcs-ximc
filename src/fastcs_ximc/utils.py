import enum
import logging
from pathlib import Path

import libximc.highlevel as ximc

logger = logging.getLogger(__name__)


class DeviceNotFoundError(Exception):
    """Raised when a configured device URI is not present on this machine."""


def libximc_flag_classes() -> list[type[enum.Flag]]:
    """Every ``enum.Flag`` subclass exposed by ``libximc.highlevel``."""
    return [
        obj
        for obj in vars(ximc).values()
        if isinstance(obj, type) and issubclass(obj, enum.Flag)
    ]


def get_flag_boundary(flag_class: type[enum.Flag]) -> enum.FlagBoundary:
    """Read ``_boundary_``, enum's private hook for out-of-range values."""
    return flag_class._boundary_  # type: ignore[attr-defined]  # noqa: SLF001


def set_flag_boundary(flag_class: type[enum.Flag], boundary: enum.FlagBoundary) -> None:
    """Set ``_boundary_``, enum's private hook for out-of-range values."""
    flag_class._boundary_ = boundary  # type: ignore[attr-defined]  # noqa: SLF001


def patch_strict_flags() -> list[str]:
    """Make libximc ``Flag`` enums drop undocumented hardware bits, not raise."""
    flag_classes = libximc_flag_classes()
    for flag_class in flag_classes:
        set_flag_boundary(flag_class, enum.FlagBoundary.CONFORM)

    logger.debug("Patched %d libximc flag enums to CONFORM", len(flag_classes))
    return [flag_class.__name__ for flag_class in flag_classes]


def enumerate_device_uris() -> list[str]:
    """List the URIs of all libximc devices attached to this machine."""
    devices = ximc.enumerate_devices(ximc.EnumerateFlags.ENUMERATE_ALL_COM)
    uris = [device["uri"] for device in devices]
    logger.debug("Enumerated device URIs: %s", uris)
    return uris


def check_device_present(uri: str) -> None:
    """Raise `DeviceNotFoundError` if a real (not ``xi-emu://``) ``uri`` is absent."""
    if uri.startswith("xi-emu://"):
        return

    if uri not in enumerate_device_uris():
        raise DeviceNotFoundError(f"No libximc device found at '{uri}'")


def prepare_virtual_device(uri: str) -> None:
    """Create the parent directory of a ``xi-emu://`` file; libximc won't."""
    if not uri.startswith("xi-emu://"):
        return

    path = Path(uri.removeprefix("xi-emu://"))
    path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Prepared virtual device state file at %s", path)
