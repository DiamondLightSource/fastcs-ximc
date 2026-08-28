"""Configuration models for a XIMC controller."""

import os
from typing import Literal

from pydantic import BaseModel, model_validator

XIMC_URI_SCHEMES = ("xi-com://", "xi-net://", "xi-udp://", "xi-emu://")
"""Device URI schemes understood by libximc."""


class XimcOptions(BaseModel):
    """Options for a single libximc device.

    A device is addressed by a full libximc URI. ``port``/``port_env`` are
    convenience shorthands that expand to a ``xi-com://`` URI for the common
    case of a directly attached serial controller.
    """

    uri: str | None = None
    """Full libximc URI, e.g. ``xi-com:///dev/ttyACM0`` or ``xi-emu:///tmp/dev.bin``."""
    port: str | None = None
    """Serial device path, shorthand for ``xi-com://<port>``."""
    port_env: str | None = None
    """Name of an environment variable holding the serial device path."""
    poll_period: float = 0.2
    """Period in seconds between reads of the device."""

    @model_validator(mode="after")
    def resolve_uri(self) -> "XimcOptions":
        """Reduce whichever one of uri, port and port_env was given to a URI."""
        match (self.uri, self.port, self.port_env):
            # URI
            case (str(uri), None, None):
                pass
            # PORT
            case (None, str(port), None):
                uri = f"xi-com://{port}"
            # PORT_ENV
            case (None, None, str(port_env)):
                self.port = _port_from_environment(port_env)
                uri = f"xi-com://{self.port}"
            case (None, None, None):
                raise ValueError("Must specify one of uri, port or port_env")
            case _:
                raise ValueError("Specify only one of uri, port or port_env")

        self.uri = uri
        if not self.uri.startswith(XIMC_URI_SCHEMES):
            raise ValueError(
                f"'{self.uri}' is not a libximc URI - "
                f"expected one of {', '.join(XIMC_URI_SCHEMES)}"
            )
        return self

    @property
    def device_uri(self) -> str:
        """The resolved libximc URI."""
        assert self.uri is not None, "URI not resolved - validation did not run"
        return self.uri

    @property
    def scheme(self) -> Literal["xi-com", "xi-net", "xi-udp", "xi-emu"]:
        """The URI scheme, e.g. ``xi-emu`` for a virtual device."""
        return self.device_uri.split("://", 1)[0]  # type: ignore[return-value]

    @property
    def is_virtual(self) -> bool:
        """Whether this URI addresses a libximc virtual (emulated) device."""
        return self.scheme == "xi-emu"


def _port_from_environment(name: str) -> str:
    """Read a serial device path out of the environment."""
    port = os.environ.get(name)
    if port is None:
        raise ValueError(f"Environment variable '{name}' is not set")
    return port
