#!/usr/bin/env python3
"""Generate the mock run used to build the UI.   usage: python scripts/gen_mock.py [out.jsonl] [--lang=en|fa]"""
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mission_control import utf8_console  # noqa: E402
from mission_control.events import check_stream  # noqa: E402
from mission_control.mock import build_mock_run  # noqa: E402

utf8_console()

args = [a for a in sys.argv[1:] if not a.startswith("--lang=")]
lang = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--lang=")), "en")
out = Path(args[0]) if args else ROOT / "sample" / "mock_run.jsonl"
out.parent.mkdir(parents=True, exist_ok=True)
events = build_mock_run(out, lang=lang)
problems = check_stream(events)
if problems:
    print("INVALID STREAM:\n" + "\n".join(problems))
    sys.exit(1)
print(f"wrote {len(events)} events -> {out}")
for t, n in sorted(Counter(e.type for e in events).items()):
    print(f"  {t:22s} {n}")
