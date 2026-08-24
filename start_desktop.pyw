"""Double-click launcher and frozen executable dispatcher."""

import io
import os
import sys


def _restore_frozen_stdout() -> None:
    """Attach a windowed child process to TaskManager's inherited output pipe."""
    if sys.stdout is not None or os.name != "nt":
        return
    try:
        import ctypes
        import msvcrt

        handle = ctypes.windll.kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        if handle in (0, -1):
            return
        descriptor = msvcrt.open_osfhandle(handle, os.O_WRONLY)
        stream = os.fdopen(descriptor, "wb", closefd=True)
        sys.stdout = io.TextIOWrapper(stream, encoding="utf-8", line_buffering=True)
        sys.stderr = sys.stdout
    except (OSError, ValueError):
        pass


def main() -> None:
    if getattr(sys, "frozen", False) and len(sys.argv) > 1 and sys.argv[1] == "--cli":
        _restore_frozen_stdout()
        sys.argv = [sys.argv[0], *sys.argv[2:]]
        from novel_cli import main as cli_main

        cli_main()
        return

    from webui.desktop import main as desktop_main

    desktop_main()


if __name__ == "__main__":
    main()
