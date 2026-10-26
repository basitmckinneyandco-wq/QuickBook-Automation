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
    subparsers.add_parser("sheet", help="Diagnose why the Google Sheet is not updating.")

    reset_parser = subparsers.add_parser(
        "reset", help="Put the four status columns back to Pending / Not needed."
    )
    reset_parser.add_argument(
        "--write", action="store_true",
        help="Actually write. Without it nothing is changed and the plan is printed.",
    )

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

    if command == "sheet":
        return _sheet(config)

    if command == "reset":
        return _reset(runtime, bool(getattr(args, "write", False)))

    # A transferred installation intentionally starts with no company path.
    # In that state the only safe choice is the company QuickBooks already has
    # open on this Windows machine.  Ask QuickBooks for its actual local/UNC
    # path and save that path for this copy; never assume another computer's
    # drive letter.  If QuickBooks is not open yet, the dashboard still starts
    # and its Status page lets the user select the open company afterwards.
    if command == "serve" and config.mode == "quickbooks" and not config.company.file_path.strip():
        try:
            runtime.use_company()
        except Exception:
            pass

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


def _sheet(config) -> int:
    """Report why the four status columns are not reaching the sheet.

    "It works on one computer and not the other" has a handful of causes, and
    every one of them is invisible from the dashboard, which only says the
    write failed. This names the one that applies.

    Read-only throughout: it signs in, lists the tabs and counts the rows.
    Nothing in the sheet is changed by running it.
    """
    from .gsheets import Sheets, SheetsError

    settings = config.google_sheet

    def line(label: str, value: object) -> None:
        print(f"  {label:<30} {value}")

    print("\n  Google Sheet connection report")
    print("  " + "-" * 56)

    if not settings.enabled:
        print("\n  The sheet is switched off in the settings: either the")
        print("  spreadsheet id or the key file name is blank in config.json.\n")
        return 1

    # Printed whether or not anything is wrong: the key is signed with this
    # clock, so a report that leaves it out cannot be read by anyone later.
    import datetime
    line("Clock on this computer", datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %z"))

    try:
        import google.oauth2.service_account  # noqa: F401
        line("Google libraries installed", "yes")
    except ImportError:
        line("Google libraries installed", "NO")
        print("\n  >>> This is the whole problem. In this folder run:")
        print("          pip install -r requirements.txt\n")
        return 1

    key = Path(settings.key_file)
    line("Key file expected at", key)
    if not key.is_file():
        line("Key file found", "NO")
        print("\n  >>> Put the service-account.json file in that exact folder.")
        near = sorted(p.name for p in key.parent.glob("service-account*"))
        if near:
            # Windows hides known extensions, so service-account.json.txt looks
            # right in Explorer and is the usual reason for this.
            print(f"      Something close is there already: {', '.join(near)}")
            print("      The name must end in .json and nothing after it.")
        print()
        return 1
    line("Key file found", "yes")

    try:
        sheets = Sheets(settings.key_file, settings.spreadsheet_id)
        line("Signed in as", sheets.account_email)
        titles = sheets.tab_titles()
    except SheetsError as exc:
        line("Signed in as", "FAILED")
        print(f"\n  {exc}\n")
        return 1

    line("Tabs in the spreadsheet", len(titles))
    missing = []
    for label, title in settings.tab_titles.items():
        if title in titles:
            rows = len(sheets.read(title))
            line(f"  {label}", f"{title} - {rows} rows, readable")
        else:
            missing.append(title)
            line(f"  {label}", f"{title} - NOT IN THIS SPREADSHEET")

    if missing:
        print("\n  >>> The tab names in config.json do not match the sheet.")
        print(f"      The spreadsheet has: {', '.join(titles)}\n")
        return 1

    line("Writing statuses back", "on" if settings.write_status else "OFF in config.json")
    print("\n  The sheet can be read and the account is signed in. If statuses")
    print("  still do not appear, the account above needs Editor access, not")
    print("  Viewer - check the Share list on the sheet itself.\n")
    return 0


def _reset(runtime, write: bool) -> int:
    """Clear the four status columns on the rows the app works with.

    Printed and not written unless --write is given. The sheet is shared, the
    change covers every loaded row at once, and there is no undo here - Google
    Sheets' own File > Version history is the way back.
    """
    from .gsheets import SheetsError

    print("\n  Reading the policies...")
    try:
        runtime.fetch_web()
    except Exception as exc:
        print(f"\n  The policies could not be read: {exc}\n", file=sys.stderr)
        return 1

    try:
        summary = runtime.reset_statuses(write=write)
    except SheetsError as exc:
        print(f"\n  {exc}\n", file=sys.stderr)
        return 1

    print(f"\n  {summary['rows']} policies loaded, {summary['cells']} cells to set:\n")
    for column, words in summary["counts"].items():
        parts = ", ".join(f"{count} {word}" for word, count in sorted(words.items()))
        print(f"    {column:<18} {parts}")

    if summary["unplaced"]:
        print(
            f"\n  {len(summary['unplaced'])} policies were skipped: their row in the sheet"
            "\n  could not be identified, and writing to a guessed row is worse than"
            "\n  not writing at all. "
            + ", ".join(summary["unplaced"][:5])
            + ("..." if len(summary["unplaced"]) > 5 else "")
        )

    if not write:
        print("\n  Nothing was changed. To apply it:\n")
        print("      python -m qb_agent reset --write\n")
        return 0

    print(f"\n  Done: {summary['written']} cells written, and this machine's record")
    print("  of what it has sent was cleared for those policies.\n")
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
