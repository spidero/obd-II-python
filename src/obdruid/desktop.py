"""Entry point of the desktop builds (Windows exe, AppImage): start the app without typing a command.

With arguments it behaves like the `obdruid` command, so one program serves both uses where
there is a console.
"""

from __future__ import annotations

import os
import sys


def _log_when_there_is_no_console() -> None:
    """A windowed Windows program has no stdout/stderr: write messages to a log file instead."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    from .core.storage import default_data_dir

    try:
        folder = default_data_dir()
        folder.mkdir(parents=True, exist_ok=True)
        log = open(folder / "obdruid.log", "w", encoding="utf-8", buffering=1)
    except OSError:
        log = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = sys.stdout or log
    sys.stderr = sys.stderr or log


def main() -> None:
    _log_when_there_is_no_console()
    if len(sys.argv) > 1:
        from .cli import run as run_cli

        run_cli()
        return
    from .core import ConnectionSettings
    from .gui import run

    run(ConnectionSettings())


if __name__ == "__main__":
    main()
