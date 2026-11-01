"""mission_control: engine core (Phase 0) - event contract, plan model, event log, LLM client."""
import sys

__version__ = "0.2.0"


def utf8_console() -> None:
    """Make the command-line scripts print UTF-8. On Windows, output redirected to a file or a pipe uses a legacy
    code page (cp1252) that cannot encode Persian or the symbols the text view uses, and printing then crashes."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass   # replaced or already-closed stream: leave it alone
