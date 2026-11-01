#!/usr/bin/env python3
"""Start Mission Control and open the UI.   usage: python scripts/serve.py [--port 8765] [--no-browser]

Works the same on Windows PowerShell, macOS and Linux (no PYTHONPATH juggling needed)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mission_control import utf8_console  # noqa: E402
from mission_control.server import main  # noqa: E402

utf8_console()

if __name__ == "__main__":
    main()
