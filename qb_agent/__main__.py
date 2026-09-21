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

    args = parser.parse_args(argv)
    command = args.command or "serve"

    if command == "whoami":
        return _whoami()

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


if __name__ == "__main__":
    raise SystemExit(main())
