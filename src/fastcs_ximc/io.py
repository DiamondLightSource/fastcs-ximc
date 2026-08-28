"""Attribute IO mapping FastCS attributes onto libximc struct fields."""

from __future__ import annotations

from dataclasses import KW_ONLY, dataclass
from typing import Any

from fastcs.attributes import AttributeIO, AttributeIORef, AttrR, AttrW

from .device import XimcDevice


@dataclass
class XimcSettingsIORef(AttributeIORef):
    """Points an ``Attribute`` at one field of one libximc struct.

    ``group`` is the libximc struct name without its ``_settings_t`` suffix, so
    ``("move", "Speed")`` reads ``get_move_settings().Speed`` and writes it back
    through ``set_move_settings``. The read-only groups ``position``, ``status``
    and ``device_information`` are read with ``get_<group>()`` instead.
    """

    group: str
    field: str
    _: KW_ONLY
    scale: float = 1.0
    """Divisor applied on read and multiplier on write, for raw device units."""
    bit: int | None = None
    """Mask selecting a single flag of a bitmask field, read and written alone."""
    update_period: float | None = 0.2

    def __post_init__(self) -> None:
        # Accept libximc Flag members as well as plain ints
        if self.bit is not None:
            self.bit = int(self.bit)


class XimcSettingsIO(AttributeIO[Any, XimcSettingsIORef]):
    """Reads and writes libximc struct fields for `XimcSettingsIORef` attributes."""

    def __init__(self, device: XimcDevice) -> None:
        super().__init__()
        self._device = device

    async def update(self, attr: AttrR[Any, XimcSettingsIORef]) -> None:
        ref = attr.io_ref
        raw = await self._device.read_field(ref.group, ref.field)
        if ref.bit is not None:
            await attr.update(bool(int(raw) & ref.bit))
        else:
            await attr.update(_to_datatype(raw, ref.scale, attr))

    async def send(self, attr: AttrW[Any, XimcSettingsIORef], value: Any) -> None:
        ref = attr.io_ref
        if ref.bit is not None:
            await self._device.write_bit(ref.group, ref.field, ref.bit, bool(value))
        else:
            await self._device.write_field(ref.group, ref.field, _to_device(value, ref))


def _to_datatype(raw: Any, scale: float, attr: AttrR[Any, XimcSettingsIORef]) -> Any:
    """Coerce a raw libximc value to the attribute's datatype.

    libximc returns ``IntEnum`` and ``Flag`` members for bitmask and mode
    fields; ``int()``/``float()`` reduce those to their numeric value.
    """
    dtype = attr.datatype.dtype
    if dtype is bool:
        return bool(raw)
    if dtype is str:
        return str(raw)
    if dtype is int:
        return int(int(raw) / scale) if scale != 1.0 else int(raw)
    if dtype is float:
        return float(raw) / scale
    return raw


def _to_device(value: Any, ref: XimcSettingsIORef) -> Any:
    """Convert an attribute value back into raw device units."""
    if ref.scale != 1.0 and isinstance(value, int | float):
        return int(round(value * ref.scale))
    return value
