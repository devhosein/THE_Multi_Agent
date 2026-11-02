#!/usr/bin/env python3
"""The charts in the README, computed from the scored runs in eval/results (nothing typed in by hand).

    python eval/make_report.py        # writes docs/img/results-*.svg and prints the numbers behind them

Which runs count (all ornith-1.5:35b, same machine, same prompts, scored by eval/score_site.py or score_taskboard.py):
  * Mission Control on the cafe, version by version: eval/results/multi-agent-v0.2 … v0.7, then the benchmark run
  * the three systems: every run made with the browser check in place —
      single agent        bench (4) + single-agent-v0.2 + bench-pilot cafe-solo-1          (the same code throughout)
      single agent+checks bench (2) + bench-pilot3 (4)   (checks from 8b60c01 on; ff38b84 fixed three check bugs)
      Mission Control     bench (1) + bench-pilot3 (3) + multi-agent-v0.5 … v0.7
    Left out, on purpose: runs broken by a bug in the check itself (bench-pilot2: the agent hard-coded the check's
    temporary port; bench-pilot: before the link check), and Mission Control before the browser check (v0.2–v0.4,
    shown in the version chart instead).
Hand-written SVG on a light card, so the charts read the same in GitHub's light and dark themes; colours are the
validated categorical slots (blue, orange, aqua), and every number is also written as text."""
import json
import statistics
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "eval" / "results"
OUT = ROOT / "docs" / "img"
FONT = "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
INK, INK2, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#8a8984", "#e6e5e1", "#fcfcfb"
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"


def score(label: str) -> dict:
    d = json.loads((RES / label / "score.json").read_text(encoding="utf-8"))
    return {"automatic": d["automatic_total"], "points": d["points"]}


def rows(folder: str) -> list[dict]:
    p = RES / folder / "runs.jsonl"
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()] if p.exists() else []


def systems() -> dict[str, list[dict]]:
    bench, pilot, pilot3 = rows("bench"), rows("bench-pilot"), rows("bench-pilot3")
    solo = [r for r in bench if r["system"] == "solo"] + [score("single-agent-v0.2")] + \
           [r for r in pilot if r["system"] == "solo"]
    check = [r for r in bench + pilot3 if r["system"] == "solo-check"]
    mc = [r for r in bench + pilot3 if r["system"] == "mc"] + [score(f"multi-agent-v0.{i}") for i in (5, 6, 7)]
    return {"solo": solo, "check": check, "mc": mc}


def stats(rs: list[dict]) -> dict:
    a = [r["automatic"] for r in rs]
    works = sum(1 for r in rs if r["points"].get("F4") == 15 and r["points"].get("F5") == 15)
    clean = sum(1 for r in rs if r["points"].get("F7") == 5)
    return {"n": len(a), "mean": statistics.mean(a), "min": min(a), "max": max(a), "works": works, "clean": clean}


def card(w: int, h: int, body: str, title: str, subtitle: str) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
            f'font-family="{FONT}" role="img" aria-label="{escape(title)}">'
            f'<rect x="0.5" y="0.5" width="{w - 1}" height="{h - 1}" rx="14" fill="{SURFACE}" stroke="{GRID}"/>'
            f'<text x="28" y="40" font-size="19" font-weight="650" fill="{INK}">{escape(title)}</text>'
            f'<text x="28" y="63" font-size="13" fill="{INK2}">{escape(subtitle)}</text>{body}</svg>')


def column(x: float, base: float, w: float, h: float, color: str) -> str:
    """A column with a 4px rounded data-end and a square base."""
    r = min(4, h)
    return (f'<path d="M{x},{base} V{base - h + r} Q{x},{base - h} {x + r},{base - h} H{x + w - r} '
            f'Q{x + w},{base - h} {x + w},{base - h + r} V{base} Z" fill="{color}"/>')


def grid(left: int, right: int, sy) -> str:
    return "".join(f'<line x1="{left}" x2="{right}" y1="{sy(v):.1f}" y2="{sy(v):.1f}" stroke="{GRID}"/>'
                   f'<text x="{left - 10}" y="{sy(v) + 4:.1f}" font-size="12" fill="{MUTED}" text-anchor="end">{v}</text>'
                   for v in (0, 20, 40, 60, 80))


def headline(s: dict) -> str:
    solo, mc = stats(s["solo"]), stats(s["mc"])
    first = score("multi-agent-v0.2")["automatic"]
    tiles = [(f"{first} → {mc['max']}", "on the cafe task, out of 85",
              "Mission Control, first run → best run"),
             (f"{mc['clean']}/{mc['n']}", "runs without a single JavaScript error",
              f"plain single agent: {solo['clean']}/{solo['n']}"),
             (f"{mc['mean'] - solo['mean']:+.0f}", "points vs a plain single agent",
              f"mean {mc['mean']:.0f} vs {solo['mean']:.0f} · worst {mc['min']} vs {solo['min']}")]
    w, tw = 760, 229
    body = []
    for i, (big, label, sub) in enumerate(tiles):
        x = 28 + i * (tw + 8)
        body.append(f'<rect x="{x}" y="80" width="{tw}" height="96" rx="10" fill="#ffffff" stroke="{GRID}"/>'
                    f'<rect x="{x}" y="80" width="5" height="96" rx="2" fill="{BLUE}"/>'
                    f'<text x="{x + 20}" y="121" font-size="32" font-weight="700" fill="{INK}">{escape(big)}</text>'
                    f'<text x="{x + 20}" y="145" font-size="12.5" fill="{INK}">{escape(label)}</text>'
                    f'<text x="{x + 20}" y="163" font-size="11.5" fill="{MUTED}">{escape(sub)}</text>')
    return card(w, 200, "".join(body), "A local 35B model that builds web apps which actually work",
                "ornith-1.5:35b on one GPU via Ollama · scored by a real browser, not by the agents")


VERSIONS = [("v0.2", "multi-agent-v0.2", "first run"), ("v0.3", "multi-agent-v0.3", "team, output cap"),
            ("v0.4", "multi-agent-v0.4", "(a bad run)"), ("v0.5", "multi-agent-v0.5", "browser check"),
            ("v0.6", "multi-agent-v0.6", "history fix"), ("v0.7", "multi-agent-v0.7", "CSS checks")]


def progress(solo_mean: float) -> str:
    bench = [r for r in rows("bench") if r["system"] == "mc" and r["task"] == "cafe"]
    pts = [(v, score(label)["automatic"], note) for v, label, note in VERSIONS]
    if bench:
        pts.append(("v1.0", bench[0]["automatic"], "link, data checks"))
    w, h, top, bottom, left = 760, 400, 104, 320, 64
    sy = lambda v: bottom - v / 85 * (bottom - top)                                          # noqa: E731
    body = [grid(left, w - 28, sy)]
    step = (w - 28 - left) / len(pts)
    for i, (v, a, note) in enumerate(pts):
        x = left + i * step + step / 2 - 14
        body.append(column(x, bottom, 28, bottom - sy(a), BLUE))
        body.append(f'<text x="{x + 14}" y="{sy(a) - 8:.1f}" font-size="14" font-weight="650" fill="{INK}" '
                    f'text-anchor="middle">{a}</text>'
                    f'<text x="{x + 14}" y="{bottom + 22}" font-size="13" font-weight="600" fill="{INK}" '
                    f'text-anchor="middle">{v}</text>'
                    f'<text x="{x + 14}" y="{bottom + 40}" font-size="11" fill="{MUTED}" text-anchor="middle">'
                    f'{escape(note)}</text>')
    y = sy(solo_mean)
    body.append(f'<line x1="{left}" x2="{w - 28}" y1="{y:.1f}" y2="{y:.1f}" stroke="{AQUA}" stroke-width="2"/>'
                f'<line x1="{w - 250}" x2="{w - 232}" y1="80" y2="80" stroke="{AQUA}" stroke-width="2"/>'
                f'<text x="{w - 226}" y="84" font-size="12" fill="{INK2}">plain single agent, mean {solo_mean:.0f}</text>')
    body.append(f'<line x1="{left}" x2="{w - 28}" y1="{bottom}" y2="{bottom}" stroke="{MUTED}"/>')
    return card(w, h, "".join(body), "Mission Control on the cafe task, version by version",
                "Automatic score out of 85, one live run per version; each step fixes a failure seen in the run before")


def compare(s: dict) -> str:
    sets = [("Single agent", AQUA, stats(s["solo"])), ("Single agent + our checks", ORANGE, stats(s["check"])),
            ("Mission Control", BLUE, stats(s["mc"]))]
    w, h, top, bottom, left = 760, 430, 92, 300, 64
    sy = lambda v: bottom - v / 85 * (bottom - top)                                          # noqa: E731
    body = [grid(left, w - 28, sy)]
    step = (w - 28 - left) / len(sets)
    for i, (name, color, st) in enumerate(sets):
        cx = left + i * step + step / 2
        body.append(column(cx - 14, bottom, 28, bottom - sy(st["mean"]), color))
        body.append(f'<line x1="{cx}" x2="{cx}" y1="{sy(st["min"]):.1f}" y2="{sy(st["max"]):.1f}" stroke="{INK}" '
                    f'stroke-width="1.5" opacity="0.55"/>')
        for v in (st["min"], st["max"]):
            body.append(f'<line x1="{cx - 6}" x2="{cx + 6}" y1="{sy(v):.1f}" y2="{sy(v):.1f}" stroke="{INK}" '
                        f'stroke-width="1.5" opacity="0.55"/>')
        body.append(f'<text x="{cx + 12}" y="{sy(st["mean"]) - 6:.1f}" font-size="14" font-weight="650" fill="{INK}">'
                    f'{st["mean"]:.0f}</text>'
                    f'<rect x="{cx - 70}" y="{bottom + 14}" width="10" height="10" rx="2" fill="{color}"/>'
                    f'<text x="{cx - 54}" y="{bottom + 24}" font-size="13" font-weight="600" fill="{INK}">{escape(name)}</text>'
                    f'<text x="{cx - 70}" y="{bottom + 46}" font-size="12" fill="{INK2}">every feature works: '
                    f'{st["works"]}/{st["n"]} runs</text>'
                    f'<text x="{cx - 70}" y="{bottom + 64}" font-size="12" fill="{INK2}">no JS errors: '
                    f'{st["clean"]}/{st["n"]} runs</text>'
                    f'<text x="{cx - 70}" y="{bottom + 82}" font-size="12" fill="{INK2}">worst run: {st["min"]}</text>')
    body.append(f'<line x1="{left}" x2="{w - 28}" y1="{bottom}" y2="{bottom}" stroke="{MUTED}"/>')
    body.append(f'<text x="28" y="{h - 14}" font-size="11.5" fill="{MUTED}">Mean score out of 85 (whisker: worst to best '
                'run), cafe and task board runs together. Which runs count: eval/make_report.py.</text>')
    return card(w, h, "".join(body), "Same model, three set-ups",
                "The checks make the result dependable; on these small apps, extra agents add little")


def main() -> None:
    s = systems()
    OUT.mkdir(parents=True, exist_ok=True)
    solo_mean = stats(s["solo"])["mean"]
    for name, svg in (("results-headline", headline(s)), ("results-versions", progress(solo_mean)),
                      ("results-systems", compare(s))):
        (OUT / f"{name}.svg").write_text(svg, encoding="utf-8", newline="\n")
    for k, rs in s.items():
        print(k, stats(rs), [r["automatic"] for r in rs])


if __name__ == "__main__":
    main()
