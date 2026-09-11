"""Interface for ``python -m fastcs_ximc``."""

from fastcs.launch import launch

from . import __version__
from .connections import XimcConnection, XimcDRAConnection
from .controller import XimcController

__all__ = ["main"]


def main() -> None:
    """Entry point for the fastcs-ximc CLI."""
    launch(
        XimcController,
        version=__version__,
        connection_classes=[XimcConnection, XimcDRAConnection],
    )


if __name__ == "__main__":
    main()
