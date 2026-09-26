"""aria2curl -- a transparent, curl-compatible front end for aria2.

``aria2curl`` accepts (a useful subset of) the ``curl`` command line.  When the
command line can be faithfully translated into an ``aria2c`` invocation the
download is handed to aria2 with parallel connections, resumability and a modern
terminal progress display.  When it cannot -- because the arguments use a curl
feature aria2 has no equivalent for, or because the arguments do not even parse
-- the original ``curl`` command is executed untouched via ``execvp``.

The result is a drop-in command: scripting that relies on exact curl behaviour
keeps working, while ordinary downloads get aria2's speed.
"""

from __future__ import annotations

__all__ = ["__version__", "main", "__main__"]

__version__ = "1.0.0"


def main() -> int:
    """Convenience re-export of :func:`aria2curl.cli.main`."""
    from .cli import main as _main

    return _main()
