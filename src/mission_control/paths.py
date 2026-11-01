"""Turning whatever the user pasted into a usable folder path.

Windows' "Copy as path" wraps the path in double quotes, Explorer's address bar may give forward slashes or a
trailing backslash, people paste `%USERPROFILE%\\code`, `~/code`, or a `file:///C:/...` link from a browser.
This is pure string handling (no filesystem access) so it behaves the same on every OS and is easy to test."""
from __future__ import annotations

import os
import re
from urllib.parse import unquote

_QUOTES = {'"': '"', "'": "'", "“": "”", "‘": "’", "«": "»", "„": "“"}
_ENV = re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%")
_DRIVE_ROOT = re.compile(r"^[A-Za-z]:[/\\]?$")


def clean_path_input(raw: str | None) -> str:
    s = (raw or "").strip()
    while len(s) >= 2 and s[0] in _QUOTES and s[-1] == _QUOTES[s[0]]:      # "C:\My Folder"  ->  C:\My Folder
        s = s[1:-1].strip()
    if s.lower().startswith("file://"):                                      # file:///C:/x  or  file:///home/x
        rest = unquote(s[7:])
        if rest.lower().startswith("localhost/"):
            rest = rest[len("localhost"):]
        if re.match(r"^/[A-Za-z]:[/\\]", rest):
            rest = rest[1:]
        s = rest
    s = _ENV.sub(lambda m: os.environ.get(m.group(1), m.group(0)), s)        # %USERPROFILE%  (any OS)
    s = os.path.expandvars(os.path.expanduser(s))                            # ~  and  $HOME
    if len(s) > 1 and not _DRIVE_ROOT.match(s):                              # keep C:\ and /, drop other trailing slashes
        s = s.rstrip("/\\") or s
    return s


def looks_absolute(path: str) -> bool:
    """True for POSIX absolute, drive-letter (C:\\ or C:/) and UNC (\\\\server\\share) paths on ANY platform."""
    return path.startswith(("/", "\\\\")) or bool(re.match(r"^[A-Za-z]:[/\\]", path))
