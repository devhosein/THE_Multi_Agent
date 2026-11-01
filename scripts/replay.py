#!/usr/bin/env python3
"""Replay an events.jsonl in the terminal with the original timing - the text-mode twin of the UI.

usage: python scripts/replay.py [events.jsonl] [--speed 4] [--no-chunks]
       --speed 0  prints instantly
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mission_control.eventlog import read_events, replay  # noqa: E402
from mission_control.textview import format_event  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("path", nargs="?", default=str(ROOT / "sample" / "mock_run.jsonl"))
ap.add_argument("--speed", type=float, default=4.0)
ap.add_argument("--no-chunks", action="store_true", help="hide file.chunk lines")
args = ap.parse_args()

for ev in replay(read_events(args.path), speed=args.speed):
    out = format_event(ev, show_chunks=not args.no_chunks)
    if out:
        print(out, flush=True)
