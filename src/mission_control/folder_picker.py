"""Open the operating system's NATIVE folder dialog and print one JSON line.

Runs as a separate process (the server starts it with the same Python), because GUI toolkits want their own
main thread and a crash or a missing display must never take the server down. It only uses the standard
library (tkinter), whose folder dialog is the real native one: the Windows folder picker, the macOS sheet, or
the GTK dialog. The output is pure ASCII JSON so Windows console code pages cannot garble non-Latin paths.

    python folder_picker.py <initial-folder> <dialog-title>
    -> {"status": "ok", "path": "..."} | {"status": "cancel"} | {"status": "error", "message": "..."}"""
import json
import os
import sys


def main() -> None:
    initial = sys.argv[1] if len(sys.argv) > 1 else ""
    title = sys.argv[2] if len(sys.argv) > 2 else "Select the project folder"
    try:
        import tkinter
        from tkinter import filedialog
    except Exception as e:  # tkinter is an optional component of some Python installs
        print(json.dumps({"status": "error", "message": f"tkinter is not available: {e}"}))
        return
    try:
        root = tkinter.Tk()
    except Exception as e:  # e.g. no display on a headless machine
        print(json.dumps({"status": "error", "message": f"cannot open a window: {e}"}))
        return
    chosen = ""
    try:
        root.withdraw()
        try:
            root.attributes("-topmost", True)   # otherwise the dialog can open behind the browser
        except Exception:
            pass
        root.update()
        options = {"parent": root, "title": title, "mustexist": False}
        if initial and os.path.isdir(initial):
            options["initialdir"] = initial
        chosen = filedialog.askdirectory(**options)
    except Exception as e:
        print(json.dumps({"status": "error", "message": str(e)}))
        return
    finally:
        try:
            root.destroy()
        except Exception:
            pass
    print(json.dumps({"status": "ok", "path": os.path.normpath(chosen)}) if chosen else json.dumps({"status": "cancel"}))


if __name__ == "__main__":
    main()
