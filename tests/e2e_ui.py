"""End-to-end UI checks in a real browser (Playwright + Chromium).

    pip install playwright && playwright install chromium
    python tests/e2e_ui.py            # screenshots go to ./e2e-shots
    python tests/e2e_ui.py --browser msedge   # use an installed Edge (or chrome) when Chromium cannot be downloaded

Not part of `unittest discover` on purpose: it needs a browser. It starts its own server, wired to a scripted
stand-in model (so the LIVE engine path runs without Ollama) and a stand-in folder dialog.
Covers: layout for 1-8 agents in both languages and several window sizes, live-run robustness (plan stays
visible, ticks drawn, clicks survive streaming, nothing slides sideways), chat, pause, stop, reload, a full
live engine run, language switching, text direction of mixed Persian/English text, folder picking."""
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
from mission_control import utf8_console  # noqa: E402
from mission_control.server import make_server  # noqa: E402
from tests.helpers import APP_PY, FINISH, T, T1, T2, OllamaStandIn, call, happy_workers  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

utf8_console()

HOLDER = {"llm": OllamaStandIn(installed=("ornith-1.5:9b",), on_worker=lambda tid, msgs: time.sleep(0.25))}
PICKER = {"answer": {"status": "error", "message": "tkinter is not available"}}
_httpd = make_server("127.0.0.1", 0, llm_factory=lambda model, host: HOLDER["llm"], pick_folder=lambda initial, title: PICKER["answer"])
threading.Thread(target=_httpd.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{_httpd.server_address[1]}/"
TMP = tempfile.mkdtemp(prefix="mc-e2e-")
SHOTS = Path("e2e-shots")
SHOTS.mkdir(exist_ok=True)
problems = []


def check(cond, msg):
    if not cond:
        problems.append(msg)
        if os.environ.get("GITHUB_ACTIONS"):   # an annotation: readable on the run page without signing in
            print("::error title=e2e::" + msg.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A"))
    print(("PASS " if cond else "FAIL ") + msg)


def open_page(browser, lang="en", size=(1366, 768)):
    ctx = browser.new_context(viewport={"width": size[0], "height": size[1]})
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(BASE)
    pg.evaluate(f"localStorage.setItem('mc.lang', '{lang}')")
    pg.reload()
    return ctx, pg, errs


def reset_server():
    s = _httpd.app.session
    if s:
        s.stop()
    _httpd.app.session = None


SCROLLS = "['.workspace','.stage','.main','#room','body','html'].map(s => document.querySelector(s).scrollLeft)"


def approve(pg, timeout=20000):
    """The go-ahead after planning (on by default): the Start button must be fully visible, then click it."""
    pg.wait_for_selector("#gate:not([hidden]) #gateStart", timeout=timeout)
    visible = pg.evaluate("(() => { const r = document.querySelector('#gateStart').getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight; })()")
    check(visible, "the approval panel's Start button is fully visible")
    pg.click("#gateStart")
    pg.wait_for_selector("#gate", state="hidden", timeout=timeout)
INTER = lambda a, c: a["l"] < c["r"] - 1 and c["l"] < a["r"] - 1 and a["t"] < c["b"] - 1 and c["t"] < a["b"] - 1

CHANNEL = sys.argv[sys.argv.index("--browser") + 1] if "--browser" in sys.argv[:-1] else None

with sync_playwright() as p:
    b = p.chromium.launch(channel=CHANNEL) if CHANNEL else p.chromium.launch()
    print(f"browser: {CHANNEL or 'bundled chromium'} {b.version}")

    # ───────────── A: layout for 1..8 agents, both languages, several window sizes
    for lang, sizes in (("en", [(1366, 768), (1280, 720), (1920, 1080), (1100, 640)]), ("fa", [(1366, 768), (1100, 640)])):
        for (W, H) in sizes:
            reset_server()
            ctx, pg, errs = open_page(b, lang, (W, H))
            pg.wait_for_selector("#start:not([hidden])")
            pg.evaluate("document.querySelector('#start').hidden=true; document.querySelector('#room').hidden=false")
            for n in range(1, 9):
                r = pg.evaluate("""(n) => {
                  const {state, render} = window.__mc, MC = window.MC;
                  Object.assign(state, MC.createState());
                  document.querySelectorAll('#cards .slot').forEach(s => s.remove());
                  window.__mc.ui.cardEls.clear(); window.__mc.ui.layoutSig='';
                  let seq = 0; const ts = '2026-01-01T00:00:00Z';
                  const ev = (type, data, extra={}) => MC.applyEvent(state, {seq: ++seq, ts, run_id:'r', type, data, ...extra});
                  ev('run.started', {project_path:'/p', prompt:'x', mock:true});
                  const tasks = Array.from({length:n}, (_, i) => ({id:'t'+i, title:'A fairly long task title number '+i+' for wrapping', role:'role'+i, goal:'g', depends_on:[], owns:[], acceptance:['a']}));
                  ev('plan.created', {version:1, tasks});
                  tasks.forEach((t, i) => ev('agent.created', {role:t.role, model:'ornith-1.5:9b', title:'Agent '+i, task_ids:[t.id]}, {agent_id:'a'+i}));
                  render();
                  // measure the FINAL layout. On a cold browser's first page the render itself can come 300-700 ms late,
                  // and cards and the lead box then glide into place (transitions), so no fixed delay is safe: wait until
                  // this layout is applied, then until every finite animation has ended (infinite ones, e.g. blink, never do)
                  const sig = tasks.map((_, i) => 'a' + i).join(',') + '|';
                  const applied = () => new Promise((res, rej) => { const t0 = performance.now(); (function poll() {
                    if (window.__mc.ui.layoutSig.startsWith(sig) && document.querySelectorAll('#cards .card').length === n) return res();
                    if (performance.now() - t0 > 5000) return rej(new Error('layout never applied: ' + window.__mc.ui.layoutSig));
                    requestAnimationFrame(poll); })(); });
                  const settled = () => Promise.all(document.getAnimations()
                    .filter(a => a.effect && a.effect.getTiming().iterations !== Infinity).map(a => a.finished.catch(() => null)));
                  return Promise.all([document.fonts.ready, applied()]).then(() => new Promise(r => setTimeout(r, 50))).then(settled).then(() => {
                    const R = e => { const r = e.getBoundingClientRect(); return {l:r.left, t:r.top, r:r.right, b:r.bottom}; };
                    const st = document.querySelector('#stage');
                    return {stage: R(st), orch: R(document.querySelector('#orch')), center: R(document.querySelector('#center')),
                            flow: st.classList.contains('flow'), scrollW: st.scrollWidth, clientW: st.clientWidth,
                            cards: [...document.querySelectorAll('#cards .card')].map(R)};
                  });
                }""", n)
                boxes = r["cards"] + [r["orch"], r["center"]]
                clash = [(i, j) for i in range(len(boxes)) for j in range(i + 1, len(boxes)) if INTER(boxes[i], boxes[j])]
                if r["flow"]:
                    check(not clash and len(r["cards"]) == n and r["scrollW"] <= r["clientW"] + 1, f"[{lang}] {W}x{H} n={n}: flow layout, no overlap, no sideways scroll {clash}")
                    continue
                S = r["stage"]
                outside = [i for i, x in enumerate(boxes) if x["l"] < S["l"] - 1 or x["t"] < S["t"] - 1 or x["r"] > S["r"] + 1 or x["b"] > S["b"] + 1]
                check(not clash and not outside and len(r["cards"]) == n, f"[{lang}] {W}x{H} n={n}: no overlaps, all inside the stage {clash} {outside}"
                      + (f" stage={S} boxes={[boxes[i] for i in outside]}" if outside else ""))
                if n == 8 and (W, H) == (1366, 768):
                    pg.screenshot(path=str(SHOTS / f"10_eight_agents_{lang}.png"))
            check(not errs, f"[{lang}] {W}x{H}: no page errors {errs}")
            ctx.close()

    # ───────────── B: demo run at 1x in English
    reset_server()
    ctx, pg, errs = open_page(b, "en")
    pg.wait_for_selector("#start:not([hidden])")
    check(pg.evaluate("document.documentElement.dir") == "ltr" and pg.evaluate("document.documentElement.lang") == "en", "English is the default, laid out left to right")
    start_box = pg.evaluate("(() => { const r = document.querySelector('.titleblock').getBoundingClientRect(); return r.bottom; })()")
    check(start_box <= 768, f"the whole start sheet fits in a 768px-high window (bottom at {start_box:.0f}px)")
    pg.screenshot(path=str(SHOTS / "00_start_en.png"))

    # pasting a Windows "Copy as path" value: quotes and trailing slash are removed
    pg.fill("#projectPath", '"' + TMP + '/demo/"')
    pg.dispatch_event("#projectPath", "change")
    check(pg.input_value("#projectPath") == TMP + "/demo", f"a quoted pasted path is tidied ({pg.input_value('#projectPath')!r})")

    # the native dialog is unavailable here -> the built-in browser opens instead
    pg.click("#pickFolder")
    pg.wait_for_selector(".modal .dialog", timeout=5000)
    check(pg.is_visible(".toast.warn"), "when the system folder dialog is unavailable the user is told and the built-in browser opens")
    pg.keyboard.press("Escape")
    PICKER["answer"] = {"status": "ok", "path": TMP + "/picked"}
    pg.click("#pickFolder")
    pg.wait_for_function(f"document.querySelector('#projectPath').value === {json.dumps(TMP + '/picked')}", timeout=5000)
    check(True, "the path chosen in the system folder dialog lands in the field")

    pg.fill("#projectPath", TMP + "/demo")
    pg.fill("#prompt", "Build a cafe website")
    pg.click("#modeSeg button[data-mode='demo']")
    check(pg.is_visible("#demoOpts") and not pg.is_visible("#liveOpts"), "demo mode shows the speed control and hides the model options")
    pg.click("#speedSeg button[data-speed='1']")
    pg.click("#startBtn")
    pg.wait_for_selector("#room:not([hidden])")
    approve(pg)
    pg.wait_for_function("window.__mc.state.files.size >= 3", timeout=40000)
    rows = pg.evaluate("[...document.querySelectorAll('.prow')].map(r => +getComputedStyle(r).opacity)")
    check(rows and min(rows) > 0.95, f"plan rows fully opaque while files stream (min={min(rows) if rows else None})")
    pg.wait_for_function("[...window.__mc.state.tasks.values()].some(t => t.status==='verified')", timeout=40000)
    vis = pg.evaluate("""[...document.querySelectorAll('.tick[data-s=verified]')].map(t => {
        const cs = el => getComputedStyle(el);
        return [t.querySelector('.p1'), t.querySelector('.p2')].map(p => cs(p).display !== 'none' && parseFloat(cs(p).strokeDashoffset) === 0);
    })""")
    check(vis and all(all(v) for v in vis), f"verified ticks show both check strokes, fully drawn ({len(vis)} ticks)")
    struck = pg.evaluate("[...document.querySelectorAll('.prow:not(.removed) .ptitle')].filter(e => getComputedStyle(e).textDecorationLine !== 'none').length")
    check(struck == 0, f"no plan title is struck through unless removed ({struck})")
    clicked = 0
    for _ in range(4):
        pg.evaluate("window.__mc.ui.follow = true")
        pg.locator("#tree .trow.file").first.click(timeout=8000)
        if pg.evaluate("window.__mc.ui.follow === false && window.__mc.state.files.has(window.__mc.ui.selectedFile)"):
            clicked += 1
        time.sleep(0.5)
    check(clicked >= 3, f"tree clicks register during live streaming ({clicked}/4)")
    check(pg.evaluate(SCROLLS) == [0] * 6, "clicking does not slide the room sideways")
    pg.mouse.move(600, 400); pg.mouse.wheel(500, 0); pg.keyboard.press("End"); pg.wait_for_timeout(200)
    check(pg.evaluate(SCROLLS) == [0] * 6, "wheel / End cannot scroll the room sideways")

    pg.click("#cards .card[data-id='a_frontend']")
    pg.wait_for_selector("#drawer.open")
    pg.wait_for_timeout(450)                              # let the 0.3 s slide-in finish before measuring
    box = pg.evaluate("(() => { const r = document.querySelector('#drawer').getBoundingClientRect(); return [r.left, r.right, r.width]; })()")
    check(abs(box[1] - 1366) <= 2 and abs(box[0] - (1366 - box[2])) <= 2, f"in English the drawer sits exactly on the right edge {box}")
    pg.fill("#drawer textarea", "Please add a dark mode"); pg.keyboard.press("Enter")
    pg.wait_for_function("[...document.querySelectorAll('#dTx .msg:not(.user) .txt')].some(m => m.innerText.includes('(demo)'))", timeout=8000)
    check(True, "chat from the drawer: the message appears and the demo reply arrives")
    pg.screenshot(path=str(SHOTS / "12_drawer_en.png"))
    pg.keyboard.press("Escape")
    pg.wait_for_function("!document.querySelector('#drawer').classList.contains('open')")
    check(pg.evaluate(SCROLLS) == [0] * 6, "opening/closing the drawer leaves the room in place")
    pg.click("#btnPauseAll")
    pg.wait_for_selector(".toast.warn", timeout=5000)
    check("Demo mode" in pg.inner_text(".toast.warn >> nth=-1"), "pause in demo mode says honestly that it has no effect")
    pg.click("#btnStop")
    check("Sure" in pg.inner_text("#btnStop"), "End run asks for confirmation first")
    pg.click("#btnStop")
    pg.wait_for_function("document.querySelector('#runChip').dataset.state==='stopped'", timeout=8000)
    halted = pg.evaluate("[...document.querySelectorAll('.card')].filter(c => c.dataset.s==='halted').length")
    check(halted > 0, f"a stopped run greys out unfinished agents ({halted})")
    pg.wait_for_timeout(300)
    stop_msgs = [t for t in pg.locator(".toast").all_inner_texts() if "stop" in t.lower() or "ended" in t.lower()]
    check(len(stop_msgs) == 1, f"stopping produces exactly one notice {stop_msgs}")
    pg.reload(); pg.wait_for_selector("#room:not([hidden])")
    pg.wait_for_function("window.__mc.state.run.status==='stopped'", timeout=8000)
    check(pg.evaluate("window.__mc.state.agents.size") >= 3, "reload rebuilds the room from the event log")
    check(not errs, f"no page errors in the demo run {errs}")
    pg.click("#btnNew"); pg.wait_for_selector("#start:not([hidden])")
    check(pg.locator(".toast").count() == 0, "notices about the previous run are cleared for the next one")

    # ───────────── C: a LIVE run of the real engine (stand-in model) in English
    pg.fill("#projectPath", TMP + "/live"); pg.fill("#prompt", "Build a cafe website")
    pg.click("#modeSeg button[data-mode='live']")
    pg.wait_for_function("document.querySelector('#modelStatus').dataset.state === 'ok'", timeout=5000)
    check("running" in pg.inner_text("#modelStatus"), "live mode reports that Ollama is running and the model is installed")
    pg.fill("#modelInput", "missing-model")
    pg.dispatch_event("#modelInput", "input")
    check(pg.get_attribute("#modelStatus", "data-state") == "bad" and "ollama pull missing-model" in pg.inner_text("#modelStatus"), "a model that is not installed is flagged with the exact fix")
    pg.click("#startBtn")
    pg.wait_for_selector("#startError:not([hidden])")
    check("ollama pull missing-model" in pg.inner_text("#startError"), "starting with a missing model shows a coded, translated error")
    pg.fill("#modelInput", "ornith-1.5:9b"); pg.dispatch_event("#modelInput", "input")
    pg.click("#startBtn")
    pg.wait_for_selector("#room:not([hidden])")
    approve(pg)
    pg.wait_for_function("document.querySelector('#runChip').dataset.state === 'running'", timeout=10000)
    check("ornith-1.5:9b" in pg.inner_text("#runChip"), "a live run shows the model name instead of the Demo badge")
    pg.wait_for_function("[...window.__mc.state.agents.values()].some(a => a.status === 'running')", timeout=20000)
    pg.screenshot(path=str(SHOTS / "20_live_running.png"))
    pg.click("#btnPauseAll")
    pg.wait_for_function("document.querySelector('#runChip').dataset.state === 'paused'", timeout=10000)
    check(True, "live pause really pauses: the chip reaches Paused (via Pausing)")
    paused_seq = pg.evaluate("window.__mc.state.lastSeq")
    pg.wait_for_timeout(800)
    check(pg.evaluate("window.__mc.state.lastSeq") == paused_seq, "while paused, nothing happens")
    pg.click("#btnPauseAll")
    pg.wait_for_function("document.querySelector('#runChip').dataset.state === 'completed'", timeout=60000)
    check(Path(TMP, "live", "backend", "app.py").is_file(), "the live run wrote real files to the project folder")
    verified = pg.evaluate("[...document.querySelectorAll('.card')].filter(c => c.dataset.s==='verified').length")
    check(verified == 3, f"all three live agents end verified ({verified})")
    pg.screenshot(path=str(SHOTS / "21_live_done.png"))
    check(not errs, f"no page errors in the live run {errs}")
    ctx.close()

    # ───────────── E: the lead agent changes the plan mid-run, and the room follows
    reset_server()
    w = happy_workers()
    w["t4"] = [call("write_file", path="docs/USAGE.md", content="# Usage\n"), FINISH]
    t4 = T("t4", "Usage guide", "docs", ["t2"], ["docs/**"])
    HOLDER["llm"] = OllamaStandIn(installed=("ornith-1.5:9b",), workers=w, on_worker=lambda tid, msgs: time.sleep(0.3),
                                  reviews=[{"action": "update", "reason": "The frontend is not needed; add a usage guide instead",
                                            "decisions": ["Keep the API small"], "tasks": [T1, T2, t4]},
                                           {"action": "keep", "reason": "ok", "tasks": []}])
    ctx, pg, errs = open_page(b, "en")
    pg.wait_for_selector("#start:not([hidden])")
    pg.fill("#projectPath", TMP + "/replan"); pg.fill("#prompt", "Build a cafe website")
    pg.click("#modeSeg button[data-mode='live']")
    pg.wait_for_function("document.querySelector('#modelStatus').dataset.state === 'ok'", timeout=5000)
    pg.click("#startBtn")
    pg.wait_for_selector("#room:not([hidden])")
    approve(pg)
    pg.wait_for_function("window.__mc.state.plan.version === 2", timeout=30000)
    pg.wait_for_timeout(700)                               # the removed card fades out, the others glide into place
    cards = pg.evaluate("[...document.querySelectorAll('#cards .card')].map(c => c.dataset.id)")
    check("a_t3" not in cards and "a_t4" in cards, f"the removed agent's card disappears and the new agent's card appears {cards}")
    struck = pg.evaluate("(() => { const r = document.querySelector('.prow[data-id=t3]'); return r && r.classList.contains('removed') && getComputedStyle(r.querySelector('.ptitle')).textDecorationLine; })()")
    check(struck == "line-through", f"the removed task stays visible in the plan, struck through ({struck})")
    label = pg.inner_text(".prow[data-id=t3] .pstat")
    check(label == "removed from the plan", f"the removed task says so, instead of 'waiting' ({label!r})")
    check("frontend is not needed" in pg.inner_text(".plan-reason"), "the reason for the change is shown above the plan")
    check("Keep the API small" in pg.inner_text(".decisions"), "the lead's new decision is listed")
    pg.screenshot(path=str(SHOTS / "22_live_replan.png"))
    pg.wait_for_function("document.querySelector('#runChip').dataset.state === 'completed'", timeout=60000)
    boxes = pg.evaluate("[...document.querySelectorAll('#cards .card, #orch, #center')].map(e => { const r = e.getBoundingClientRect(); return {l:r.left, t:r.top, r:r.right, b:r.bottom}; })")
    clash = [(i, j) for i in range(len(boxes)) for j in range(i + 1, len(boxes)) if INTER(boxes[i], boxes[j])]
    check(not clash, f"after the re-layout nothing overlaps {clash}")
    check(Path(TMP, "replan", "docs", "USAGE.md").is_file() and not Path(TMP, "replan", "frontend").exists(),
          "the new task's file exists on disk and the removed task never ran")
    check(not errs, f"no page errors during the replan {errs}")
    pg.screenshot(path=str(SHOTS / "23_live_replan_done.png"))
    ctx.close()

    # ───────────── F: a task fails, the user decides, the logs explain it, "Retry failed tasks" finishes the job
    reset_server()
    bad = call("write_file", path="backend/app.py", content="def broken(:\n")
    wf = happy_workers()
    wf["t2"] = [bad, FINISH] * 3
    HOLDER["llm"] = OllamaStandIn(installed=("ornith-1.5:9b", "ornith-1.5:35b"), sizes={"ornith-1.5:9b": "9.2B", "ornith-1.5:35b": "35B"},
                                  workers=wf, on_worker=lambda tid, msgs: time.sleep(0.1),
                                  diagnoses=[{"diagnosis": "unclosed parameter list", "user_summary": "The backend keeps a syntax error.",
                                              "suggestions": ["Retry with a note", "Use the 35B model"], "action": "ask_user",
                                              "guidance": "", "tasks": []}])
    ctx, pg, errs = open_page(b, "en")
    pg.wait_for_selector("#start:not([hidden])")
    pg.fill("#projectPath", TMP + "/fail"); pg.fill("#prompt", "Build a cafe website")
    pg.click("#modeSeg button[data-mode='live']")
    pg.wait_for_function("document.querySelector('#modelStatus').dataset.state === 'ok'", timeout=5000)
    pg.click("#startBtn"); pg.wait_for_selector("#room:not([hidden])")
    pg.wait_for_selector("#gate:not([hidden]) .approval")
    pg.select_option("#gate tbody tr:nth-child(2) select", "ornith-1.5:35b")
    approve(pg)
    check(pg.evaluate("window.__mc.state.agents.get('a_t2').model") == "ornith-1.5:35b", "the model picked during approval is used by that agent")
    pg.wait_for_selector("#gate:not([hidden]) .attention", timeout=30000)
    check(pg.get_attribute("#runChip", "data-state") == "attention", "the run says it needs a decision")
    check("syntax error" in pg.inner_text(".gate-card .reasons"), "the decision panel shows the real reason")
    check("Use the 35B model" in pg.inner_text(".gate-card.attention"), "the decision panel shows the lead's suggestions")
    btns = pg.evaluate("[...document.querySelectorAll('.gate-card.attention footer button')].every(b => { const r = b.getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight; })")
    check(btns, "Retry / Skip / End are fully visible")
    check(pg.locator(".toast.err").count() == 0, "failed attempts do not pile up as error toasts")
    check(pg.inner_text("#logBadge") == "2", f"the Logs button counts the errors ({pg.inner_text('#logBadge')!r})")
    pg.click("#btnLogs"); pg.wait_for_selector("#drawer.open .loglist")
    pg.click(".logs-controls .seg button:nth-child(3)")
    rows = pg.locator(".logrow").count()
    check(rows == 2 and pg.locator(".logrow.lv-error").count() == 2, f"the Errors filter shows exactly the errors ({rows})")
    pg.locator(".logrow details").first.click()
    check("syntax error" in pg.inner_text(".logrow pre >> nth=0"), "each error carries its evidence")
    report = pg.evaluate("fetch('/api/report').then(r => r.text())")
    check("## Errors and warnings" in report and "a_t2" in report and "ornith-1.5:35b" in report, "the downloadable report has the errors, agents and models")
    pg.screenshot(path=str(SHOTS / "40_logs.png"))
    pg.keyboard.press("Escape")
    pg.click(".gate-card.attention button:has-text('Skip this task')")
    pg.wait_for_function("document.querySelector('#runChip').dataset.state === 'failed'", timeout=20000)
    check(pg.is_visible("#btnRetry"), "a failed live run offers Retry failed tasks")
    HOLDER["llm"].workers["t2"] = [APP_PY, FINISH]
    pg.click("#btnRetry")
    pg.wait_for_function("document.querySelector('#runChip').dataset.state === 'completed'", timeout=30000)
    verified = pg.evaluate("[...document.querySelectorAll('.card')].filter(c => c.dataset.s === 'verified').length")
    check(verified == 3, f"after Retry failed tasks everything is verified ({verified})")
    check(pg.evaluate("window.__mc.state.run.resumed") is True, "the retry continued the previous run instead of starting over")
    check(not errs, f"no page errors in the failure flow {errs}")
    pg.screenshot(path=str(SHOTS / "41_retried.png"))
    ctx.close()

    # ───────────── D: language switch, Persian layout and text direction
    reset_server()
    ctx, pg, errs = open_page(b, "en")
    pg.wait_for_selector("#start:not([hidden])")
    pg.fill("#projectPath", TMP + "/fa"); pg.fill("#prompt", "keep me")
    pg.click(".sheet .langs button[data-lang='fa']")
    pg.wait_for_selector("#start:not([hidden])")
    check(pg.evaluate("document.documentElement.dir") == "rtl", "switching to Persian flips the layout to right-to-left")
    check(pg.input_value("#prompt") == "keep me" and pg.input_value("#projectPath") == TMP + "/fa", "what was typed survives the language switch")
    pg.screenshot(path=str(SHOTS / "30_start_fa.png"))
    pg.click("#modeSeg button[data-mode='demo']"); pg.click("#speedSeg button[data-speed='4']"); pg.click("#startBtn")
    pg.wait_for_selector("#room:not([hidden])")
    approve(pg)
    pg.wait_for_function("window.__mc.state.decisions.length >= 1", timeout=30000)
    d = pg.evaluate("(() => { const li = document.querySelector('.decisions li'); return [li.dir, li.innerText]; })()")
    check(d[0] == "rtl", f"a Persian sentence that starts with an English word is still laid out right-to-left {d}")
    pg.click("#cards .card >> nth=0"); pg.wait_for_selector("#drawer.open"); pg.wait_for_timeout(450)
    box = pg.evaluate("(() => { const r = document.querySelector('#drawer').getBoundingClientRect(); return [r.left, r.right, r.width]; })()")
    check(abs(box[0]) <= 2 and abs(box[1] - box[2]) <= 2, f"in Persian the drawer sits exactly on the left edge {box}")
    pg.keyboard.press("Escape")
    pg.wait_for_function("document.querySelector('#runChip').dataset.state==='completed'", timeout=60000)
    pg.screenshot(path=str(SHOTS / "31_room_fa_done.png"))
    # Which font really drew the text (asked from the browser engine itself). The code fonts have no Persian, and the
    # browser's fallback was a MONOSPACE Arabic font (Courier New) that splits words into separate letters.
    # The samples have no spaces: a space is not Persian, so it rightly keeps the code font and would blur the result.
    pg.evaluate("""(() => { const code = document.querySelector('.code');
      for (const [id, s] of [['probe-fa', 'سلامدنیا'], ['probe-en', 'const_x=1;']]) {
        const sp = document.createElement('span'); sp.id = id; sp.textContent = s; code.append(sp); } })()""")
    cdp = pg.context.new_cdp_session(pg)
    cdp.send("DOM.enable"); cdp.send("CSS.enable")
    doc = cdp.send("DOM.getDocument")["root"]["nodeId"]
    used = {k: [f["familyName"] for f in cdp.send("CSS.getPlatformFontsForNode", {
                "nodeId": cdp.send("DOM.querySelector", {"nodeId": doc, "selector": "#" + k})["nodeId"]})["fonts"]]
            for k in ("probe-fa", "probe-en")}
    pg.evaluate("document.querySelectorAll('#probe-fa, #probe-en').forEach(n => n.remove())")
    check(used["probe-fa"] and not any("courier" in f.lower() or "mono" in f.lower() for f in used["probe-fa"]),
          f"Persian in the file viewer uses a proportional font, so its letters join {used}")
    mono = lambda f: any(k in f.lower() for k in ("mono", "code", "consolas", "menlo", "courier"))
    check(used["probe-en"] and all(mono(f) for f in used["probe-en"]), f"code in the file viewer is still in a monospace font {used}")
    check(pg.evaluate(SCROLLS) == [0] * 6, "nothing slides sideways in right-to-left either")
    check(not errs, f"no page errors in Persian {errs}")
    ctx.close()
    b.close()

print("\nFAILED:" if problems else "\nALL CHECKS PASSED", json.dumps(problems, ensure_ascii=False, indent=1) if problems else "")
sys.exit(1 if problems else 0)
