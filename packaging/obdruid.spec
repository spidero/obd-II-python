# PyInstaller recipe for the standalone builds (result: dist/OBDruid/).
#   Windows: OBDruid.exe      - the desktop app: double click, no console window
#            obdruid-cli.exe  - the command line tool (same commands as `obdruid`)
#   Linux:   obdruid          - one program: no arguments = desktop app, with arguments = command line
# Build from the repository root: pip install ".[gui]" pyinstaller   (Linux: ".[web]" is enough)
#                                 pyinstaller --noconfirm packaging/obdruid.spec
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent
WINDOWS = sys.platform == "win32"

datas = collect_data_files("obdruid")             # web UI, trouble code texts, PID sets
datas += collect_data_files("pint")               # unit definitions used by python-OBD
datas += [(str(ROOT / "LICENSE"), ".")]

hidden = collect_submodules("obdruid")            # transports are found by name at run time
hidden += collect_submodules("serial")            # pyserial URL handlers (socket://) and port listing
hidden += collect_submodules("uvicorn")           # loops and protocols are imported by name
hidden += ["websockets", "websockets.legacy", "websockets.legacy.server"]

excludes = ["tkinter", "pytest", "IPython", "matplotlib", "numpy"]
if not WINDOWS:
    # Linux: a bundled pywebview could not load the system's GTK/WebKit anyway - the app opens the browser
    excludes += ["webview", "gi", "PyQt5", "PyQt6", "PySide6"]

icon = str(ROOT / "packaging" / "obdruid.ico") if WINDOWS else None


def analysis(script):
    return Analysis([str(ROOT / "packaging" / script)], pathex=[str(ROOT / "src")], datas=datas,
                    hiddenimports=hidden, excludes=excludes, noarchive=False)


desktop = analysis("entry_desktop.py")
desktop_exe = EXE(PYZ(desktop.pure), desktop.scripts, [], exclude_binaries=True,
                  name="OBDruid" if WINDOWS else "obdruid", console=not WINDOWS, icon=icon, upx=False)
parts = [desktop_exe, desktop.binaries, desktop.datas]
if WINDOWS:
    # a windowed program cannot print to the terminal, so the command line gets its own exe
    cli = analysis("entry_cli.py")
    parts += [EXE(PYZ(cli.pure), cli.scripts, [], exclude_binaries=True, name="obdruid-cli", console=True,
                  icon=icon, upx=False), cli.binaries, cli.datas]

COLLECT(*parts, name="OBDruid", upx=False)
