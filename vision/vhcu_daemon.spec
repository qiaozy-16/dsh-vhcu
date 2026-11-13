# PyInstaller spec for the VHCU daemon (P1 packaging readiness).
#
# Build from the repository root:
#   pyinstaller --clean vision/vhcu_daemon.spec
#
# Models are deliberately NOT bundled: the host injects VHCU_MODEL_PATH as an
# absolute path at runtime, and the offline model pack is a separate Release
# asset. This is a onedir build (no onefile extraction) so a locked-down TEMP
# directory cannot prevent startup.
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
VISION = ROOT / "vision"

a = Analysis(
    [str(VISION / "vhcu_daemon" / "entrypoint.py")],
    pathex=[str(VISION)],
    hiddenimports=[
        "vhcu_daemon.drivers.windows",
        "vhcu_daemon.drivers.factory",
        "vhcu_daemon.paths",
        "vhcu_daemon.interrupts",
    ],
    excludes=[
        # GUI / notebook / data-science packages: not imported by the daemon.
        "tkinter",
        "_tkinter",
        "pandas",
        "matplotlib",
        "scipy",
        "IPython",
        "jupyter",
        "pytest",
        "notebook",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "wx",
        "torch",
        "tensorflow",
        "sklearn",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="vhcu-daemon",
    debug=False,
    strip=False,
    upx=False,
    console=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="vhcu-daemon",
)
