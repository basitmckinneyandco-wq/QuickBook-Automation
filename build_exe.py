"""Build QBAutomation.exe, so a colleague needs no Python at all.

Run this on a Windows machine that already runs the app from source:

    pip install pyinstaller
    python build_exe.py

It produces  dist/QuickBooks Automation/  - a folder to zip and hand over.
The .exe is self-contained, but config.json and web/ sit beside it as ordinary
files so settings can be edited and the dashboard tweaked without rebuilding.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "dist" / "QuickBooks Automation"
APP_NAME = "QBAutomation"


def build() -> int:
    for folder in (ROOT / "build", ROOT / "dist"):
        shutil.rmtree(folder, ignore_errors=True)

    result = subprocess.run(
        [
            sys.executable, "-m", "PyInstaller",
            "--noconfirm",
            "--onefile",
            "--name", APP_NAME,
            "--console",                     # the window is the app; closing it stops the server
            # pywin32's COM layer is imported dynamically, so name it explicitly.
            "--hidden-import", "win32com.client",
            "--hidden-import", "pythoncom",
            "--hidden-import", "pywintypes",
            str(ROOT / "run_dashboard.py"),
        ],
        cwd=ROOT,
    )
    if result.returncode != 0:
        print("\nPyInstaller failed.")
        return result.returncode

    OUT.mkdir(parents=True, exist_ok=True)
    shutil.move(str(ROOT / "dist" / f"{APP_NAME}.exe"), str(OUT / f"{APP_NAME}.exe"))
    shutil.copytree(ROOT / "web", OUT / "web")
    shutil.copy2(ROOT / "config.example.json", OUT / "config.example.json")
    shutil.copy2(ROOT / "SETUP.md", OUT / "SETUP.md")
    (OUT / "Start Dashboard.cmd").write_text(
        "@echo off\r\n"
        "cd /d \"%~dp0\"\r\n"
        f"\"{APP_NAME}.exe\"\r\n"
        "pause\r\n",
        encoding="utf-8",
    )

    print(f"\nBuilt: {OUT}")
    print("Zip that folder and send it. No Python needed on the other machine.")
    return 0


if __name__ == "__main__":
    raise SystemExit(build())
