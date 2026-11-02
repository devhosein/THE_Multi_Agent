#!/usr/bin/env python3
"""Turn eval/results/bench/runs.jsonl into the charts and gallery shown in the README.

    python eval/make_report.py            # writes eval/results/bench/*.svg, gallery_*.png and summary.md

Charts are hand-written SVG (no plotting library): a fixed light card, so they read the same in GitHub's light and dark
themes. Colours: the validated categorical slots 1-3 (blue, orange, aqua), always in the same order per system;
every value is also written as text, so nothing depends on colour alone."""
import json
import statistics
import sys
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "eval" / "results" / "bench"
SYSTEMS = [("mc", "Mission Control", "#2a78d6"), ("solo-check", "Single agent + our checks", "#eb6834"),
           ("solo", "Single agent", "#1baf7a")]
TASKS = [("cafe", "Cafe website"), ("taskboard", "Task board")]
CRITERIA = [("F1", "Backend starts, stdlib only", 10), ("F2", "API answers with JSON", 10),
            ("F3", "Page loads (story & hours / form)", 10), ("F4", "Core feature works in the browser", 15),
            ("F5", "Second feature works in the browser", 15), ("F6", "Invalid input rejected", 5),
            ("F7", "No JS errors, no failed requests", 5), ("Q1", "No sideways scroll on a phone", 5),
            ("Q2", "Its own tests pass", 5), ("Q3", "Says how to run it", 5)]
FONT = "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
INK, INK2, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#8a8984", "#e6e5e1", "#fcfcfb"


def load() -> list[dict]:
    return [json.loads(line) for line in (BENCH / "runs.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]


def card(w: int, h: int, body: str, title: str, subtitle: str) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
            f'font-family="{FONT}" role="img" aria-label="{escape(title)}">'
            f'<rect x="0.5" y="0.5" width="{w - 1}" height="{h - 1}" rx="14" fill="{SURFACE}" stroke="{GRID}"/>'
            f'<text x="28" y="40" font-size="19" font-weight="650" fill="{INK}">{escape(title)}</text>'
            f'<text x="28" y="63" font-size="13" fill="{INK2}">{escape(subtitle)}</text>{body}</svg>')


def legend(x: int, y: int) -> str:
    out, cx = [], x
    for _, name, color in SYSTEMS:
        out.append(f'<rect x="{cx}" y="{y - 10}" width="12" height="12" rx="3" fill="{color}"/>'
                   f'<text x="{cx + 18}" y="{y}" font-size="13" fill="{INK2}">{escape(name)}</text>')
        cx += 30 + len(name) * 7
    return "".join(out)


def column(x: float, y_base: float, w: float, h: float, color: str) -> str:
    """A column with a 4px rounded data-end and a square base."""
    r = min(4, h)
    return (f'<path d="M{x},{y_base} V{y_base - h + r} Q{x},{y_base - h} {x + r},{y_base - h} H{x + w - r} '
            f'Q{x + w},{y_base - h} {x + w},{y_base - h + r} V{y_base} Z" fill="{color}"/>')


def score_chart(rows: list[dict]) -> str:
    w, h, top, bottom, left = 760, 430, 120, 360, 64
    ymax = 85
    sy = lambda v: bottom - v / ymax * (bottom - top)                                   # noqa: E731
    body = [legend(28, 95)]
    for v in (0, 20, 40, 60, 80):
        body.append(f'<line x1="{left}" x2="{w - 28}" y1="{sy(v)}" y2="{sy(v)}" stroke="{GRID}"/>'
                    f'<text x="{left - 10}" y="{sy(v) + 4}" font-size="12" fill="{MUTED}" text-anchor="end">{v}</text>')
    body.append(f'<line x1="{left}" x2="{w - 28}" y1="{sy(85)}" y2="{sy(85)}" stroke="{MUTED}" stroke-dasharray="0"/>'
                f'<text x="{w - 28}" y="{sy(85) - 6}" font-size="11" fill="{MUTED}" text-anchor="end">max 85</text>')
    group_w = (w - 28 - left) / len(TASKS)
    bw, gap = 24, 22
    for gi, (task, tname) in enumerate(TASKS):
        gx = left + gi * group_w + group_w / 2 - (len(SYSTEMS) * bw + (len(SYSTEMS) - 1) * gap) / 2
        for si, (sys_id, _, color) in enumerate(SYSTEMS):
            vals = [r["automatic"] for r in rows if r["task"] == task and r["system"] == sys_id and r.get("automatic") is not None]
            if not vals:
                continue
            m = statistics.mean(vals)
            x = gx + si * (bw + gap)
            body.append(column(x, bottom, bw, bottom - sy(m), color))
            if len(vals) > 1:                       # the spread over the runs: min-max whisker
                body.append(f'<line x1="{x + bw / 2}" x2="{x + bw / 2}" y1="{sy(min(vals))}" y2="{sy(max(vals))}" '
                            f'stroke="{INK}" stroke-width="1.5" opacity="0.55"/>')
                for v in (min(vals), max(vals)):
                    body.append(f'<line x1="{x + bw / 2 - 5}" x2="{x + bw / 2 + 5}" y1="{sy(v)}" y2="{sy(v)}" '
                                f'stroke="{INK}" stroke-width="1.5" opacity="0.55"/>')
            body.append(f'<text x="{x + bw / 2}" y="{sy(max(vals)) - 9}" font-size="14" font-weight="650" fill="{INK}" '
                        f'text-anchor="middle">{m:.0f}</text>')
        body.append(f'<text x="{left + gi * group_w + group_w / 2}" y="{bottom + 30}" font-size="15" font-weight="600" '
                    f'fill="{INK}" text-anchor="middle">{escape(tname)}</text>')
    body.append(f'<line x1="{left}" x2="{w - 28}" y1="{bottom}" y2="{bottom}" stroke="{MUTED}"/>')
    n = max((r["run"] for r in rows), default=0)
    body.append(f'<text x="28" y="{h - 16}" font-size="12" fill="{MUTED}">Mean of {n} runs per bar; the whisker spans the '
                'worst and best run. Measured by a real browser, not judged.</text>')
    return card(w, h, "".join(body), "Automatic score out of 85", "Same model (ornith-1.5:35b), same machine, same prompts")


def ramp(p: float) -> tuple[str, str]:
    """Sequential blue for 'share of the points earned'; text colour that stays readable on it."""
    steps = ["#f3f3f1", "#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95"]
    i = min(len(steps) - 1, int(round(p * (len(steps) - 1))))
    return steps[i], ("#ffffff" if i >= 4 else INK)


def criteria_chart(rows: list[dict]) -> str:
    cols = [(t, s) for t, _ in TASKS for s, _, _ in SYSTEMS]
    left, top, cw, ch = 300, 128, 72, 30
    w = left + cw * len(cols) + 28
    h = top + ch * len(CRITERIA) + 60
    body = []
    for ti, (task, tname) in enumerate(TASKS):
        x0 = left + ti * cw * len(SYSTEMS)
        body.append(f'<text x="{x0 + cw * len(SYSTEMS) / 2}" y="96" font-size="14" font-weight="650" fill="{INK}" '
                    f'text-anchor="middle">{escape(tname)}</text>')
        for si, (_, name, color) in enumerate(SYSTEMS):
            x = x0 + si * cw + cw / 2
            short = {"mc": "MC", "solo-check": "Solo+chk", "solo": "Solo"}[SYSTEMS[si][0]]
            body.append(f'<rect x="{x - 26}" y="106" width="8" height="8" rx="2" fill="{color}"/>'
                        f'<text x="{x - 14}" y="114" font-size="11" fill="{INK2}">{short}</text>')
    for ri, (cid, cname, mx) in enumerate(CRITERIA):
        y = top + ri * ch
        body.append(f'<text x="28" y="{y + 20}" font-size="13" fill="{INK}">{escape(cname)}</text>'
                    f'<text x="{left - 12}" y="{y + 20}" font-size="11" fill="{MUTED}" text-anchor="end">{cid} · {mx}</text>')
        for ci, (task, sys_id) in enumerate(cols):
            vals = [r["points"].get(cid) or 0 for r in rows if r["task"] == task and r["system"] == sys_id and r.get("points")]
            if not vals:
                continue
            p = statistics.mean(vals) / mx
            fill, txt = ramp(p)
            body.append(f'<rect x="{left + ci * cw + 1}" y="{y + 1}" width="{cw - 2}" height="{ch - 2}" rx="4" fill="{fill}"/>'
                        f'<text x="{left + ci * cw + cw / 2}" y="{y + 20}" font-size="12" fill="{txt}" '
                        f'text-anchor="middle">{p * 100:.0f}%</text>')
    body.append(f'<text x="28" y="{h - 20}" font-size="12" fill="{MUTED}">Share of each criterion\'s points earned, '
                'averaged over the runs (MC = Mission Control, Solo+chk = single agent with our checks).</text>')
    return card(w, h, "".join(body), "Where the points come from", "Every criterion of the rubric, per task and system")


def time_chart(rows: list[dict]) -> str:
    w, left, top, bh, gap = 760, 190, 104, 18, 8
    groups = len(TASKS)
    h = top + groups * (len(SYSTEMS) * (bh + gap) + 34) + 40
    vals_all = [r.get("seconds") or 0 for r in rows]
    xmax = max(vals_all + [60]) / 60
    xmax = (int(xmax / 5) + 1) * 5
    sx = lambda m: left + m / xmax * (w - 60 - left)                                    # noqa: E731
    body = [legend(28, 90)]
    y = top
    for task, tname in TASKS:
        body.append(f'<text x="28" y="{y + 30}" font-size="14" font-weight="650" fill="{INK}">{escape(tname)}</text>')
        for sys_id, _, color in SYSTEMS:
            vals = [r["seconds"] / 60 for r in rows if r["task"] == task and r["system"] == sys_id and r.get("seconds")]
            if vals:
                m = statistics.mean(vals)
                x1 = sx(m)
                body.append(f'<path d="M{left},{y + 16} H{x1 - 4} Q{x1},{y + 16} {x1},{y + 20} V{y + 16 + bh - 4} '
                            f'Q{x1},{y + 16 + bh} {x1 - 4},{y + 16 + bh} H{left} Z" fill="{color}"/>'
                            f'<text x="{x1 + 8}" y="{y + 30}" font-size="12" fill="{INK}">{m:.1f} min</text>')
            y += bh + gap
        y += 34
    body.append(f'<line x1="{left}" x2="{left}" y1="{top + 8}" y2="{y - 30}" stroke="{MUTED}"/>')
    body.append(f'<text x="28" y="{h - 16}" font-size="12" fill="{MUTED}">Wall-clock minutes from the request to a finished '
                'project, mean of the runs (waiting for the user\'s approval not counted).</text>')
    return card(w, h, "".join(body), "Time to a finished project", "Mission Control runs its tasks one after another today")


def gallery(rows: list[dict], task: str) -> Path | None:
    """The desktop page of each system's median run, side by side."""
    from PIL import Image, ImageDraw, ImageFont
    shots = []
    for sys_id, name, color in SYSTEMS:
        runs = sorted((r for r in rows if r["task"] == task and r["system"] == sys_id and r.get("automatic") is not None),
                      key=lambda r: r["automatic"])
        if not runs:
            return None
        med = runs[len(runs) // 2]
        folder = BENCH / f"{task}-{sys_id}-{med['run']}"
        img = next((folder / f for f in ("1_landing_desktop.png", "2_board_after_add_desktop.png", "1_board_desktop.png")
                    if (folder / f).is_file()), None)
        if img is None:
            return None
        shots.append((name, color, med["automatic"], Image.open(img).convert("RGB")))
    tw, th, pad, head = 560, 620, 24, 56
    out = Image.new("RGB", (pad + len(shots) * (tw + pad), head + th + pad), SURFACE)
    draw = ImageDraw.Draw(out)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", 20)
    except OSError:
        font = ImageFont.load_default()
    for i, (name, color, pts, im) in enumerate(shots):
        x = pad + i * (tw + pad)
        crop = im.crop((0, 0, im.width, min(im.height, int(im.width * th / tw))))
        crop = crop.resize((tw, int(crop.height * tw / crop.width)))
        out.paste(crop, (x, head))
        draw.rounded_rectangle((x, 18, x + 14, 32), radius=3, fill=color)
        draw.text((x + 22, 12), f"{name} — {pts}/85", fill=INK, font=font)
    path = BENCH / f"gallery_{task}.png"
    out.save(path, optimize=True)
    return path


def summary(rows: list[dict]) -> str:
    lines = ["| task | system | runs | automatic /85 (mean, min–max) | all functional points | minutes | tokens out |",
             "|---|---|---|---|---|---|---|"]
    for task, tname in TASKS:
        for sys_id, name, _ in SYSTEMS:
            rs = [r for r in rows if r["task"] == task and r["system"] == sys_id and r.get("automatic") is not None]
            if not rs:
                continue
            a = [r["automatic"] for r in rs]
            full = sum(1 for r in rs if sum((r["points"].get(k) or 0) for k in ("F1", "F2", "F3", "F4", "F5", "F6", "F7")) == 70)
            mins = statistics.mean(r.get("seconds") or 0 for r in rs) / 60
            tok = statistics.mean(r.get("tokens_out") or 0 for r in rs)
            lines.append(f"| {tname} | {name} | {len(rs)} | **{statistics.mean(a):.1f}** ({min(a)}–{max(a)}) | "
                         f"{full}/{len(rs)} | {mins:.1f} | {tok / 1000:.0f}k |")
    return "\n".join(lines)


def main() -> None:
    rows = load()
    (BENCH / "score.svg").write_text(score_chart(rows), encoding="utf-8", newline="\n")
    (BENCH / "criteria.svg").write_text(criteria_chart(rows), encoding="utf-8", newline="\n")
    (BENCH / "time.svg").write_text(time_chart(rows), encoding="utf-8", newline="\n")
    for task, _ in TASKS:
        gallery(rows, task)
    text = summary(rows)
    (BENCH / "summary.md").write_text(text + "\n", encoding="utf-8", newline="\n")
    sys.stdout.reconfigure(encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
