/* The UI's brain: a pure reducer from the event stream to view state.
 * No DOM here, so it runs in the browser AND in Node (tests/ui_state.test.mjs).
 * applyEvent() mutates the state and returns "effects" - hints for animations (pulses, flashes)
 * that a live UI plays and a silent rebuild (page refresh / reconnect) ignores. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.MC = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const ORCH = 'orchestrator';

  function createState() {
    return {
      run: { id: null, status: 'idle', prompt: '', projectPath: '', mock: false, model: '', startedMs: null,
             finishedMs: null, paused: false, pausing: false, summary: '' },
      orchStatus: '',
      plan: { version: 0, stages: [], reason: '', diff: null },
      tasks: new Map(),     // id -> {id,title,role,goal,dependsOn,owns,acceptance,status,removed}
      agents: new Map(),    // id -> {id,role,model,title,taskIds,status,currentFile,files:Set}
      chat: new Map(),      // agentId (incl. 'orchestrator') -> [{role,text,seq}]
      files: new Map(),     // path -> {path,agent,content,status,size}
      decisions: [],
      logs: [],                 // diagnostics: {seq, ts, level, source, message, detail, task, agent}
      logCounts: { debug: 0, info: 0, warn: 0, error: 0 },
      approval: null,           // {agents: {id: model}, models: [...], sizes: {model: "9.2B"}} while waiting for the go-ahead
      attention: null,          // {taskId, title, summary, diagnosis, reasons, suggestions, options} while the user must decide
      usage: { in: 0, out: 0 },
      errors: [],
      lastSeq: 0,
      lastMs: null,
    };
  }

  function pushChat(st, id, role, text, seq) {
    if (!st.chat.has(id)) st.chat.set(id, []);
    st.chat.get(id).push({ role, text, seq });
  }

  function agentOfTask(st, taskId) {
    for (const a of st.agents.values()) if (a.taskIds.includes(taskId)) return a;
    return null;
  }

  function computeStages(tasks) {
    const byId = new Map(tasks.map((t) => [t.id, t]));
    const depth = new Map();
    const d = (id, seen = new Set()) => {
      if (depth.has(id)) return depth.get(id);
      if (seen.has(id)) return 0;
      seen.add(id);
      const t = byId.get(id);
      const deps = (t && t.depends_on || []).filter((x) => byId.has(x));
      const v = deps.length ? 1 + Math.max(...deps.map((x) => d(x, seen))) : 0;
      depth.set(id, v);
      return v;
    };
    const waves = [];
    tasks.forEach((t) => { (waves[d(t.id)] = waves[d(t.id)] || []).push(t.id); });
    return waves.filter(Boolean);
  }

  function applyPlan(st, ev) {
    const d = ev.data;
    for (const [id, t] of [...st.tasks]) if (t.removed) st.tasks.delete(id); // purge previously struck rows
    const incoming = new Set();
    for (const t of d.tasks) {
      incoming.add(t.id);
      const old = st.tasks.get(t.id);
      st.tasks.set(t.id, {
        id: t.id, title: t.title, role: t.role, goal: t.goal, dependsOn: t.depends_on || [],
        owns: t.owns || [], acceptance: t.acceptance || [],
        status: old ? old.status : 'pending', removed: false,
      });
    }
    for (const t of st.tasks.values()) if (!incoming.has(t.id)) t.removed = true;
    st.plan.version = d.version;
    st.plan.stages = d.stages && d.stages.length ? d.stages : computeStages(d.tasks);
    st.plan.reason = ev.type === 'plan.updated' ? d.reason : '';
    st.plan.diff = ev.type === 'plan.updated'
      ? { added: d.added || [], removed: d.removed || [], changed: d.changed || [] } : null;
    return [{ kind: 'plan', updated: ev.type === 'plan.updated', diff: st.plan.diff, reason: st.plan.reason }];
  }

  function applyEvent(st, ev) {
    const d = ev.data || {};
    const ms = Date.parse(ev.ts);
    st.lastSeq = ev.seq;
    if (!Number.isNaN(ms)) st.lastMs = ms;
    const fx = [];
    switch (ev.type) {
      case 'run.started':
        Object.assign(st.run, { id: ev.run_id, status: 'running', prompt: d.prompt, projectPath: d.project_path,
          mock: !!d.mock, model: d.model || '', startedMs: ms, finishedMs: null, paused: false, pausing: false,
          resumed: !!d.resumed, settings: d.settings || null });
        pushChat(st, ORCH, 'user', d.prompt, ev.seq);
        fx.push({ kind: 'run-start' });
        break;
      case 'run.finished':
        Object.assign(st.run, { status: d.status, finishedMs: ms, summary: d.summary || '', pausing: false });
        st.approval = null; st.attention = null;
        fx.push({ kind: 'run-end', status: d.status });
        break;
      case 'run.paused': st.run.paused = true; st.run.pausing = false; fx.push({ kind: 'run-paused' }); break;
      case 'run.resumed': st.run.paused = false; st.run.pausing = false; fx.push({ kind: 'run-resumed' }); break;
      case 'user.command':
        if (d.command === 'pause') st.run.pausing = true;
        if (d.command === 'resume') st.run.pausing = false;
        break;
      case 'orchestrator.status': st.orchStatus = d.text; fx.push({ kind: 'orch-status' }); break;
      case 'plan.created':
      case 'plan.updated': fx.push(...applyPlan(st, ev)); break;
      case 'agent.created':
        st.agents.set(ev.agent_id, { id: ev.agent_id, role: d.role, model: d.model, title: d.title,
          taskIds: d.task_ids || [], status: 'queued', currentFile: null, files: new Set() });
        fx.push({ kind: 'agent-created', agent: ev.agent_id });
        break;
      case 'agent.updated': {
        const a = st.agents.get(ev.agent_id);
        if (a) { a.model = d.model; fx.push({ kind: 'agent-updated', agent: a.id }); }
        break;
      }
      case 'log':
        st.logs.push({ seq: ev.seq, ts: ev.ts, level: d.level, source: d.source, message: d.message, detail: d.detail || '',
          task: ev.task_id || null, agent: ev.agent_id || null });
        if (st.logCounts[d.level] !== undefined) st.logCounts[d.level] += 1;
        fx.push({ kind: 'log', level: d.level });
        break;
      case 'approval.requested':
        st.approval = { agents: { ...d.agents }, models: d.models || [], sizes: d.sizes || {} };
        fx.push({ kind: 'approval' });
        break;
      case 'approval.granted': st.approval = null; fx.push({ kind: 'approved' }); break;
      case 'attention.required':
        st.attention = { taskId: ev.task_id, title: d.title, summary: d.summary, diagnosis: d.diagnosis || '',
          reasons: d.reasons || [], suggestions: d.suggestions || [], options: d.options || ['retry', 'skip', 'stop'] };
        fx.push({ kind: 'attention', task: ev.task_id });
        break;
      case 'attention.resolved': st.attention = null; break;
      case 'agent.removed':
        st.agents.delete(ev.agent_id);
        fx.push({ kind: 'agent-removed', agent: ev.agent_id });
        break;
      case 'agent.status': {
        const a = st.agents.get(ev.agent_id);
        if (a) { a.status = d.status; fx.push({ kind: 'agent-status', agent: a.id, status: d.status }); }
        break;
      }
      case 'agent.message':
        pushChat(st, ev.agent_id, d.role, d.text, ev.seq);
        fx.push({ kind: 'message', agent: ev.agent_id, role: d.role });
        break;
      case 'task.status': {
        const t = st.tasks.get(ev.task_id);
        if (t) {
          t.status = d.status;
          const a = agentOfTask(st, t.id);
          if (!d.carried) {     // carried over from a previous run: no hand-out animations for old work
            if (d.status === 'running' && a) fx.push({ kind: 'dispatch', agent: a.id });
            if (d.status === 'done' && a) fx.push({ kind: 'report', agent: a.id });
            if (d.status === 'verified' && a) fx.push({ kind: 'verified', agent: a.id });
          }
          fx.push({ kind: 'task-status', task: t.id, status: d.status });
        }
        break;
      }
      case 'file.created': {
        const prev = st.files.get(d.path);
        const f = prev && !d.rewrite ? prev : { path: d.path, agent: ev.agent_id, content: '', status: 'writing', size: 0 };
        f.agent = ev.agent_id; f.status = 'writing'; if (d.rewrite) f.content = '';
        st.files.set(d.path, f);
        const a = st.agents.get(ev.agent_id);
        if (a) { a.currentFile = d.path; a.files.add(d.path); }
        fx.push({ kind: 'file-created', path: d.path, rewrite: !!d.rewrite });
        break;
      }
      case 'file.chunk': {
        const f = st.files.get(d.path);
        if (f) { f.content += d.text; fx.push({ kind: 'file-chunk', path: d.path }); }
        break;
      }
      case 'file.saved': {
        const f = st.files.get(d.path);
        if (f) { f.status = 'saved'; f.size = d.size; }
        const a = st.agents.get(ev.agent_id);
        if (a && a.currentFile === d.path) a.currentFile = null;
        fx.push({ kind: 'file-saved', path: d.path });
        break;
      }
      case 'file.deleted': {
        st.files.delete(d.path);
        const a = st.agents.get(ev.agent_id);
        if (a) { a.files.delete(d.path); if (a.currentFile === d.path) a.currentFile = null; }
        fx.push({ kind: 'file-deleted', path: d.path });
        break;
      }
      case 'decision.recorded':
        st.decisions.push({ id: d.id, text: d.text, source: d.source, seq: ev.seq });
        fx.push({ kind: 'decision' });
        break;
      case 'usage.updated': st.usage = { in: d.tokens_in, out: d.tokens_out }; break;
      case 'error':
        st.errors.push({ message: d.message, agent: ev.agent_id || null, seq: ev.seq });
        fx.push({ kind: 'error', agent: ev.agent_id || null, task: ev.task_id || null, message: d.message });
        break;
      default: break; // unknown future event types are ignored, never fatal
    }
    return fx;
  }

  /** What the card should show: derives "verified" and "halted" from the raw statuses. */
  function agentDisplayStatus(st, a) {
    const ended = ['stopped', 'failed'].includes(st.run.status);
    if (ended && (a.status === 'running' || a.status === 'queued' || a.status === 'paused')) return 'halted';
    if (a.status === 'done') {
      const mine = a.taskIds.map((id) => st.tasks.get(id)).filter(Boolean);
      if (mine.length && mine.every((t) => t.status === 'verified')) return 'verified';
    }
    return a.status;
  }

  function agentTask(st, a) {
    const mine = a.taskIds.map((id) => st.tasks.get(id)).filter(Boolean);
    return mine.find((t) => t.status !== 'verified') || mine[mine.length - 1] || null;
  }

  /** Plan rows grouped into waves; a wave of 2+ tasks is "parallel". Removed tasks trail at the end. */
  function planGroups(st) {
    const placed = new Set();
    const groups = [];
    st.plan.stages.forEach((ids, i) => {
      const live = ids.filter((id) => st.tasks.has(id));
      live.forEach((id) => placed.add(id));
      if (live.length) groups.push({ wave: i + 1, ids: live, parallel: live.length > 1 });
    });
    const struck = [...st.tasks.values()].filter((t) => t.removed && !placed.has(t.id)).map((t) => t.id);
    if (struck.length) groups.push({ wave: 0, ids: struck, parallel: false, removed: true });
    return groups;
  }

  /** Nested tree: dirs first, then files, each alphabetical. */
  function fileTree(st) {
    const rootNode = { name: '', path: '', dirs: new Map(), files: [] };
    for (const f of st.files.values()) {
      const parts = f.path.split('/').filter(Boolean);
      let node = rootNode;
      parts.slice(0, -1).forEach((p, i) => {
        if (!node.dirs.has(p)) node.dirs.set(p, { name: p, path: parts.slice(0, i + 1).join('/'), dirs: new Map(), files: [] });
        node = node.dirs.get(p);
      });
      node.files.push({ name: parts[parts.length - 1], path: f.path });
    }
    const finish = (n) => ({
      name: n.name, path: n.path,
      dirs: [...n.dirs.values()].sort((a, b) => a.name.localeCompare(b.name)).map(finish),
      files: n.files.sort((a, b) => a.name.localeCompare(b.name)),
    });
    return finish(rootNode);
  }

  /** Strength the engine would pick for a model of this size (mirrors profiles.detect). */
  function strengthOf(parameterSize, name) {
    let m = /^\s*([\d.]+)\s*([KMBT])/i.exec(parameterSize || '');
    let b = m ? parseFloat(m[1]) * ({ K: 1e-6, M: 1e-3, B: 1, T: 1e3 })[m[2].toUpperCase()] : null;
    if (b === null) { m = /[:\-_](\d+(?:\.\d+)?)b\b/i.exec(name || ''); b = m ? parseFloat(m[1]) : null; }
    if (b === null) return 'medium';
    return b < 14 ? 'small' : b < 40 ? 'medium' : 'large';
  }

  return { ORCH, createState, applyEvent, agentOfTask, agentDisplayStatus, agentTask, planGroups, fileTree, computeStages, strengthOf };
});
