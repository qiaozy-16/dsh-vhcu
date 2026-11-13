"""PyInstaller entry point.

Build from the repository root:

    pyinstaller --clean --paths vision vision/vhcu_daemon/entrypoint.py

Keeping this module free of package-relative imports lets PyInstaller discover
``vhcu_daemon`` through ``--paths vision`` and bundle the drivers subpackage.
"""

from vhcu_daemon.cli import main


def run() -> int:
    return main()


if __name__ == "__main__":
    raise SystemExit(run())
