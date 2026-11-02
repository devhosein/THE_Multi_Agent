import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# The engine tests must not depend on whether a browser is installed; tests of the browser check turn it on.
os.environ.setdefault("MC_BROWSER_CHECK", "off")
