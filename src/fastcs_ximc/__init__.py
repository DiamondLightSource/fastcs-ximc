"""FastCS support for XIMC motion controllers.

.. data:: __version__
    :type: str

    Version number as calculated by https://github.com/pypa/setuptools_scm
"""

from ._version import __version__
from .config import XimcOptions
from .controller import MotionInhibitedError, XimcController
from .device import XimcDevice
from .io import XimcSettingsIO, XimcSettingsIORef
from .utils import DeviceNotFoundError, enumerate_device_uris, patch_strict_flags

__all__ = [
    "DeviceNotFoundError",
    "MotionInhibitedError",
    "XimcController",
    "XimcDevice",
    "XimcOptions",
    "XimcSettingsIO",
    "XimcSettingsIORef",
    "__version__",
    "enumerate_device_uris",
    "patch_strict_flags",
]
