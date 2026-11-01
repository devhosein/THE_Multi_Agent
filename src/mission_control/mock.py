"""Mock run generator.

Produces a realistic, fully valid event stream (written through the real EventLog, so it
is validated like production events) for building and polishing the UI without spending
a single token. The scenario deliberately exercises every UI state:

  * plan creation, agents appearing around the screen
  * a sequential step (architect) and two PARALLEL steps (backend + frontend, then seo + qa)
  * soft pause / resume while agents are mid-task
  * the user chatting with one agent during the pause -> decision recorded -> plan edited
    (new task + new agent + changed acceptance criteria)
  * files streaming in live, including a rewrite of an existing file
  * an agent error, followed by a retry
  * two-level ticks: done -> verified
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .eventlog import EventLog
from .events import ORCHESTRATOR_ID, Event, T
from .plan import Plan, Task, diff_plans, plan_stages, validate_plan

MODEL = "ornith-1.5:9b"

# All human-readable text of the demo lives here, in two languages with IDENTICAL keys
# (a test enforces that both languages produce the same event stream shape).
TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "prompt": "Build a cafe website: a product menu, a table-reservation form and a simple backend.",
        "api_md": '# API contract\n\nGET  /api/menu          -> [{"id": 1, "name": "Espresso", "price": 3.5}]\nPOST /api/reservations  <- {"name": str, "phone": str, "people": int}\n                        -> 201 {"id": 7, "status": "confirmed"}\n',
        "readme": "# Cafe site\n\nbackend/    API service (Flask)\nfrontend/   static page (HTML/CSS/JS)\ncontracts/  shared API contract\ntests/      API tests\n",
        "menu_item": "Espresso",
        "title_plain": "Cafe",
        "title_seo": "Our Cafe | Menu and table reservations",
        "meta_desc": "The cafe menu and online table reservations",
        "heading": "Our Cafe",
        "guest": "Sam",
        "usage_md": "# Usage\n\n1. `pip install -r backend/requirements.txt`\n2. `flask --app backend.app run`\n3. Open `frontend/index.html` in your browser.\n",
        "t1_title": "Design the API contract", "t1_goal": "Write the shared API contract and the folder structure.",
        "t1_a1": "contracts/api.md defines both endpoints", "t1_a2": "The project structure is described in README.md",
        "t2_title": "Build the backend", "t2_goal": "Implement the API service following the contract.",
        "t2_a1": "The endpoints behave as the contract says", "t2_a2": "The app starts without errors",
        "t3_title": "Build the frontend", "t3_goal": "Build the menu page and the reservation form.",
        "t3_a1": "The menu is fetched from /api/menu and shown", "t3_a2": "The layout is responsive", "t3_a3": "Supports dark mode",
        "t4_title": "Optimize SEO", "t4_goal": "Add meta tags, a sitemap and robots.txt.",
        "t4_a1": "index.html has a title and a meta description", "t4_a2": "sitemap.xml and robots.txt exist",
        "t5_title": "Test and verify", "t5_goal": "Write and run tests for both endpoints.",
        "t5_a1": "Both endpoints have tests", "t5_a2": "All tests pass",
        "t6_title": "Write the usage guide", "t6_goal": "Write instructions for running the project.",
        "t6_a1": "docs/USAGE.md explains the install and run steps",
        "r_architect": "Architect", "r_backend": "Backend", "r_frontend": "Frontend", "r_seo": "SEO", "r_qa": "QA", "r_docs": "Docs",
        "analyzing": "Analyzing the request and the input files…", "planning": "Building the execution plan…",
        "plan_ready": "The plan is ready: 5 tasks. The API contract is written first, then the backend and frontend are built in parallel.",
        "t1_start": "Task t1 (architect) started",
        "arch_intro": "I'm writing the API contract and defining the project structure.", "arch_outro": "The contract is ready; every other agent must follow it.",
        "d1": "The API has two endpoints: GET /api/menu and POST /api/reservations",
        "phase_b": "Tasks t2 and t3 are independent; they run in parallel",
        "be_intro": "Building the backend with Flask, following the contract.", "be_outro": "The backend is ready and matches the contract.",
        "fe_intro": "Building the menu page with plain HTML.", "fe_dark": "Following decision d2, I'm adding dark mode to style.css.",
        "fe_outro": "The frontend is ready, with dark mode.",
        "user_msg": "Please add a dark mode too, I'd like that.",
        "orch_reply": "Your message for the frontend agent is recorded. I'm updating the plan and adding a documentation task.",
        "d2": "The site must support dark mode", "plan_reason": "User request: dark mode + add a usage guide",
        "fe_reply": "Sure! I'll add dark mode with CSS variables.",
        "phase_c": "Tasks t4 and t5 are ready; they run in parallel",
        "seo_intro": "Adding meta tags, and creating the sitemap and robots.txt.", "seo_outro": "Basic SEO is done.",
        "qa_intro": "Writing tests for both endpoints and running them.",
        "qa_error": "test_reservation_validation failed: expected 422 but got 201",
        "qa_status": "The test agent hit an error; running it again",
        "qa_retry": "My expectation was wrong (the contract has no validation). I'm fixing the test.", "qa_outro": "All tests pass.",
        "docs_intro": "Writing the run instructions.", "docs_outro": "The guide is ready.",
        "final": "All tasks are done and verified. The project is ready.", "summary": "6 tasks completed and verified",
        "awaiting": "Plan ready. Review the team, change models if you like, then start the work.",
        "approved": "Approved. Starting the work.",
        "log_start": "Demo run: a recorded six-agent session (no model is called)",
        "log_qa": "t5 attempt 1/2 failed (checks): the test expected 422 but the API returned 201",
        "log_qa_detail": "Problems:\n- test_reservation_validation: expected 422, got 201\n\nAgent's last words:\nWriting tests for both endpoints and running them.",
        "log_plan": "Plan v2: +[t6] -[] ~[t3]",
    },
    "fa": {
        "prompt": "یه سایت معرفی کافه بساز: منوی محصولات، فرم رزرو میز و یه بک‌اند ساده.",
        "api_md": '# قرارداد API\n\nGET  /api/menu          -> [{"id": 1, "name": "اسپرسو", "price": 90000}]\nPOST /api/reservations  <- {"name": str, "phone": str, "people": int}\n                        -> 201 {"id": 7, "status": "confirmed"}\n',
        "readme": "# سایت کافه\n\nbackend/    سرویس API (Flask)\nfrontend/   صفحه‌ی ایستا (HTML/CSS/JS)\ncontracts/  قرارداد مشترک API\ntests/      تست‌های API\n",
        "menu_item": "اسپرسو",
        "title_plain": "کافه",
        "title_seo": "کافه‌ی ما | منو و رزرو میز",
        "meta_desc": "منوی کافه و رزرو آنلاین میز",
        "heading": "کافه‌ی ما",
        "guest": "علی",
        "usage_md": "# راهنما\n\n1. `pip install -r backend/requirements.txt`\n2. `flask --app backend.app run`\n3. صفحه‌ی `frontend/index.html` رو باز کن.\n",
        "t1_title": "طراحی قرارداد API", "t1_goal": "قرارداد مشترک API و ساختار پوشه‌ها را بنویس.",
        "t1_a1": "contracts/api.md هر دو endpoint را تعریف می‌کند", "t1_a2": "ساختار پروژه در README.md آمده است",
        "t2_title": "ساخت بک‌اند", "t2_goal": "سرویس API را مطابق قرارداد پیاده کن.",
        "t2_a1": "endpointها مطابق قرارداد کار می‌کنند", "t2_a2": "برنامه بدون خطا بالا می‌آید",
        "t3_title": "ساخت فرانت‌اند", "t3_goal": "صفحه‌ی منو و فرم رزرو را بساز.",
        "t3_a1": "منو از /api/menu خوانده و نمایش داده می‌شود", "t3_a2": "طرح ریسپانسیو است", "t3_a3": "از حالت تاریک پشتیبانی می‌کند",
        "t4_title": "بهینه‌سازی سئو", "t4_goal": "متاتگ‌ها، sitemap و robots را اضافه کن.",
        "t4_a1": "index.html عنوان و meta description دارد", "t4_a2": "sitemap.xml و robots.txt موجودند",
        "t5_title": "تست و بررسی", "t5_goal": "برای هر دو endpoint تست بنویس و اجرا کن.",
        "t5_a1": "هر دو endpoint تست دارند", "t5_a2": "همه‌ی تست‌ها پاس می‌شوند",
        "t6_title": "نوشتن راهنما", "t6_goal": "راهنمای اجرای پروژه را بنویس.",
        "t6_a1": "docs/USAGE.md مراحل نصب و اجرا را دارد",
        "r_architect": "معمار", "r_backend": "بک‌اند", "r_frontend": "فرانت‌اند", "r_seo": "سئو", "r_qa": "تست و کیفیت", "r_docs": "مستندسازی",
        "analyzing": "در حال تحلیل درخواست و فایل‌های ورودی…", "planning": "در حال ساخت پلن اجرایی…",
        "plan_ready": "پلن آماده است: ۵ تسک. اول قرارداد API نوشته می‌شود، بعد بک‌اند و فرانت‌اند هم‌زمان ساخته می‌شوند.",
        "t1_start": "تسک t1 (معمار) شروع شد",
        "arch_intro": "قرارداد API را می‌نویسم و ساختار پروژه را مشخص می‌کنم.", "arch_outro": "قرارداد آماده شد؛ بقیه‌ی ایجنت‌ها باید به آن پایبند باشند.",
        "d1": "API دو endpoint دارد: GET /api/menu و POST /api/reservations",
        "phase_b": "تسک‌های t2 و t3 مستقل‌اند؛ هم‌زمان اجرا می‌شوند",
        "be_intro": "بک‌اند را با Flask مطابق قرارداد می‌سازم.", "be_outro": "بک‌اند آماده است و با قرارداد هم‌خوان است.",
        "fe_intro": "صفحه‌ی منو را با HTML ساده و RTL می‌سازم.", "fe_dark": "طبق تصمیم d2، حالت تاریک را به style.css اضافه می‌کنم.",
        "fe_outro": "فرانت‌اند با حالت تاریک آماده شد.",
        "user_msg": "حالت تاریک هم داشته باشه، دوست دارم.",
        "orch_reply": "پیام شما برای ایجنت فرانت‌اند ثبت شد. پلن را به‌روز می‌کنم و یک تسک مستندسازی اضافه می‌شود.",
        "d2": "سایت باید حالت تاریک داشته باشد", "plan_reason": "درخواست کاربر: حالت تاریک + افزودن راهنما",
        "fe_reply": "باشه! حالت تاریک را با متغیرهای CSS اضافه می‌کنم.",
        "phase_c": "تسک‌های t4 و t5 آماده‌اند؛ هم‌زمان اجرا می‌شوند",
        "seo_intro": "متاتگ‌ها را اضافه و sitemap و robots را می‌سازم.", "seo_outro": "سئوی پایه انجام شد.",
        "qa_intro": "برای هر دو endpoint تست می‌نویسم و اجرا می‌کنم.",
        "qa_error": "test_reservation_validation شکست خورد: انتظار 422 بود ولی 201 برگشت",
        "qa_status": "ایجنت تست با خطا مواجه شد؛ دوباره اجرا می‌شود",
        "qa_retry": "انتظار تست اشتباه بود (قرارداد اعتبارسنجی ندارد). تست را اصلاح می‌کنم.", "qa_outro": "همه‌ی تست‌ها پاس شدند.",
        "docs_intro": "راهنمای اجرا را می‌نویسم.", "docs_outro": "راهنما آماده شد.",
        "final": "همه‌ی تسک‌ها انجام و تأیید شدند. پروژه آماده است.", "summary": "۶ تسک انجام و تأیید شد",
        "awaiting": "پلن آماده است. تیم را بررسی کن، اگر خواستی مدل‌ها را عوض کن و بعد کار را شروع کن.",
        "approved": "تأیید شد. کار شروع می‌شود.",
        "log_start": "اجرای نمونه: یک جلسه‌ی ضبط‌شده‌ی شش‌ایجنتی (هیچ مدلی فراخوانی نمی‌شود)",
        "log_qa": "تلاش ۱ از ۲ برای t5 ناموفق بود (بررسی‌ها): تست انتظار 422 داشت ولی API پاسخ 201 داد",
        "log_qa_detail": "مشکلات:\n- test_reservation_validation: انتظار 422، دریافت 201\n\nآخرین پیام ایجنت:\nبرای هر دو endpoint تست می‌نویسم و اجرا می‌کنم.",
        "log_plan": "پلن نسخه‌ی ۲: +[t6] -[] ~[t3]",
    },
}

# ------------------------------------------------------------------ file contents (language-aware)
APP_PY = '''from flask import Flask, jsonify, request

app = Flask(__name__)
MENU = [{"id": 1, "name": "@ITEM@", "price": 90000}]
RESERVATIONS = []


@app.get("/api/menu")
def menu():
    return jsonify(MENU)


@app.post("/api/reservations")
def reserve():
    body = request.get_json(force=True)
    RESERVATIONS.append(body)
    return jsonify({"id": len(RESERVATIONS), "status": "confirmed"}), 201
'''
REQUIREMENTS = "flask>=3.0\n"
STYLE_CSS = "body { font-family: sans-serif; margin: 2rem; }\nh1 { color: #6b3f1d; }\n"
STYLE_CSS_DARK = """:root { --bg: #fff; --fg: #222; --brand: #6b3f1d; }
@media (prefers-color-scheme: dark) { :root { --bg: #161412; --fg: #eee; --brand: #d9a066; } }
body { background: var(--bg); color: var(--fg); font-family: sans-serif; margin: 2rem; }
h1 { color: var(--brand); }
"""
APP_JS = """fetch("/api/menu").then(r => r.json()).then(items => {
  document.querySelector("#menu").innerHTML =
    items.map(i => `<li>${i.name} - ${i.price}</li>`).join("");
});
"""
SITEMAP = '<?xml version="1.0"?>\n<urlset><url><loc>https://example.com/</loc></url></urlset>\n'
ROBOTS = "User-agent: *\nAllow: /\nSitemap: https://example.com/sitemap.xml\n"
TEST_API = """def test_reservation_validation(client):
    r = client.post("/api/reservations", json={})
    assert r.status_code == 422
"""
TEST_API_FIXED = """def test_menu(client):
    assert client.get("/api/menu").status_code == 200


def test_reservation(client):
    r = client.post("/api/reservations", json={"name": "@GUEST@", "phone": "0912", "people": 2})
    assert r.status_code == 201
"""


def index_html(x: dict[str, str], *, seo: bool) -> str:
    rtl = x is TEXTS["fa"]
    title = x["title_seo"] if seo else x["title_plain"]
    meta = f'\n<meta name="description" content="{x["meta_desc"]}">' if seo else ""
    return (f'<!doctype html>\n<html lang="{"fa" if rtl else "en"}" dir="{"rtl" if rtl else "ltr"}">\n'
            f'<head><meta charset="utf-8"><title>{title}</title>{meta}\n<link rel="stylesheet" href="style.css"></head>\n'
            f'<body>\n  <h1>{x["heading"]}</h1>\n  <ul id="menu"></ul>\n  <script src="app.js"></script>\n</body>\n</html>\n')


# ---------------------------------------------------------------------- scripting

class _Script:
    """Collects (time, event) entries from independent 'actors', then writes them in time order."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self._n = 0

    def at(self, t: float, type_: str, data: dict[str, Any] | None = None, *,
           agent: str | None = None, task: str | None = None,
           usage: tuple[int, int] | None = None) -> None:
        self.items.append({"t": t, "n": self._n, "type": type_, "data": data or {},
                           "agent": agent, "task": task, "usage": usage})
        self._n += 1


class _Actor:
    """One agent working on one task, with a local clock. Supports a *soft* pause window:
    a step that has already started finishes; the next step waits until the window ends."""

    def __init__(self, s: _Script, agent: str, task: str, t: float,
                 pause: tuple[float, float] | None = None, stagger: float = 0.0) -> None:
        self.s, self.agent, self.task, self.t = s, agent, task, t
        self.pause, self.stagger = pause, stagger
        self._paused_once = False
        self.pause_hit_t: float | None = None  # when this actor really stopped
        self._chars = 0
        self._files = 0

    def _gate(self) -> None:
        if self.pause and not self._paused_once and self.pause[0] <= self.t < self.pause[1]:
            self._paused_once = True
            self.pause_hit_t = self.t
            self.s.at(self.t, T.AGENT_STATUS, {"status": "paused"}, agent=self.agent)
            self.t = self.pause[1] + self.stagger
            self.s.at(self.t, T.AGENT_STATUS, {"status": "running"}, agent=self.agent)
            self.t += 0.2

    def begin(self, intro: str) -> None:
        self.s.at(self.t, T.TASK_STATUS, {"status": "running"}, task=self.task)
        self.s.at(self.t, T.AGENT_STATUS, {"status": "running"}, agent=self.agent)
        self.t += 0.4
        self.say(intro)

    def say(self, text: str) -> None:
        self.s.at(self.t, T.AGENT_MESSAGE, {"role": "agent", "text": text}, agent=self.agent)
        self.t += 0.3

    def write(self, path: str, content: str, *, rewrite: bool = False,
              per_chunk: int = 36, dt: float = 0.12) -> None:
        self._gate()
        data: dict[str, Any] = {"path": path}
        if rewrite:
            data["rewrite"] = True
        self.s.at(self.t, T.FILE_CREATED, data, agent=self.agent)
        self.t += 0.2
        for i in range(0, len(content), per_chunk):
            self.s.at(self.t, T.FILE_CHUNK, {"path": path, "text": content[i:i + per_chunk]}, agent=self.agent)
            self.t += dt
        self.s.at(self.t, T.FILE_SAVED, {"path": path, "size": len(content.encode("utf-8"))}, agent=self.agent)
        self.t += 0.2
        self._chars += len(content)
        self._files += 1

    def fail(self, message: str) -> None:
        self.s.at(self.t, T.ERROR, {"message": message}, agent=self.agent, task=self.task)
        self.s.at(self.t, T.TASK_STATUS, {"status": "failed"}, task=self.task)
        self.s.at(self.t, T.AGENT_STATUS, {"status": "error"}, agent=self.agent)

    def retry(self, text: str, after: float = 1.6) -> None:
        self.t += after
        self.s.at(self.t, T.TASK_STATUS, {"status": "running"}, task=self.task)
        self.s.at(self.t, T.AGENT_STATUS, {"status": "running"}, agent=self.agent)
        self.t += 0.4
        self.say(text)

    def end(self, outro: str, verify_delay: float = 1.2) -> float:
        """Finish the task; returns the time at which it is verified."""
        self._gate()
        self.say(outro)
        self.s.at(self.t, T.TASK_STATUS, {"status": "done"}, task=self.task)
        self.s.at(self.t, T.AGENT_STATUS, {"status": "done"}, agent=self.agent)
        self.s.at(self.t, T.USAGE, usage=(1200 + 150 * self._files, self._chars // 3 + 80))
        self.t += verify_delay
        self.s.at(self.t, T.TASK_STATUS, {"status": "verified"}, task=self.task)
        return self.t


# ------------------------------------------------------------------------- plans

def _plans(x: dict[str, str]) -> tuple[Plan, Plan]:
    t1 = Task("t1", x["t1_title"], "architect", x["t1_goal"], owns=("contracts/**", "README.md"),
              acceptance=(x["t1_a1"], x["t1_a2"]))
    t2 = Task("t2", x["t2_title"], "backend", x["t2_goal"], depends_on=("t1",), owns=("backend/**",),
              acceptance=(x["t2_a1"], x["t2_a2"]))
    t3 = Task("t3", x["t3_title"], "frontend", x["t3_goal"], depends_on=("t1",), owns=("frontend/**",),
              acceptance=(x["t3_a1"], x["t3_a2"]))
    t4 = Task("t4", x["t4_title"], "seo", x["t4_goal"], depends_on=("t3",),
              owns=("frontend/index.html", "frontend/sitemap.xml", "frontend/robots.txt"),
              acceptance=(x["t4_a1"], x["t4_a2"]))
    t5 = Task("t5", x["t5_title"], "qa", x["t5_goal"], depends_on=("t2", "t3"), owns=("tests/**",),
              acceptance=(x["t5_a1"], x["t5_a2"]))
    v1 = Plan(1, (t1, t2, t3, t4, t5))
    t3b = replace(t3, acceptance=t3.acceptance + (x["t3_a3"],))
    t6 = Task("t6", x["t6_title"], "docs", x["t6_goal"], depends_on=("t5",), owns=("docs/**",), acceptance=(x["t6_a1"],))
    v2 = Plan(2, (t1, t2, t3b, t4, t5, t6))
    for p in (v1, v2):
        problems = validate_plan(p)
        assert not problems, problems  # the mock must never contain an invalid plan
    return v1, v2


# ------------------------------------------------------------------------ builder

def build_mock_run(
    out_path: str | Path,
    *,
    lang: str = "en",
    run_id: str = "run_mock_001",
    project_path: str = "~/projects/cafe-site",
    start: datetime | None = None,
) -> list[Event]:
    x = TEXTS.get(lang, TEXTS["en"])
    start = start or datetime(2026, 10, 27, 9, 0, 0, tzinfo=timezone.utc)
    v1, v2 = _plans(x)
    s = _Script()
    api_md, readme, usage = x["api_md"], x["readme"], x["usage_md"]
    app_py = APP_PY.replace("@ITEM@", x["menu_item"])
    test_fixed = TEST_API_FIXED.replace("@GUEST@", x["guest"])

    def plan_event(t: float, type_: str, plan: Plan, **extra: Any) -> None:
        s.at(t, type_, {"version": plan.version, "tasks": [p.to_dict() for p in plan.tasks],
                        "stages": plan_stages(plan), **extra})

    def orch(t: float, text: str) -> None:
        s.at(t, T.AGENT_MESSAGE, {"role": "agent", "text": text}, agent=ORCHESTRATOR_ID)

    # --- planning
    s.at(0.0, T.RUN_STARTED, {"project_path": project_path, "prompt": x["prompt"], "inputs": ["inputs/menu.pdf"], "mock": True})
    s.at(0.1, T.LOG, {"level": "info", "source": "engine", "message": x["log_start"]})
    s.at(0.6, T.ORCH_STATUS, {"text": x["analyzing"]})
    s.at(2.0, T.ORCH_STATUS, {"text": x["planning"]})
    plan_event(3.0, T.PLAN_CREATED, v1)
    s.at(3.0, T.USAGE, usage=(1250, 640))
    roster = [("a_architect", "architect", "t1"), ("a_backend", "backend", "t2"), ("a_frontend", "frontend", "t3"),
              ("a_seo", "seo", "t4"), ("a_qa", "qa", "t5")]
    for i, (aid, role, tid) in enumerate(roster):
        s.at(3.3 + 0.3 * i, T.AGENT_CREATED, {"role": role, "model": MODEL, "title": x["r_" + role], "task_ids": [tid]}, agent=aid)
    orch(5.0, x["plan_ready"])
    agents_models = {aid: MODEL for aid, _r, _t in roster}
    s.at(5.1, T.APPROVAL_REQUESTED, {"agents": agents_models, "models": [MODEL], "sizes": {MODEL: "9.2B"}})
    s.at(5.15, T.ORCH_STATUS, {"text": x["awaiting"]})
    s.at(5.4, T.APPROVAL_GRANTED, {"models": agents_models})
    s.at(5.45, T.ORCH_STATUS, {"text": x["approved"]})

    # --- phase A: sequential (architect)
    s.at(5.6, T.ORCH_STATUS, {"text": x["t1_start"]})
    arch = _Actor(s, "a_architect", "t1", 6.0)
    arch.begin(x["arch_intro"])
    arch.write("contracts/api.md", api_md)
    arch.write("README.md", readme)
    end_a = arch.end(x["arch_outro"])
    s.at(end_a + 0.2, T.DECISION, {"id": "d1", "source": "orchestrator", "text": x["d1"]})

    # --- phase B: backend || frontend, with a soft pause and a user interruption
    tb = end_a + 1.0
    s.at(tb - 0.2, T.ORCH_STATUS, {"text": x["phase_b"]})
    pause_at, resume_at = tb + 1.6, tb + 6.6
    window = (pause_at, resume_at)

    be = _Actor(s, "a_backend", "t2", tb, pause=window, stagger=0.1)
    be.begin(x["be_intro"])
    be.write("backend/app.py", app_py)
    be.write("backend/requirements.txt", REQUIREMENTS)

    fe = _Actor(s, "a_frontend", "t3", tb, pause=window, stagger=0.3)
    fe.begin(x["fe_intro"])
    fe.write("frontend/index.html", index_html(x, seo=False))
    fe.write("frontend/style.css", STYLE_CSS)
    fe.write("frontend/app.js", APP_JS)
    fe.say(x["fe_dark"])
    fe.write("frontend/style.css", STYLE_CSS_DARK, rewrite=True)

    be_end = be.end(x["be_outro"])
    fe_end = fe.end(x["fe_outro"])

    # Soft pause: `user.command` is only the *request*; each agent finishes its current step and
    # then reports `paused`; `run.paused` is emitted only once EVERY running agent has stopped.
    hits = [a.pause_hit_t for a in (be, fe) if a.pause_hit_t is not None]
    fully_paused = max(hits)
    assert fully_paused + 3.2 < resume_at, "pause window too short for the scripted chat"
    s.at(pause_at, T.USER_COMMAND, {"command": "pause", "target": "all"})
    s.at(fully_paused + 0.05, T.RUN_PAUSED)

    # while paused: the user chats with the frontend agent, the orchestrator replans
    s.at(fully_paused + 1.0, T.AGENT_MESSAGE, {"role": "user", "text": x["user_msg"]}, agent="a_frontend")
    orch(fully_paused + 1.6, x["orch_reply"])
    s.at(fully_paused + 1.7, T.DECISION, {"id": "d2", "source": "user", "text": x["d2"]})
    plan_event(fully_paused + 2.2, T.PLAN_UPDATED, v2, reason=x["plan_reason"], **diff_plans(v1, v2))
    s.at(fully_paused + 2.25, T.LOG, {"level": "info", "source": "lead", "message": x["log_plan"], "detail": x["plan_reason"]})
    s.at(fully_paused + 2.5, T.AGENT_CREATED,
         {"role": "docs", "model": MODEL, "title": x["r_docs"], "task_ids": ["t6"]}, agent="a_docs")
    s.at(fully_paused + 2.9, T.AGENT_MESSAGE, {"role": "agent", "text": x["fe_reply"]}, agent="a_frontend")
    s.at(resume_at - 0.1, T.USER_COMMAND, {"command": "resume", "target": "all"})
    s.at(resume_at, T.RUN_RESUMED)

    # --- phase C: seo || qa
    tc = max(be_end, fe_end) + 1.0
    s.at(tc - 0.2, T.ORCH_STATUS, {"text": x["phase_c"]})
    seo = _Actor(s, "a_seo", "t4", tc)
    seo.begin(x["seo_intro"])
    seo.write("frontend/sitemap.xml", SITEMAP)
    seo.write("frontend/robots.txt", ROBOTS)
    seo.write("frontend/index.html", index_html(x, seo=True), rewrite=True)
    seo_end = seo.end(x["seo_outro"])

    qa = _Actor(s, "a_qa", "t5", tc)
    qa.begin(x["qa_intro"])
    qa.write("tests/test_api.py", TEST_API)
    s.at(qa.t, T.LOG, {"level": "error", "source": "task", "message": x["log_qa"], "detail": x["log_qa_detail"]},
         agent="a_qa", task="t5")
    qa.fail(x["qa_error"])
    s.at(qa.t + 0.2, T.ORCH_STATUS, {"text": x["qa_status"]})
    qa.retry(x["qa_retry"])
    qa.write("tests/test_api.py", test_fixed, rewrite=True)
    qa_end = qa.end(x["qa_outro"])

    # --- phase D: docs (starts as soon as t5 is verified, while seo may still be finishing)
    docs = _Actor(s, "a_docs", "t6", qa_end + 0.8)
    docs.begin(x["docs_intro"])
    docs.write("docs/USAGE.md", usage)
    docs_end = docs.end(x["docs_outro"])

    final = max(seo_end, docs_end) + 0.8
    orch(final, x["final"])
    s.at(final + 0.3, T.RUN_FINISHED, {"status": "completed", "summary": x["summary"]})

    # --- write everything in time order through the real, validating EventLog
    out = Path(out_path)
    if out.exists():
        out.unlink()
    log = EventLog(out, run_id, clock=lambda: start.isoformat(timespec="milliseconds"))
    tin = tout = 0
    for it in sorted(s.items, key=lambda i: (i["t"], i["n"])):
        data = it["data"]
        if it["usage"]:
            tin += it["usage"][0]
            tout += it["usage"][1]
            data = {"tokens_in": tin, "tokens_out": tout, "cost_usd": 0.0}
        ts = (start + timedelta(seconds=it["t"])).isoformat(timespec="milliseconds")
        log.append(it["type"], data, agent_id=it["agent"], task_id=it["task"], ts=ts)
    return log.read()
