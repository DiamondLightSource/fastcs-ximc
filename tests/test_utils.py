import enum

import libximc.highlevel as ximc
import pytest

from fastcs_ximc import DeviceNotFoundError, patch_strict_flags
from fastcs_ximc.utils import (
    check_device_present,
    get_flag_boundary,
    libximc_flag_classes,
    prepare_virtual_device,
    set_flag_boundary,
)


@pytest.fixture
def strict_flags():
    """Force all libximc flag enums back to STRICT, and restore afterwards."""
    flag_classes = libximc_flag_classes()
    original = [get_flag_boundary(cls) for cls in flag_classes]
    for cls in flag_classes:
        set_flag_boundary(cls, enum.FlagBoundary.STRICT)
    yield flag_classes
    for cls, boundary in zip(flag_classes, original, strict=True):
        set_flag_boundary(cls, boundary)


# If this test starts failing the bug may have been fixed upstream in libximc,
# in which case patch_strict_flags can be removed.
def test_flags_reject_unknown_bits_without_patch(strict_flags):
    """Confirm the underlying bug exists - 0xCC is rejected by the enum."""
    with pytest.raises(ValueError, match="MoveFlags"):
        ximc.MoveFlags(0xCC)


def test_patch_tolerates_unknown_bits(strict_flags):
    """After patching, hardware values with undocumented bits don't raise."""
    patch_strict_flags()

    assert int(ximc.MoveFlags(0xCC)) == 0  # no defined bits set
    assert ximc.MoveFlags(0x01) is ximc.MoveFlags.RPM_DIV_1000  # valid value survives


def test_patch_covers_every_flag_class(strict_flags):
    """The patch is applied across the whole library, not one field."""
    patched = patch_strict_flags()

    assert len(patched) == len(strict_flags)
    assert "StateFlags" in patched
    for cls in strict_flags:
        assert get_flag_boundary(cls) is enum.FlagBoundary.CONFORM


def test_patch_keeps_defined_bits_of_a_mixed_value(strict_flags):
    """Undefined bits are dropped, defined bits are kept."""
    patch_strict_flags()

    status = ximc.MvcmdStatus(0x81)  # MVCMD_RUNNING | MVCMD_MOVE
    assert ximc.MvcmdStatus.MVCMD_RUNNING in status
    assert ximc.MvcmdStatus.MVCMD_MOVE in status


class TestPrepareVirtualDevice:
    def test_creates_missing_parent_directory(self, tmp_path):
        """libximc creates the .bin but not the directory holding it."""
        target = tmp_path / "deeply" / "nested" / "axis.bin"
        prepare_virtual_device(f"xi-emu://{target}")

        assert target.parent.is_dir()
        assert not target.exists()  # libximc creates the file itself

    def test_existing_directory_is_left_alone(self, tmp_path):
        prepare_virtual_device(f"xi-emu://{tmp_path / 'axis.bin'}")
        assert tmp_path.is_dir()

    def test_real_uri_is_ignored(self, tmp_path):
        prepare_virtual_device("xi-com:///dev/ttyACM0")
        assert list(tmp_path.iterdir()) == []


class TestCheckDevicePresent:
    def test_virtual_device_is_always_accepted(self):
        check_device_present("xi-emu:///tmp/axis.bin")

    def test_missing_real_device_raises(self, monkeypatch):
        monkeypatch.setattr(
            "fastcs_ximc.utils.enumerate_device_uris",
            lambda: ["xi-com:///dev/ttyACM1"],
        )
        with pytest.raises(DeviceNotFoundError, match="ttyACM0"):
            check_device_present("xi-com:///dev/ttyACM0")

    def test_present_real_device_passes(self, monkeypatch):
        monkeypatch.setattr(
            "fastcs_ximc.utils.enumerate_device_uris",
            lambda: ["xi-com:///dev/ttyACM0"],
        )
        check_device_present("xi-com:///dev/ttyACM0")
