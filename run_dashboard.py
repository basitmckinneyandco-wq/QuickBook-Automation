"""What the built .exe runs: start the dashboard and open the browser.

Kept separate from qb_agent/__main__.py because PyInstaller needs a plain
script as its entry point, and because the .exe should just start - a colleague
double-clicks it rather than typing a subcommand.
"""

from __future__ import annotations

import sys
from pathlib import Path

from qb_agent.__main__ import main

if __name__ == "__main__":
    args = sys.argv[1:]
    # Double-clicked with no arguments: behave like "serve --open".
    if not args:
        args = ["serve", "--open"]
    try:
        raise SystemExit(main(args))
    except KeyboardInterrupt:
        raise SystemExit(0)
    except Exception as exc:                       # never vanish without a word
        print(f"\n  The dashboard could not start: {exc}\n")
        config = Path(sys.executable).resolve().parent / "config.json"
        if not config.is_file():
            print("  There is no config.json next to the program.")
            print("  Copy config.example.json to config.json and fill it in.\n")
        input("  Press Enter to close.")
        raise SystemExit(1)
