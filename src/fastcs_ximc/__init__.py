"""FastCS support for XIMC motion controllers.

.. data:: __version__
    :type: str

    Version number as calculated by https://github.com/pypa/setuptools_scm
"""

from ._version import __version__
from .config import XimcConnectionSettings, XimcOptions
from .connections import XimcConnection, XimcDRAConnection
from .controller import MotionInhibitedError, XimcController
from .utils import DeviceNotFoundError, enumerate_device_uris, patch_strict_flags

__all__ = [
    "DeviceNotFoundError",
    "MotionInhibitedError",
    "XimcConnection",
    "XimcConnectionSettings",
    "XimcDRAConnection",
    "XimcController",
    "XimcOptions",
    "__version__",
    "enumerate_device_uris",
    "patch_strict_flags",
]
