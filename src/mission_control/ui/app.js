/* Mission Control UI. All text from the engine/model is inserted with textContent, never innerHTML:
   file contents and chat come from an LLM and must never be able to inject markup.
   All UI wording comes from i18n.js (t()); this file contains no human-language literals. */
(() => {
  'use strict';
  const { ORCH } = MC;
  const { t } = I18N;
  const state = MC.createState();
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  const CARD = { w: 196, h: 120 };
  const ORCHBOX = { w: 300, h: 84 };

  const ui = {
    live: false, sel: null, drawerId: null, follow: true, selectedFile: null, layout: null, layoutSig: '',
    fresh: new Set(), planFlash: null, lineEls: [], rendered: { path: null, len: 0 },
    cardEls: new Map(), rowEls: new Map(), txFor: null, txCount: 0, lastOrchText: '', stopArmed: false,
    gateModels: {}, gateBusy: false, logFilter: 'all', logDebug: false, logCount: 0,
  };
  const LOGS = '__logs';

  // ───────────────────────── helpers
  const $ = (s, r = document) => r.querySelector(s);
  const setText = (n, s) => { if (n.textContent !== s) n.textContent = s; };
  /** Direction for text written by people or models. Not dir="auto": that follows the FIRST strong letter, so a Persian
   *  sentence that starts with "API ..." would be laid out left-to-right. Any RTL letter makes the text RTL. */
  const RTL_CHARS = /[\u0590-\u08ff\ufb1d-\ufdff\ufe70-\ufeff]/;
  const textDir = (s) => (RTL_CHARS.test(s || '') ? 'rtl' : 'ltr');
  const setTextD = (n, s) => { setText(n, s); const d = textDir(s); if (n.dir !== d) n.dir = d; };
  /** Rebuild a container only when its *structure* signature changed. Rebuilding on every event would
   *  restart CSS animations and swallow clicks that straddle a re-render. */
  function patch(node, sig, build) {
    if (node.__sig === sig) return false;
    node.__sig = sig; node.replaceChildren(...build());
    return true;
  }
  function el(tag, props, ...kids) {
    const n = document.createElement(tag);
    if (props) for (const [k, v] of Object.entries(props)) {
      if (k === 'class') n.className = v;
      else if (k === 'dataset') Object.assign(n.dataset, v);
      else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
      else if (v === true) n.setAttribute(k, '');
      else if (v !== false && v != null) n.setAttribute(k, v);
    }
    for (const kid of kids.flat()) {
      if (kid == null || kid === false) continue;
      n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    }
    return n;
  }
  async function api(path, opts) {
    const r = await fetch(path, opts);
    let data = null;
    try { data = await r.json(); } catch (_) { /* non-JSON */ }
    if (!r.ok) {
      const e = new Error((data && data.error) || t('err.http', { status: r.status }));
      e.code = data && data.code; e.params = (data && data.params) || {}; e.status = r.status;
      throw e;
    }
    return data;
  }
  /** Server errors carry a code: show them in the user's language, fall back to the server's English text. */
  const errText = (e) => (e && e.code && I18N.has('err.' + e.code) ? t('err.' + e.code, e.params) : (e && e.message) || String(e));
  const postJSON = (path, body) => api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  function toast(msg, kind = '') {
    const n = el('div', { class: `toast ${kind}` }, msg);
    $('#toasts').append(n);
    setTimeout(() => { n.classList.add('out'); setTimeout(() => n.remove(), 320); }, kind === 'err' ? 7000 : 4500);
  }
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (_) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (_) { /* private mode */ } },
  };
  function autogrow(ta) { ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight + 2, 120) + 'px'; }
  function onEnterSend(ta, send) {
    ta.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
    });
    ta.addEventListener('input', () => autogrow(ta));
  }
  /** Same idea as the server's clean_path_input: drop surrounding quotes (Windows "Copy as path") and trailing slashes. */
  function tidyPath(raw) {
    let s = (raw || '').trim();
    const pairs = { '"': '"', "'": "'", '“': '”', '‘': '’', '«': '»' };
    while (s.length >= 2 && pairs[s[0]] && s[s.length - 1] === pairs[s[0]]) s = s.slice(1, -1).trim();
    if (s.length > 1 && !/^[A-Za-z]:[\\/]?$/.test(s)) s = s.replace(/[\\/]+$/, '') || s;
    return s;
  }

  // ───────────────────────── language
  function switchLang(next) {
    if (next === I18N.lang) return;
    // keep what the user typed on the start screen across the reload
    try {
      sessionStorage.setItem('mc.form', JSON.stringify({ path: $('#projectPath').value, prompt: $('#prompt').value }));
    } catch (_) { /* ignore */ }
    I18N.set(next);
    location.reload();   // the run itself lives on the server: the room is rebuilt from its event log
  }

  // ───────────────────────── screens
  function showStart() { $('#room').hidden = true; $('#start').hidden = false; closeDrawer(); }
  function showRoom() { $('#start').hidden = true; $('#room').hidden = false; scheduleRender(); }

  // ───────────────────────── start screen
  const start = { files: [], speed: 2, mode: null, models: null, strength: 'auto' };
  function renderChosenFiles() {
    const ul = $('#fileList');
    ul.replaceChildren(...start.files.map((f, i) => el('li', null,
      el('span', null, f.name), el('small', null, I18N.size(f.size)),
      el('button', { type: 'button', 'aria-label': t('start.files.remove', { name: f.name }), onclick: () => { start.files.splice(i, 1); renderChosenFiles(); } }, '×'))));
  }
  function addFiles(list) {
    for (const f of list) if (!start.files.some((x) => x.name === f.name && x.size === f.size)) start.files.push(f);
    renderChosenFiles();
  }
  function setMode(mode) {
    start.mode = mode;
    store.set('mc.mode', mode);
    document.querySelectorAll('#modeSeg button').forEach((b) => b.setAttribute('aria-checked', String(b.dataset.mode === mode)));
    $('#liveOpts').hidden = mode !== 'live';
    $('#demoOpts').hidden = mode !== 'demo';
    setText($('#modeHint'), t(mode === 'live' ? 'start.live.hint' : 'start.demo.hint'));
    renderModelStatus();
  }
  function renderModelStatus() {
    const s = $('#modelStatus'), m = start.models, model = $('#modelInput').value.trim();
    if (!m) { s.dataset.state = 'checking'; setText(s, t('start.model.checking')); return; }
    if (!m.reachable) { s.dataset.state = 'bad'; setText(s, t('start.model.down')); return; }
    const installed = m.models.includes(model) || m.models.includes(model + ':latest');
    s.dataset.state = installed ? 'ok' : 'bad';
    setText(s, installed ? t('start.model.ok', { n: I18N.num(m.models.length) }) : t('start.model.missing', { model }));
    renderStrengthHint();
  }
  function modelDetail(name) {
    const d = (start.models && start.models.details) || [];
    return d.find((x) => x.name === name || x.name === name + ':latest') || null;
  }
  function renderStrengthHint() {
    const model = $('#modelInput').value.trim(), d = modelDetail(model);
    const s = t('strength.' + MC.strengthOf(d ? d.parameter_size : '', model));
    setText($('#strengthHint'), start.strength !== 'auto' ? '' : d && d.parameter_size
      ? t('start.strength.hint', { s, size: d.parameter_size }) : t('start.strength.hintNoSize', { s }));
  }
  function setStrength(v) {
    start.strength = v;
    store.set('mc.strength', v);
    document.querySelectorAll('#strengthSeg button').forEach((b) => b.setAttribute('aria-checked', String(b.dataset.strength === v)));
    renderStrengthHint();
  }
  async function loadModels() {
    try {
      start.models = await api('/api/models');
    } catch (_) {
      start.models = { reachable: false, models: [] };
    }
    $('#modelList').replaceChildren(...start.models.models.map((m) => el('option', { value: m })));
    // first visit: pick Live only when it can actually work right now
    if (!store.get('mc.mode')) {
      const model = $('#modelInput').value.trim();
      const ok = start.models.reachable && (start.models.models.includes(model) || start.models.models.includes(model + ':latest'));
      setMode(ok ? 'live' : 'demo');
    }
    renderModelStatus();
  }
  function wireStart() {
    $('#projectPath').value = store.get('mc.lastPath') || '';
    $('#modelInput').value = store.get('mc.model') || $('#modelInput').value;
    try {
      const saved = JSON.parse(sessionStorage.getItem('mc.form') || 'null');
      if (saved) { $('#projectPath').value = saved.path || ''; $('#prompt').value = saved.prompt || ''; sessionStorage.removeItem('mc.form'); }
    } catch (_) { /* ignore */ }
    setMode(store.get('mc.mode') === 'demo' ? 'demo' : 'live');
    setStrength(store.get('mc.strength') || 'auto');
    $('#ctxSelect').value = store.get('mc.ctx') || '';
    $('#approvalToggle').checked = store.get('mc.approval') !== '0';
    $('#strengthSeg').addEventListener('click', (e) => { const b = e.target.closest('button'); if (b) setStrength(b.dataset.strength); });
    $('#ctxSelect').addEventListener('change', () => store.set('mc.ctx', $('#ctxSelect').value));
    $('#approvalToggle').addEventListener('change', () => store.set('mc.approval', $('#approvalToggle').checked ? '1' : '0'));
    loadModels();

    const drop = $('#drop'), input = $('#fileInput');
    drop.addEventListener('click', () => input.click());
    drop.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); } });
    input.addEventListener('change', () => { addFiles(input.files); input.value = ''; });
    ['dragenter', 'dragover'].forEach((n) => drop.addEventListener(n, (e) => { e.preventDefault(); drop.classList.add('drag'); }));
    ['dragleave', 'drop'].forEach((n) => drop.addEventListener(n, (e) => { e.preventDefault(); drop.classList.remove('drag'); }));
    drop.addEventListener('drop', (e) => addFiles(e.dataTransfer.files));
    window.addEventListener('dragover', (e) => e.preventDefault());   // a file dropped outside must not navigate away
    window.addEventListener('drop', (e) => e.preventDefault());

    const path = $('#projectPath');
    path.addEventListener('change', () => { path.value = tidyPath(path.value); });
    path.addEventListener('paste', () => setTimeout(() => { path.value = tidyPath(path.value); }, 0));
    $('#pickFolder').addEventListener('click', pickFolder);
    $('#modeSeg').addEventListener('click', (e) => { const b = e.target.closest('button'); if (b) setMode(b.dataset.mode); });
    $('#modelInput').addEventListener('input', renderModelStatus);
    $('#modelInput').addEventListener('change', () => store.set('mc.model', $('#modelInput').value.trim()));
    $('#speedSeg').addEventListener('click', (e) => {
      const b = e.target.closest('button'); if (!b) return;
      start.speed = Number(b.dataset.speed);
      document.querySelectorAll('#speedSeg button').forEach((x) => x.setAttribute('aria-checked', String(x === b)));
    });
    $('#prompt').addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); $('#startForm').requestSubmit(); } });
    $('#startForm').addEventListener('submit', submitStart);
  }
  async function pickFolder() {
    const btn = $('#pickFolder'), input = $('#projectPath');
    btn.disabled = true; setText(btn, t('start.folder.waiting'));
    try {
      const r = await postJSON('/api/pick-folder', { initial: tidyPath(input.value), lang: I18N.lang });
      if (r.path) { input.value = r.path; input.dispatchEvent(new Event('change')); }
    } catch (ex) {
      if (ex.code === 'native_unavailable') {
        toast(t('picker.fallback'), 'warn');
        openFolderPicker(input.value, (p) => { input.value = p; });
      } else toast(errText(ex), 'err');
    } finally { btn.disabled = false; setText(btn, t('start.folder.browse')); }
  }
  async function submitStart(e) {
    e.preventDefault();
    const err = $('#startError'), btn = $('#startBtn');
    err.hidden = true;
    const project_path = tidyPath($('#projectPath').value), prompt = $('#prompt').value.trim();
    $('#projectPath').value = project_path;
    if (!project_path) return fail(t('start.need.folder'), '#projectPath');
    if (!prompt) return fail(t('start.need.prompt'), '#prompt');
    btn.disabled = true; setText(btn, t('start.going'));
    try {
      let batch = '';
      if (start.files.length) {
        batch = (crypto.randomUUID ? crypto.randomUUID() : Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, '0')).join('')).replace(/-/g, '');
        for (const f of start.files) {
          await api(`/api/upload?batch=${batch}`, { method: 'POST', headers: { 'X-Filename': encodeURIComponent(f.name) }, body: f });
        }
      }
      const model = $('#modelInput').value.trim();
      await postJSON('/api/run', { project_path, prompt, batch, mode: start.mode, model, speed: start.speed,
        review: $('#reviewToggle').checked, lang: I18N.lang, strength: start.strength,
        num_ctx: $('#ctxSelect').value ? Number($('#ctxSelect').value) : null,
        require_approval: $('#approvalToggle').checked, on_failure: 'ask' });
      store.set('mc.lastPath', project_path);
      store.set('mc.model', model);
      resetRoom(); showRoom(); connect();
    } catch (ex) { fail(errText(ex)); } finally { btn.disabled = false; setText(btn, t('start.go')); }
    function fail(msg, focusSel) { err.textContent = msg; err.hidden = false; if (focusSel) $(focusSel).focus(); }
  }

  // ───────────────────────── built-in folder browser (fallback when the native dialog is unavailable)
  function openFolderPicker(initial, onPick) {
    const root = $('#modalRoot');
    let current = null;
    const input = el('input', { dir: 'ltr', spellcheck: 'false', 'aria-label': t('picker.path') });
    const list = el('ul', { class: 'dlist' });
    const choose = el('button', { class: 'btn primary', type: 'button' }, t('picker.choose'));
    const close = () => { root.replaceChildren(); document.removeEventListener('keydown', onKey); };
    const onKey = (e) => { if (e.key === 'Escape') close(); };
    const go = el('button', { class: 'btn', type: 'button', onclick: () => load(tidyPath(input.value)) }, t('picker.go'));
    const up = el('button', { class: 'btn', type: 'button' }, t('picker.up'));
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); load(tidyPath(input.value)); } });
    choose.addEventListener('click', () => { if (current && current.path !== '::drives') { onPick(current.path); close(); } });
    const dialog = el('div', { class: 'dialog', role: 'dialog', 'aria-modal': 'true', 'aria-label': t('picker.title') },
      el('h2', null, t('picker.title')), el('div', { class: 'pathbar' }, up, input, go), list,
      el('footer', null, el('button', { class: 'btn', type: 'button', onclick: close }, t('picker.cancel')), choose));
    const overlay = el('div', { class: 'modal', onmousedown: (e) => { if (e.target === overlay) close(); } }, dialog);
    root.replaceChildren(overlay);
    document.addEventListener('keydown', onKey);
    async function load(path) {
      try {
        const d = await api('/api/fs?path=' + encodeURIComponent(path || ''));
        current = d;
        input.value = d.path === '::drives' ? '' : d.path;
        choose.disabled = d.path === '::drives';
        up.disabled = d.parent === null;
        up.onclick = () => d.parent !== null && load(d.parent);
        const rows = d.dirs.map((x) => el('li', { tabindex: '0', onclick: () => load(x.path),
          onkeydown: (e) => { if (e.key === 'Enter') load(x.path); } }, el('span', { 'aria-hidden': 'true' }, '▸'), x.name));
        list.replaceChildren(...(rows.length ? rows : [el('li', { class: 'none', dir: I18N.dir }, t('picker.empty'))]));
        list.scrollTop = 0;
      } catch (ex) { toast(errText(ex), 'err'); }
    }
    load(tidyPath(initial));
    input.focus();
  }

  // ───────────────────────── connection
  let es = null, lostToastAt = 0;
  function disconnect() { if (es) { es.close(); es = null; } }
  function connect() {
    disconnect();
    ui.live = false;
    es = new EventSource('/api/events?after=0');
    es.addEventListener('ev', (m) => onEvent(JSON.parse(m.data)));
    es.addEventListener('ready', () => { ui.live = true; scheduleRender(); });
    es.onerror = () => {
      if (Date.now() - lostToastAt > 8000) {
        lostToastAt = Date.now();
        toast(es && es.readyState === EventSource.CLOSED ? t('toast.lost') : t('toast.reconnecting'), 'warn');
      }
    };
  }
  function onEvent(ev) {
    if (ev.seq <= state.lastSeq) return;
    const fx = MC.applyEvent(state, ev);
    if (ui.live) handleEffects(fx);
    else for (const f of fx) if (f.kind === 'file-created') ui.selectedFile = f.path; // silent backlog: just follow the files
    scheduleRender();
  }
  function resetRoom() {
    disconnect();
    Object.assign(state, MC.createState());
    for (const { slot } of ui.cardEls.values()) slot.remove();
    ui.cardEls.clear(); ui.rowEls.clear();
    Object.assign(ui, { sel: null, drawerId: null, follow: true, selectedFile: null, layout: null, layoutSig: '', txFor: null, txCount: 0,
      lastOrchText: '', gateModels: {}, gateBusy: false, logCount: 0 });
    ui.rendered = { path: null, len: 0 }; ui.lineEls = [];
    ['#plan', '#tree', '#viewerHead', '#runChip', '#dockPreview', '#gate'].forEach((s) => { $(s).__sig = undefined; });
    $('#toasts').replaceChildren();   // messages about the previous run must not linger over the next one
    closeDrawer();
  }

  // ───────────────────────── effects (live only)
  function handleEffects(fx) {
    for (const f of fx) {
      switch (f.kind) {
        case 'dispatch': pulse(f.agent, 'out'); break;
        case 'report': pulse(f.agent, 'back'); break;
        case 'verified': flash(ui.cardEls.get(f.agent)?.card, 'verifiedflash'); break;
        case 'file-created': ui.fresh.add(f.path); if (ui.follow) ui.selectedFile = f.path; break;
        case 'plan': if (f.updated) ui.planFlash = f.diff; break;
        case 'agent-removed': if (ui.drawerId === f.agent) closeDrawer(); break;
        case 'attention': flash($('#center'), 'hit'); break;
        case 'error': if (!f.task) toast(f.message, 'err'); break;   // task failures live on the card, in Logs and in the decision panel
        case 'run-end': toast(f.status === 'completed' ? t('toast.done') : t('toast.ended'), f.status === 'completed' ? '' : 'warn'); break;
        default: break;
      }
    }
  }
  function flash(node, cls) {
    if (!node) return;
    node.classList.remove(cls); void node.offsetWidth; node.classList.add(cls);
    node.addEventListener('animationend', () => node.classList.remove(cls), { once: true });
  }

  // ───────────────────────── render scheduling
  let raf = 0;
  function scheduleRender() { if (!raf) raf = requestAnimationFrame(() => { raf = 0; renderAll(); }); }
  function renderAll() {
    if ($('#room').hidden) return;
    renderTopbar(); renderPlan(); renderAgents(); renderCenter(); renderGate(); renderDock(); renderDrawer();
  }

  // ───────────────────────── top bar
  function elapsedMs() {
    const r = state.run;
    if (!r.startedMs) return 0;
    return Math.max(0, (r.finishedMs || (r.status === 'running' ? Date.now() : state.lastMs || r.startedMs)) - r.startedMs);
  }
  function renderTopbar() {
    const r = state.run;
    setText($('#projName'), r.projectPath); $('#projName').title = r.projectPath;
    const key = r.status === 'running'
      ? (state.attention ? 'attention' : state.approval ? 'awaiting' : r.paused ? 'paused' : r.pausing ? 'pausing' : 'running') : r.status;
    const chip = $('#runChip');
    chip.dataset.state = key;
    const badge = r.mock ? t('run.demo') : r.model;
    patch(chip, `${key}|${badge}|${I18N.lang}`, () => [el('span', { class: 'dot' }), t('run.' + key), badge ? el('em', { dir: textDir(badge) }, badge) : null]);
    setText($('#tokIn'), I18N.num(state.usage.in)); setText($('#tokOut'), I18N.num(state.usage.out));
    const ended = r.status !== 'running' && r.status !== 'idle';
    setText($('#btnPauseAll'), r.paused || r.pausing ? t('btn.resume') : t('btn.pauseAll'));
    $('#btnPauseAll').hidden = ended || !!state.approval || !!state.attention;
    $('#btnStop').hidden = ended; $('#btnNew').hidden = !ended;
    $('#btnRetry').hidden = !(ended && !r.mock && r.status === 'failed');
    const errs = state.logCounts.error, warns = state.logCounts.warn, logBadge = $('#logBadge');
    logBadge.hidden = !(errs || warns);
    logBadge.dataset.level = errs ? 'error' : 'warn';
    setText(logBadge, I18N.num(errs || warns));
    tickElapsed();
  }
  function tickElapsed() {
    const s = Math.floor(elapsedMs() / 1000);
    setText($('#elapsed'), `${I18N.pad2(Math.floor(s / 60))}:${I18N.pad2(s % 60)}`);
  }
  setInterval(() => { if (!$('#room').hidden) tickElapsed(); }, 500);

  // ───────────────────────── plan sidebar
  const TICK_SVG = '<svg viewBox="0 0 14 14" aria-hidden="true"><path class="p1" d="M1.8 7.6 4.3 10.1 9.2 4.6"/><path class="p2" d="M5.4 10.2 6.9 11.7 12.4 5.6"/><path class="px" d="M3.5 3.5l7 7M10.5 3.5l-7 7"/></svg>';
  function planRow(task) {
    let row = ui.rowEls.get(task.id);
    if (!row) {
      const tick = el('span', { class: 'tick' });
      tick.innerHTML = TICK_SVG; // static markup, no model text
      row = el('div', { class: 'prow', tabindex: '0', role: 'button', dataset: { id: task.id } }, tick,
        el('div', { class: 'pbody' }, el('div', { class: 'ptitle' }), el('div', { class: 'psub' }, el('span', { class: 'prole' }), el('span', { class: 'pstat' }))));
      const open = () => { const a = MC.agentOfTask(state, task.id); if (a) openDrawer(a.id); };
      row.addEventListener('click', open);
      row.addEventListener('keydown', (e) => { if (e.key === 'Enter') open(); });
      ui.rowEls.set(task.id, row);
      if (ui.live) { row.classList.add('added'); row.addEventListener('animationend', () => row.classList.remove('added'), { once: true }); }
    }
    const a = MC.agentOfTask(state, task.id);
    // the plan and the cards must tell the same story: a "running" task whose agent is paused is paused
    const ended = state.run.status === 'stopped' || state.run.status === 'failed';
    let shown = task.status === 'running' && ((a && a.status === 'paused') || state.run.paused) ? 'paused' : task.status;
    if (ended && (task.status === 'running' || task.status === 'pending')) shown = 'halted';   // will never run now
    if (task.removed) shown = 'removed';                                                         // dropped from the plan by the lead
    const tick = row.querySelector('.tick');
    if (tick.dataset.s && tick.dataset.s !== shown && ui.live) {   // animate only a REAL status change
      tick.classList.remove('pop'); void tick.offsetWidth; tick.classList.add('pop');
      tick.addEventListener('animationend', () => tick.classList.remove('pop'), { once: true });
    }
    tick.dataset.s = shown;
    setTextD(row.querySelector('.ptitle'), task.title);
    setText(row.querySelector('.prole'), task.role);
    setText(row.querySelector('.pstat'), t(shown === 'halted' && task.status === 'pending' ? 'task.notRun' : 'task.' + shown));
    row.classList.toggle('removed', !!task.removed);
    row.classList.toggle('focus', !!a && a.id === ui.sel);
    return row;
  }
  function renderPlan() {
    const root = $('#plan'), groups = MC.planGroups(state);
    state.tasks.forEach((task) => planRow(task));   // rows are updated in place, every time
    const sig = [I18N.lang, state.plan.version, state.plan.reason, state.decisions.length,
      groups.map((g) => g.ids.join(',') + (g.parallel ? 'P' : '') + (g.removed ? 'R' : '')).join('|')].join('#');
    const prevReason = root.__reason;
    const rebuilt = patch(root, sig, () => {
      const head = el('div', { class: 'plan-head' }, el('h2', null, t('plan.title')),
        state.plan.version ? el('span', { class: 'plan-ver' }, t('plan.version', { n: I18N.num(state.plan.version) })) : null);
      if (!state.plan.version) return [head, el('p', { class: 'plan-empty' }, t('plan.empty'))];
      const kids = [head];
      if (state.plan.reason) kids.push(el('div', { class: 'plan-reason', dir: textDir(state.plan.reason) }, state.plan.reason));
      for (const g of groups) {
        kids.push(el('div', { class: `wave${g.parallel ? ' parallel' : ''}` },
          g.parallel ? el('span', { class: 'wave-tag' }, t('plan.parallel')) : null, ...g.ids.map((id) => ui.rowEls.get(id))));
      }
      if (state.decisions.length) {
        kids.push(el('div', { class: 'decisions' }, el('h3', null, t('plan.decisions')),
          el('ul', null, ...state.decisions.map((d) => el('li', { dir: textDir(d.text) }, d.text)))));
      }
      return kids;
    });
    root.__reason = state.plan.reason;
    if (rebuilt && ui.live && state.plan.reason && state.plan.reason !== prevReason) flash(root.querySelector('.plan-reason'), 'reasonin');
    if (ui.planFlash) {
      const { added, changed } = ui.planFlash;
      added.forEach((id) => flash(ui.rowEls.get(id), 'added'));
      changed.forEach((id) => flash(ui.rowEls.get(id), 'changed'));
      ui.planFlash = null;
    }
  }

  // ───────────────────────── stage layout: cards on a bus loop around the file explorer
  function pointOnLoop(L, s) {
    const { x0, y0, bw, bh, P } = L; const cx = x0 + bw / 2;
    s = ((s % P) + P) % P;
    if (s < bw / 2) return { x: cx + s, y: y0 };
    s -= bw / 2; if (s < bh) return { x: x0 + bw, y: y0 + s };
    s -= bh; if (s < bw) return { x: x0 + bw - s, y: y0 + bh };
    s -= bw; if (s < bh) return { x: x0, y: y0 + bh - s };
    s -= bh; return { x: x0 + s, y: y0 };
  }
  const overlaps = (a, b) => Math.abs(a.x - b.x) < (a.w + b.w) / 2 && Math.abs(a.y - b.y) < (a.h + b.h) / 2;
  function computeLayout(ids) {
    const stage = $('#stage'); const W = stage.clientWidth, H = stage.clientHeight;
    if (W < 900 || H < 500) return { mode: 'flow', W, H };
    const x0 = CARD.w / 2 + 10, y0 = CARD.h / 2 + 10;
    const L = { mode: 'ring', W, H, x0, y0, bw: W - 2 * x0, bh: H - 2 * y0, pos: new Map() };
    L.P = 2 * (L.bw + L.bh);
    L.at = (s) => pointOnLoop(L, s);
    const taken = [{ x: W / 2, y: y0, w: ORCHBOX.w + 20, h: ORCHBOX.h + 20 }];
    const n = ids.length;
    ids.forEach((id, i) => {
      let s = ((i + 1) * L.P) / (n + 1), placed = null;
      for (let k = 0; k < 400 && !placed; k++, s += 5) {
        const p = L.at(s), box = { x: p.x, y: p.y, w: CARD.w + 12, h: CARD.h + 12 };
        if (!taken.some((o) => overlaps(o, box))) { taken.push(box); placed = { x: p.x, y: p.y, s: ((s % L.P) + L.P) % L.P }; }
      }
      if (!placed) { const p = L.at(((i + 1) * L.P) / (n + 1)); placed = { x: p.x, y: p.y, s: ((i + 1) * L.P) / (n + 1) }; }
      L.pos.set(id, placed);
    });
    return L;
  }
  function applyLayout(L) {
    const stage = $('#stage'), center = $('#center'), orch = $('#orch'), bus = $('#bus');
    stage.classList.toggle('flow', L.mode === 'flow');
    document.documentElement.style.setProperty('--dock-h', $('#dock').offsetHeight + 'px');
    stage.style.setProperty('--cw', CARD.w + 'px'); stage.style.setProperty('--ch', CARD.h + 'px');
    stage.style.setProperty('--ow', ORCHBOX.w + 'px'); stage.style.setProperty('--oh', ORCHBOX.h + 'px');
    if (L.mode === 'flow') {
      ['left', 'top', 'right', 'bottom'].forEach((k) => { center.style[k] = ''; });
      orch.style.transform = ''; bus.replaceChildren();
      return;
    }
    const { W, H, x0, y0, bw, bh } = L;
    bus.setAttribute('viewBox', `0 0 ${W} ${H}`);
    const loop = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    loop.setAttribute('class', 'loop');
    loop.setAttribute('d', `M${W / 2} ${y0}H${x0 + bw}V${y0 + bh}H${x0}V${y0}Z`);
    bus.replaceChildren(loop);
    orch.style.transform = `translate(${W / 2 - ORCHBOX.w / 2}px, ${y0 - ORCHBOX.h / 2}px)`;
    center.style.left = center.style.right = CARD.w + 26 + 'px';
    center.style.top = CARD.h + 26 + 'px'; center.style.bottom = CARD.h + 26 + 'px';
  }

  function buildCard(a, idx) {
    const slot = el('div', { class: 'slot' });
    const card = el('div', { class: 'card enter', tabindex: '0', role: 'button', style: `--d:${idx * 110}ms`, dataset: { id: a.id } },
      el('div', { class: 'card-top' }, el('span', { class: 'card-title' }), el('span', { class: 'card-st' })),
      el('div', { class: 'card-meta' }, el('span', { class: 'role' }), el('span', { class: 'model' })),
      el('div', { class: 'card-task' }), el('div', { class: 'card-act' }), el('div', { class: 'card-bar' }));
    card.addEventListener('animationend', (e) => { if (e.animationName === 'cardIn') card.classList.remove('enter'); });
    card.addEventListener('click', () => openDrawer(a.id));
    card.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openDrawer(a.id); } });
    slot.append(card);
    $('#cards').append(slot);
    return { slot, card };
  }
  function renderAgents() {
    // agents the lead removed from the plan fade out; the others then glide into the new layout
    for (const [id, parts] of [...ui.cardEls]) {
      if (state.agents.has(id)) continue;
      ui.cardEls.delete(id);
      if (ui.live && !reduceMotion) {
        parts.card.classList.add('leaving');
        setTimeout(() => parts.slot.remove(), 350);
      } else parts.slot.remove();
    }
    const ids = [...state.agents.keys()];
    const stage = $('#stage');
    const sig = ids.join(',') + '|' + stage.clientWidth + 'x' + stage.clientHeight;
    if (sig !== ui.layoutSig) { ui.layoutSig = sig; ui.layout = computeLayout(ids); applyLayout(ui.layout); }
    state.agents.forEach((a) => {
      let parts = ui.cardEls.get(a.id);
      if (!parts) { parts = buildCard(a, ui.live ? 0 : ui.cardEls.size); ui.cardEls.set(a.id, parts); if (!ui.live) parts.card.classList.remove('enter'); }
      const { slot, card } = parts;
      const ds = MC.agentDisplayStatus(state, a), task = MC.agentTask(state, a);
      card.dataset.s = ds;
      card.classList.toggle('sel', ui.sel === a.id);
      setTextD(card.querySelector('.card-title'), a.title);
      setText(card.querySelector('.card-st'), t('agent.' + ds));
      setText(card.querySelector('.role'), a.role);
      setText(card.querySelector('.model'), a.model);
      setTextD(card.querySelector('.card-task'), task ? task.title : '');
      const act = card.querySelector('.card-act'), actKey = a.currentFile || '';
      if (act.dataset.k !== actKey) { act.dataset.k = actKey; act.textContent = actKey; if (actKey) act.append(el('span', { class: 'caret' })); }
      const p = ui.layout.mode === 'ring' && ui.layout.pos.get(a.id);
      slot.style.transform = p ? `translate(${p.x - CARD.w / 2}px, ${p.y - CARD.h / 2}px)` : '';
    });
    renderOrch();
  }
  function renderOrch() {
    const o = $('#orch'), r = state.run;
    const model = r.model || [...state.agents.values()][0]?.model || '';
    const ended = r.status !== 'running' && r.status !== 'idle';
    const text = ended ? (r.summary || t('run.' + r.status)) : (state.orchStatus || t('lead.preparing'));
    o.dataset.live = r.status === 'running' && !r.paused ? '1' : '0';
    o.setAttribute('aria-label', t('lead.name'));
    if (!o.firstChild) {
      o.append(el('div', { class: 'orch-top' }, el('span', { class: 'led' }), el('span', { class: 'orch-name' }), el('small', null, model)),
        el('div', { class: 'orch-status' }));
      o.addEventListener('click', () => openDrawer(ORCH));
      o.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openDrawer(ORCH); } });
    }
    setText(o.querySelector('.orch-name'), t('lead.name'));
    setText(o.querySelector('small'), model);
    const st = o.querySelector('.orch-status');
    if (text !== ui.lastOrchText) { ui.lastOrchText = text; setTextD(st, text); if (ui.live) flash(st, 'swap'); }
  }

  // pulse along the bus loop: lead agent -> card ("out") or card -> lead agent ("back")
  function pulse(agentId, dir) {
    const L = ui.layout, parts = ui.cardEls.get(agentId);
    const done = () => (dir === 'out' ? flash(parts?.card, 'hit') : flash($('#orch'), 'hit'));
    const pos = L && L.mode === 'ring' && L.pos.get(agentId);
    if (!pos || reduceMotion || document.hidden) return done();
    const clockwise = pos.s <= L.P / 2, len = clockwise ? pos.s : L.P - pos.s;
    const pts = [];
    for (let d = 0; d < len; d += 6) pts.push(L.at(clockwise ? d : -d));
    pts.push(L.at(clockwise ? len : -len));
    if (dir === 'back') pts.reverse();
    const NS = 'http://www.w3.org/2000/svg', cls = dir === 'back' ? ' report' : '';
    const trail = document.createElementNS(NS, 'polyline'); trail.setAttribute('class', 'trail' + cls);
    const head = document.createElementNS(NS, 'circle'); head.setAttribute('class', 'head' + cls); head.setAttribute('r', '5');
    $('#bus').append(trail, head);
    const dur = Math.min(1300, Math.max(480, len * 1.5)), t0 = performance.now();
    (function step(now) {
      const p = Math.min(1, (now - t0) / dur), e = p < .5 ? 4 * p ** 3 : 1 - (-2 * p + 2) ** 3 / 2;
      const i = Math.round(e * (pts.length - 1));
      trail.setAttribute('points', pts.slice(Math.max(0, i - 26), i + 1).map((q) => `${q.x},${q.y}`).join(' '));
      head.setAttribute('cx', pts[i].x); head.setAttribute('cy', pts[i].y);
      if (p < 1) requestAnimationFrame(step); else { trail.remove(); head.remove(); done(); }
    })(t0);
  }

  // ───────────────────────── centre: file tree + live viewer
  function renderCenter() {
    const files = [...state.files.values()];
    setText($('#fileCount'), files.length ? t('files.count', { n: I18N.num(files.length) }) : '');
    if (!ui.selectedFile || !state.files.has(ui.selectedFile)) ui.selectedFile = files.length ? files[files.length - 1].path : null;
    $('#followBtn').hidden = ui.follow;
    const tree = $('#tree');
    const treeSig = files.map((f) => f.path + ':' + f.status).join('|') + '#' + ui.selectedFile;
    patch(tree, treeSig, () => {
      const rows = [];
      const walk = (node, depth) => {   // root = depth 0; its files and top-level dirs are not indented
        if (node.name) rows.push(el('div', { class: 'trow dir', style: `--depth:${depth - 1}` }, el('span', { class: 'glyph' }, '▾'), node.name));
        node.dirs.forEach((d) => walk(d, depth + 1));
        node.files.forEach((f) => {
          const fs = state.files.get(f.path);
          const row = el('div', { class: `trow file${f.path === ui.selectedFile ? ' sel' : ''}${ui.fresh.has(f.path) ? ' fresh' : ''}`, style: `--depth:${depth}`, role: 'button', tabindex: '0' },
            el('span', { class: 'glyph' }), f.name, fs && fs.status === 'writing' ? el('span', { class: 'live', title: t('files.writing') }) : null);
          const pick = () => { ui.follow = false; ui.selectedFile = f.path; scheduleRender(); };
          row.addEventListener('click', pick);
          row.addEventListener('keydown', (e) => { if (e.key === 'Enter') pick(); });
          rows.push(row);
        });
      };
      walk(MC.fileTree(state), 0);
      ui.fresh.clear();
      return rows;
    });
    syncCode();
  }
  function syncCode() {
    const f = state.files.get(ui.selectedFile);
    const code = $('#code'), head = $('#viewerHead');
    $('#viewerEmpty').hidden = !!f; code.hidden = !f; head.hidden = !f;
    if (!f) { ui.rendered = { path: null, len: 0 }; ui.lineEls = []; code.replaceChildren(); return; }
    const owner = state.agents.get(f.agent);
    patch(head, `${f.path}|${owner ? owner.title : ''}|${f.status}|${f.size}|${I18N.lang}`, () => [el('span', { class: 'path' }, f.path),
      owner ? el('span', { class: 'badge', dir: textDir(owner.title) }, owner.title) : null,
      f.status === 'writing' ? el('span', { class: 'writing' }, t('files.writing')) : el('span', null, I18N.size(f.size))]);
    const lines = f.content.split('\n');
    const mk = (i, text) => { const tx = el('span', { class: 't' }, text); return { row: el('div', { class: 'ln' }, el('span', { class: 'n' }, String(i + 1)), tx), t: tx }; };
    if (ui.rendered.path !== f.path || f.content.length < ui.rendered.len) {
      ui.lineEls = lines.map((text, i) => mk(i, text));
      code.replaceChildren(...ui.lineEls.map((l) => l.row));
      code.scrollTop = 0;
    } else if (f.content.length > ui.rendered.len) {
      const old = ui.lineEls.length;
      ui.lineEls[old - 1].t.textContent = lines[old - 1];
      for (let i = old; i < lines.length; i++) { const l = mk(i, lines[i]); ui.lineEls.push(l); code.append(l.row); }
      if (f.status === 'writing') code.scrollTop = code.scrollHeight;
    }
    ui.rendered = { path: f.path, len: f.content.length };
    code.querySelectorAll('.caret').forEach((c) => c.remove());
    if (f.status === 'writing' && ui.lineEls.length) ui.lineEls[ui.lineEls.length - 1].t.append(el('span', { class: 'caret' }));
  }

  // ───────────────────────── gate: the go-ahead after planning, and decisions on failed tasks
  function modelLabel(name) {
    const size = (state.approval && state.approval.sizes && (state.approval.sizes[name] || state.approval.sizes[name + ':latest'])) || '';
    const strength = t('strength.' + MC.strengthOf(size, name));
    return size ? t('gate.option', { name, size, strength }) : t('gate.optionNoSize', { name, strength });
  }
  function renderGate() {
    const gate = $('#gate'), ap = state.approval, at = state.attention;
    gate.hidden = !ap && !at;
    if (ap) {
      const ids = Object.keys(ap.agents);
      patch(gate, 'A|' + ids.join(',') + '|' + ap.models.join(',') + '|' + I18N.lang, () => {
        const rows = ids.map((aid) => {
          const a = state.agents.get(aid), task = a && MC.agentTask(state, a);
          const current = ui.gateModels[aid] || ap.agents[aid];
          const names = [...new Set([...ap.models, ap.agents[aid]])];
          const sel = el('select', { 'aria-label': t('gate.model'), dir: 'ltr',
            onchange: (e) => { ui.gateModels[aid] = e.target.value; } },
            ...names.map((n) => { const o = el('option', { value: n }, modelLabel(n)); if (n === current) o.selected = true; return o; }));
          return el('tr', null,
            el('td', null, el('b', { dir: textDir(a ? a.title : aid) }, a ? a.title : aid), el('small', null, a ? a.role : '')),
            el('td', { dir: textDir(task ? task.title : '') }, task ? task.title : ''),
            el('td', null, sel));
        });
        const go = el('button', { class: 'btn primary', type: 'button', id: 'gateStart', onclick: approve }, t('gate.start'));
        return [el('div', { class: 'gate-card approval', role: 'dialog', 'aria-labelledby': 'gateTitle' },
          el('h3', { id: 'gateTitle' }, t('gate.title')), el('p', { class: 'gate-lead' }, t('gate.lead', { n: I18N.num(ids.length) })),
          el('div', { class: 'gate-body' },
            el('table', { class: 'gate-table' }, el('thead', null, el('tr', null, el('th', null, t('gate.agent')), el('th', null, t('gate.task')),
              el('th', null, t('gate.model')))), el('tbody', null, ...rows))),
          el('footer', null, go))];
      });
      return;
    }
    if (at) {
      patch(gate, 'T|' + at.taskId + '|' + at.summary + '|' + I18N.lang, () => {
        const note = el('textarea', { rows: '2', id: 'attNote', placeholder: t('att.note.ph'), 'aria-label': t('att.note'), dir: 'auto' });
        const act = (decision) => resolve(at.taskId, decision, note.value.trim());
        return [el('div', { class: 'gate-card attention', role: 'alertdialog', 'aria-labelledby': 'attTitle' },
          el('h3', { id: 'attTitle' }, t('att.title'), el('span', { class: 'att-task', dir: textDir(at.title) }, at.title)),
          el('p', { class: 'gate-lead', dir: textDir(at.summary) }, at.summary),
          el('div', { class: 'gate-body' },
            at.reasons.length ? el('section', null, el('h4', null, t('att.why')),
              el('ul', { class: 'reasons' }, ...at.reasons.map((r) => el('li', { dir: textDir(r) }, r)))) : null,
            at.suggestions.length ? el('section', null, el('h4', null, t('att.help')),
              el('ul', null, ...at.suggestions.map((r) => el('li', { dir: textDir(r) }, r)))) : null,
            at.diagnosis ? el('details', null, el('summary', null, t('att.diagnosis')), el('p', { dir: textDir(at.diagnosis) }, at.diagnosis)) : null),
          el('label', { class: 'att-note' }, el('span', null, t('att.note')), note),
          el('footer', null,
            el('button', { class: 'btn danger', type: 'button', onclick: () => act('stop') }, t('att.stop')),
            el('button', { class: 'btn', type: 'button', onclick: () => act('skip') }, t('att.skip')),
            el('button', { class: 'btn primary', type: 'button', onclick: () => act('retry') }, t('att.retry'))))];
      });
      return;
    }
    gate.__sig = undefined;
  }
  async function approve() {
    if (ui.gateBusy || !state.approval) return;
    const btn = $('#gateStart');
    ui.gateBusy = true; btn.disabled = true; setText(btn, t('gate.starting'));
    const changed = {};
    for (const [aid, m] of Object.entries(ui.gateModels)) if (m !== state.approval.agents[aid]) changed[aid] = m;
    try { await postJSON('/api/approve', { models: changed }); }
    catch (ex) { toast(errText(ex), 'err'); btn.disabled = false; setText(btn, t('gate.start')); }
    finally { ui.gateBusy = false; }
  }
  async function resolve(taskId, decision, note) {
    try { await postJSON('/api/resolve', { task_id: taskId, decision, note }); }
    catch (ex) { toast(errText(ex), 'err'); }
  }

  // ───────────────────────── logs: what happened, and a report to hand over
  function logVisible(l) {
    if (l.level === 'debug') return ui.logDebug;
    if (ui.logFilter === 'errors') return l.level === 'error';
    if (ui.logFilter === 'problems') return l.level === 'error' || l.level === 'warn';
    return true;
  }
  function logRow(l) {
    const time = (l.ts || '').slice(11, 19), where = [l.task, l.agent].filter(Boolean).join(' ');
    return el('div', { class: `logrow lv-${l.level}` },
      el('span', { class: 'lt' }, time), el('span', { class: 'lv' }, t('level.' + l.level)),
      el('span', { class: 'ls' }, l.source + (where ? ' ' + where : '')),
      el('span', { class: 'lm', dir: textDir(l.message) }, l.message),
      l.detail ? el('details', { class: 'ld' }, el('summary', null, t('logs.detail')), el('pre', { dir: 'ltr' }, l.detail)) : null);
  }
  function buildLogs() {
    const seg = el('div', { class: 'seg', role: 'radiogroup', 'aria-label': t('logs.title') },
      ...['all', 'problems', 'errors'].map((f) => el('button', { type: 'button', role: 'radio', 'aria-checked': String(ui.logFilter === f),
        onclick: () => { ui.logFilter = f; ui.logCount = -1; buildLogs(); renderDrawer(); } }, t('logs.' + f))));
    const dbg = el('label', { class: 'check' }, el('input', { type: 'checkbox', ...(ui.logDebug ? { checked: true } : {}),
      onchange: (e) => { ui.logDebug = e.target.checked; ui.logCount = -1; renderDrawer(); } }), t('logs.details'));
    const copy = el('button', { class: 'btn', type: 'button', onclick: copyReport }, t('logs.copy'));
    $('#drawer').replaceChildren(
      el('header', { class: 'd-head' }, el('div', null, el('h2', null, t('logs.title')), el('p', null, t('logs.hint'))),
        el('button', { class: 'd-close', type: 'button', 'aria-label': t('btn.close'), onclick: closeDrawer }, '×')),
      el('div', { class: 'd-controls logs-controls' }, seg, dbg),
      el('div', { class: 'd-body', id: 'dBody' }, el('div', { id: 'logList', class: 'loglist' })),
      el('div', { class: 'd-input' }, el('a', { class: 'btn primary', href: '/api/report', download: '' }, t('logs.download')), copy));
  }
  function renderLogs() {
    const list = $('#logList'); if (!list) return;
    if (ui.logCount < 0) { list.replaceChildren(); ui.logCount = 0; }
    const body = $('#dBody'), nearBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 80;
    for (let i = ui.logCount; i < state.logs.length; i++) if (logVisible(state.logs[i])) list.append(logRow(state.logs[i]));
    ui.logCount = state.logs.length;
    const empty = list.querySelector('.hint');
    if (!list.querySelector('.logrow') && !empty) list.append(el('p', { class: 'hint' }, t('logs.empty')));
    else if (list.querySelector('.logrow') && empty) empty.remove();
    if (nearBottom) body.scrollTop = body.scrollHeight;
  }
  async function copyReport() {
    try {
      const r = await fetch('/api/report');
      if (!r.ok) throw new Error(t('err.http', { status: r.status }));
      const text = await r.text();
      try { await navigator.clipboard.writeText(text); } catch (_) {
        const ta = el('textarea', { style: 'position:fixed;opacity:0' }); ta.value = text; document.body.append(ta); ta.select();
        document.execCommand('copy'); ta.remove();
      }
      toast(t('logs.copied'));
    } catch (ex) { toast(errText(ex), 'err'); }
  }

  // ───────────────────────── dock: chat with the lead agent
  function renderDock() {
    const msgs = (state.chat.get(ORCH) || []).filter((m) => m.role === 'agent');
    const last = msgs[msgs.length - 1];
    const text = last ? last.text : t('dock.open');
    patch($('#dockPreview'), text + I18N.lang, () => [el('b', null, t('lead.name')), el('span', { dir: textDir(text) }, text)]);
  }

  // ───────────────────────── drawer: one agent (or the lead) in detail
  function openDrawer(id) {
    ui.drawerId = id; ui.sel = id === ORCH || id === LOGS ? null : id; ui.txFor = null;
    if (id === LOGS) { ui.logCount = -1; buildLogs(); $('#drawer').classList.add('open'); scheduleRender(); return; }
    buildDrawer(id);
    $('#drawer').classList.add('open');
    scheduleRender();
    setTimeout(() => $('#drawer textarea')?.focus(), 320);
  }
  function closeDrawer() { ui.drawerId = null; ui.sel = null; $('#drawer').classList.remove('open'); scheduleRender(); }
  function buildDrawer(id) {
    const isLead = id === ORCH, a = state.agents.get(id);
    const name = isLead ? t('lead.name') : a ? a.title : id;
    const ta = el('textarea', { rows: '1', dir: 'auto', placeholder: t('drawer.msg.ph', { name }), 'aria-label': t('drawer.msg') });
    const send = async () => {
      const text = ta.value.trim(); if (!text) return;
      try { await postJSON('/api/message', { target: id, text }); ta.value = ''; autogrow(ta); } catch (ex) { toast(errText(ex), 'err'); }
    };
    onEnterSend(ta, send);
    const pauseBtn = el('button', { class: 'btn', type: 'button', onclick: () => sendCommand('pause', isLead ? 'all' : id) }, isLead ? t('btn.pauseAll') : t('drawer.pauseAgent'));
    const resumeBtn = el('button', { class: 'btn', type: 'button', onclick: () => sendCommand('resume', isLead ? 'all' : id) }, t('btn.resume'));
    $('#drawer').replaceChildren(
      el('header', { class: 'd-head' }, el('div', null, el('h2', { id: 'dTitle' }), el('p', { id: 'dSub' })), el('span', { id: 'dChip', class: 'card-st' }),
        el('button', { class: 'd-close', type: 'button', 'aria-label': t('btn.close'), onclick: closeDrawer }, '×')),
      el('div', { class: 'd-controls', id: 'dControls' }, pauseBtn, resumeBtn),
      el('div', { class: 'd-body', id: 'dBody' }, el('div', { id: 'dInfo' }), el('section', { class: 'dsec' }, el('h3', null, t('drawer.chat')), el('div', { class: 'tx', id: 'dTx' }))),
      el('div', { class: 'd-input' }, ta, el('button', { class: 'btn primary', type: 'button', onclick: send }, t('btn.send'))));
  }
  function renderDrawer() {
    const id = ui.drawerId; if (!id) return;
    if (id === LOGS) { renderLogs(); return; }
    const isLead = id === ORCH, a = state.agents.get(id), r = state.run;
    if (!isLead && !a) { closeDrawer(); return; }
    const ended = r.status !== 'running' && r.status !== 'idle';
    setTextD($('#dTitle'), isLead ? t('lead.name') : a.title);
    patch($('#dSub'), isLead ? 'lead' + I18N.lang : `${a.role}|${a.model}`, () => (isLead ? [t('lead.role')]
      : [el('span', null, a.role), el('span', { class: 'model' }, a.model)]));
    const chip = $('#dChip'); chip.hidden = isLead;
    if (a) { const ds = MC.agentDisplayStatus(state, a); chip.dataset.s = ds; setText(chip, t('agent.' + ds)); }
    document.querySelectorAll('#dControls button').forEach((b) => { b.disabled = ended; });
    // info section
    const info = $('#dInfo'); const kids = [];
    if (isLead) {
      const live = [...state.tasks.values()].filter((x) => !x.removed).length;
      kids.push(el('section', { class: 'dsec' }, el('h3', null, t('drawer.plan')),
        el('p', null, state.plan.version ? t('drawer.planInfo', { v: I18N.num(state.plan.version), n: I18N.num(live) }) : t('drawer.noPlan'))));
      if (state.decisions.length) kids.push(el('section', { class: 'dsec' }, el('h3', null, t('plan.decisions')),
        el('ul', { class: 'decisions-list' }, ...state.decisions.map((d) => el('li', { dir: textDir(d.text) }, d.text)))));
    } else {
      kids.push(el('section', { class: 'dsec' }, el('h3', null, t('drawer.task')),
        ...a.taskIds.map((tid) => state.tasks.get(tid)).filter(Boolean).map((task) =>
          el('div', { class: 'dtask', dir: textDir(task.title + task.goal) }, el('b', null, task.title, el('span', { class: 'tstat' }, t('task.' + task.status))),
            el('span', null, task.goal),
            el('ul', null, ...task.acceptance.map((x) => el('li', null, x)))))));
      if (a.files.size) kids.push(el('section', { class: 'dsec' }, el('h3', null, t('drawer.files')),
        el('ul', { class: 'dfiles' }, ...[...a.files].map((p) => el('li', { onclick: () => { ui.follow = false; ui.selectedFile = p; scheduleRender(); } }, p)))));
    }
    const infoSig = isLead ? `L|${state.plan.version}|${state.tasks.size}|${state.decisions.length}`
      : `A|${a.taskIds.map((x) => x + ':' + (state.tasks.get(x)?.status || '')).join(',')}|${[...a.files].join(',')}`;
    patch(info, id + infoSig + I18N.lang, () => kids);
    // transcript (append-only unless the target changed)
    const tx = $('#dTx'), msgs = state.chat.get(id) || [];
    if (ui.txFor !== id) { tx.replaceChildren(); ui.txFor = id; ui.txCount = 0; }
    const body = $('#dBody'), nearBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 80 || ui.txCount === 0;
    if (msgs.length && ui.txCount === 0) tx.replaceChildren();
    for (let i = ui.txCount; i < msgs.length; i++) {
      const m = msgs[i];
      tx.append(el('div', { class: `msg ${m.role}` }, el('span', { class: 'who' }, m.role === 'user' ? t('drawer.you') : isLead ? t('lead.name') : a.title),
        el('span', { class: 'txt', dir: textDir(m.text) }, m.text)));
    }
    if (!msgs.length && !tx.firstChild) tx.append(el('p', { class: 'hint' }, t('drawer.noMessages')));
    ui.txCount = msgs.length;
    if (nearBottom) body.scrollTop = body.scrollHeight;
  }
  async function sendCommand(command, target) {
    try {
      const r = await postJSON('/api/command', { command, target });
      if (r.note) toast(r.note, r.applied ? '' : 'warn');
    } catch (ex) { toast(errText(ex), 'err'); }
  }

  // ───────────────────────── room wiring
  function wireRoom() {
    $('#btnPauseAll').addEventListener('click', () => sendCommand(state.run.paused || state.run.pausing ? 'resume' : 'pause', 'all'));
    $('#btnStop').addEventListener('click', (e) => {
      const b = e.currentTarget;
      if (!ui.stopArmed) {
        ui.stopArmed = true; setText(b, t('btn.stopConfirm'));
        setTimeout(() => { ui.stopArmed = false; setText(b, t('btn.stop')); }, 3000);
        return;
      }
      ui.stopArmed = false; setText(b, t('btn.stop')); sendCommand('stop', 'all');
    });
    $('#btnLogs').addEventListener('click', () => (ui.drawerId === LOGS ? closeDrawer() : openDrawer(LOGS)));
    $('#btnRetry').addEventListener('click', async () => {
      try { await postJSON('/api/retry', {}); } catch (ex) { toast(errText(ex), 'err'); return; }
      resetRoom(); showRoom(); connect(); toast(t('toast.retrying'));
    });
    $('#btnNew').addEventListener('click', async () => {
      try { await postJSON('/api/reset', {}); } catch (ex) { toast(errText(ex), 'err'); return; }
      resetRoom(); showStart();
    });
    $('#followBtn').addEventListener('click', () => {
      ui.follow = true;
      const w = [...state.files.values()].filter((f) => f.status === 'writing');
      if (w.length) ui.selectedFile = w[w.length - 1].path;
      scheduleRender();
    });
    $('#dockPreview').addEventListener('click', () => openDrawer(ORCH));
    const input = $('#dockInput');
    const send = async () => {
      const text = input.value.trim(); if (!text) return;
      try { await postJSON('/api/message', { target: ORCH, text }); input.value = ''; autogrow(input); } catch (ex) { toast(errText(ex), 'err'); }
    };
    onEnterSend(input, send);
    $('#dock').addEventListener('submit', (e) => { e.preventDefault(); send(); });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && ui.drawerId && !$('#modalRoot').firstChild) closeDrawer(); });
    new ResizeObserver(() => { ui.layoutSig = ''; scheduleRender(); }).observe($('#stage'));
  }

  // ───────────────────────── boot
  async function boot() {
    I18N.apply(document);
    document.querySelectorAll('[data-lang]').forEach((b) => b.addEventListener('click', () => switchLang(b.dataset.lang)));
    wireStart(); wireRoom();
    try {
      const s = await api('/api/status');
      if (s.session) { showRoom(); connect(); } else showStart();
    } catch (ex) { showStart(); toast(t('toast.noServer', { msg: errText(ex) }), 'err'); }
  }
  window.__mc = { state, ui, render: scheduleRender };  // handy for debugging in the console
  boot();
})();
