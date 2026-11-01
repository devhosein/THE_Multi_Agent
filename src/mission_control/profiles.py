"""Model strength profiles - the "how capable is this model" dial.

The engine must shine with strong models and still finish the job with weak ones. Instead of weakening the
code for everyone, the profile changes HOW MUCH SCAFFOLDING a model gets:

  small   (< 14B parameters)  more steps and attempts, smaller reads, step-by-step instructions with an example,
                               smaller plans, low temperature, the lead tries harder to recover on its own
  medium  (14B - 39B)         in between
  large   (>= 40B)            terse instructions, bigger plans and reads, more freedom

"auto" picks the profile from the parameter size Ollama reports for the model (e.g. "9.2B" -> small).
The context window is NOT part of a profile: by default the engine sends no limit and Ollama decides."""
from __future__ import annotations

import re
from dataclasses import dataclass

NAMES = ("small", "medium", "large")


@dataclass(frozen=True)
class Profile:
    name: str
    max_steps: int           # model calls per worker attempt
    max_attempts: int        # attempts per task before the lead steps in
    recoveries: int          # how many times the lead may try to rescue a failed task on its own
    read_chars: int          # largest file excerpt returned by read_file
    min_tasks: int
    max_tasks: int
    temperature: float
    detailed: bool           # step-by-step worker instructions with a worked example

    def describe(self) -> str:
        return f"{self.name} (attempts per task {self.max_attempts}, lead rescues {self.recoveries}, plan {self.min_tasks}-{self.max_tasks} tasks)"


PROFILES: dict[str, Profile] = {
    # max_steps is only a safety net (loops are detected separately); it is not a work budget.
    "small": Profile("small", 80, 3, 2, 12_000, 3, 6, 0.15, True),
    "medium": Profile("medium", 80, 2, 1, 20_000, 3, 8, 0.2, True),
    "large": Profile("large", 100, 2, 1, 30_000, 3, 10, 0.3, False),
}


def parse_billions(parameter_size: str) -> float | None:
    """"9.2B" -> 9.2, "35B" -> 35, "500M" -> 0.5, "" -> None."""
    m = re.match(r"^\s*([\d.]+)\s*([KMBT])", (parameter_size or "").upper())
    if not m:
        return None
    n = float(m.group(1))
    return n * {"K": 1e-6, "M": 1e-3, "B": 1.0, "T": 1e3}[m.group(2)]


def detect(parameter_size: str, model_name: str = "") -> str:
    """Strength from Ollama's parameter size; falls back to a size tag in the name (":9b", "-35b"), else medium."""
    b = parse_billions(parameter_size)
    if b is None:
        m = re.search(r"[:\-_](\d+(?:\.\d+)?)b\b", model_name.lower())
        b = float(m.group(1)) if m else None
    if b is None:
        return "medium"
    return "small" if b < 14 else "medium" if b < 40 else "large"


def resolve(choice: str, parameter_size: str = "", model_name: str = "") -> Profile:
    """An explicit choice wins over auto-detection."""
    return PROFILES[choice if choice in NAMES else detect(parameter_size, model_name)]
