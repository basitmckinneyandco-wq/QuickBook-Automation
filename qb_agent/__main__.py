"""Command line entry point:

    python -m qb_agent serve      start the dashboard
    python -m qb_agent whoami     print the company file QuickBooks currently has open
    python -m qb_agent check      print the current settings and safety state
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import load_config
from .runtime import Runtime
from .server import serve

def _project_root() -> Path:
    """Where config.json and web/ live.

    Running from source that is the project folder. Built into an .exe there is
    no source tree, so it is the folder the .exe sits in - which keeps
    config.json a plain file the user can edit, rather than something sealed
    inside the executable.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


PROJECT_ROOT = _project_root()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qb_agent", description=__doc__)
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config.json"))
    subparsers = parser.add_subparsers(dest="command")

    serve_parser = subparsers.add_parser("serve", help="Start the local dashboard.")
    serve_parser.add_argument("--open", action="store_true", help="Open the browser automatically.")
    serve_parser.add_argument("--host", default=None)
    serve_parser.add_argument("--port", type=int, default=None)

    subparsers.add_parser("whoami", help="Print the company file QuickBooks has open right now.")
    subparsers.add_parser("check", help="Print the current settings and safety state.")
    subparsers.add_parser("doctor", help="Diagnose why QuickBooks cannot be reached.")

    args = parser.parse_args(argv)
    command = args.command or "serve"

    if command == "whoami":
        return _whoami()
    if command == "doctor":
        return _doctor()

    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Configuration problem: {exc}", file=sys.stderr)
        return 1

    runtime = Runtime(config)

    if command == "check":
        print(json.dumps(runtime.health(), indent=2, default=str))
        return 0

    serve(
        runtime,
        host=getattr(args, "host", None) or config.listen_host,
        port=getattr(args, "port", None) or config.listen_port,
        static_dir=PROJECT_ROOT / "web",
        open_browser=getattr(args, "open", False),
    )
    return 0


def _whoami() -> int:
    """Read-only: opens a session purely to report the open company file."""
    from .quickbooks import QuickBooksError, current_company_file

    try:
        path = current_company_file()
    except QuickBooksError as exc:
        print(f"Could not ask QuickBooks: {exc}", file=sys.stderr)
        return 1
    print("QuickBooks currently has this company file open:\n")
    print(f"  {path}\n")
    print("To work in it, open the dashboard's Status page, pick it under")
    print("Company file and press Switch to this company. Nothing to edit by hand.")
    return 0


def _doctor() -> int:
    """Report the things that actually stop QuickBooks being reached.

    "The company is open, so why does it say it isn't?" has a small number of
    causes, and none of them are visible from the error alone: a mismatch in
    elevation, QuickBooks running as a different Windows user, or the connector
    not being installed. This prints the facts for all of them.
    """
    import ctypes
    import getpass
    import platform

    from .quickbooks import QuickBooksError, current_company_file, quickbooks_windows, sdk_registered

    def line(label: str, value: object) -> None:
        print(f"  {label:<34} {value}")

    print("\n  QuickBooks connection report")
    print("  " + "-" * 54)

    bits = "64-bit" if sys.maxsize > 2**32 else "32-bit"
    line("Python", f"{platform.python_version()} ({bits})")
    line("Windows user running this", getpass.getuser())
    try:
        elevated = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        elevated = False
    line("Running as administrator", "YES" if elevated else "no")

    try:
        import win32com.client  # noqa: F401
        line("pywin32 installed", "yes")
    except ImportError:
        line("pywin32 installed", "NO  -> run: pip install -r requirements.txt")

    line("QuickBooks connector registered", "yes" if sdk_registered() else "NO  -> is QuickBooks Desktop installed?")

    titles = quickbooks_windows()
    main_window = next((t for t in titles if "- Intuit QuickBooks" in t), "")
    if not titles:
        line("QuickBooks windows visible", "NONE")
        print("\n  QuickBooks does not appear to be running for this Windows user.")
        print("  If it IS on screen, it was started by a different user or with")
        print("  'Run as administrator' - and then this app cannot see it.\n")
    else:
        line("QuickBooks windows visible", len(titles))
        line("Company on the title bar", main_window.split("- Intuit QuickBooks")[0].strip() or "(none)")
        if any("No Company Open" in t for t in titles):
            line("WARNING", "the 'No Company Open' window is showing")
        centre = next((t for t in titles if t.startswith(("Customer Cent",))), "")
        if centre:
            line("WARNING", "the Customer Centre is open - close it before sending")

    print()
    try:
        opened = current_company_file()            # test first, then report
    except QuickBooksError as exc:
        line("Session test", "FAILED")
        print(f"\n  {exc}\n")
        if elevated:
            print("  >>> This window is running as administrator, and QuickBooks")
            print("      almost certainly is not. That alone is enough to hide")
            print("      QuickBooks from this app.")
            print("      Close this window. Open PowerShell normally - do NOT")
            print("      right-click 'Run as administrator' - and try again.\n")
        elif not main_window:
            print("  >>> No QuickBooks company window was found. Open QuickBooks")
            print("      Desktop and open the company, then try again.\n")
        return 1
    line("Session test", "OK")
    line("Company file", opened)
    print("\n  QuickBooks can be reached. The app is ready to use.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
