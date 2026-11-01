// Run: node --test tests/ui_state.test.mjs   (uses the same mock run the UI replays)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const MC = require('../src/mission_control/ui/state.js');

const events = readFileSync(new URL('../sample/mock_run.jsonl', import.meta.url), 'utf8')
  .split('\n').filter(Boolean).map((l) => JSON.parse(l));

function replay(upTo = Infinity) {
  const st = MC.createState();
  const fx = [];
  for (const ev of events) { if (ev.seq > upTo) break; fx.push(...MC.applyEvent(st, ev)); }
  return { st, fx };
}

test('final state: everything finished and verified', () => {
  const { st } = replay();
  assert.equal(st.run.status, 'completed');
  assert.equal(st.run.mock, true);
  assert.equal(st.agents.size, 6);
  assert.deepEqual([...st.tasks.values()].map((t) => t.status), Array(6).fill('verified'));
  for (const a of st.agents.values()) assert.equal(MC.agentDisplayStatus(st, a), 'verified', a.id);
  assert.equal(st.plan.version, 2);
});

test('file contents are reassembled from chunks and rewrites replace content', () => {
  const { st } = replay();
  const css = st.files.get('frontend/style.css');
  assert.match(css.content, /prefers-color-scheme: dark/);       // the rewritten (dark-mode) version
  assert.doesNotMatch(css.content, /^body \{ font-family/);       // not the first version
  assert.equal(Buffer.byteLength(css.content, 'utf8'), css.size); // chunks add up to the saved size
  for (const f of st.files.values()) {
    assert.equal(f.status, 'saved');
    assert.equal(Buffer.byteLength(f.content, 'utf8'), f.size, f.path);
  }
  assert.equal(st.files.size, 11);  // 11 unique paths (rewrites are not new files)
});

test('plan update: new task + changed acceptance, reason kept, old tasks keep their status', () => {
  const upd = events.find((e) => e.type === 'plan.updated');
  const before = replay(upd.seq - 1).st;
  const t1Status = before.tasks.get('t1').status;
  const { st, fx } = replay(upd.seq);
  assert.equal(st.tasks.size, 6);
  assert.equal(st.tasks.get('t1').status, t1Status);
  const planFx = fx.filter((f) => f.kind === 'plan' && f.updated)[0];
  assert.deepEqual(planFx.diff.added, ['t6']);
  assert.deepEqual(planFx.diff.changed, ['t3']);
  assert.match(st.plan.reason, /dark mode/);
  assert.equal(st.tasks.get('t3').acceptance.length, 3);
});

test('removed tasks are struck, then purged by the next plan event', () => {
  const st = MC.createState();
  const mk = (type, ids, extra = {}) => ({ seq: ++st.lastSeq, ts: '2026-01-01T00:00:00Z', type, run_id: 'r',
    data: { version: 1, tasks: ids.map((id) => ({ id, title: id, role: 'r', goal: 'g', depends_on: [], owns: [], acceptance: ['a'] })), ...extra } });
  MC.applyEvent(st, mk('plan.created', ['a', 'b']));
  MC.applyEvent(st, mk('plan.updated', ['a'], { reason: 'x', added: [], removed: ['b'], changed: [] }));
  assert.equal(st.tasks.get('b').removed, true);
  assert.equal(MC.planGroups(st).at(-1).removed, true);
  MC.applyEvent(st, mk('plan.updated', ['a'], { reason: 'y' }));
  assert.equal(st.tasks.has('b'), false);
});

test('parallel waves are detected in the plan groups', () => {
  const { st } = replay();
  const groups = MC.planGroups(st);
  assert.ok(groups.some((g) => g.parallel && g.ids.includes('t2') && g.ids.includes('t3')));
  assert.equal(groups[0].parallel, false);
});

test('computeStages works without server-provided stages', () => {
  const tasks = [{ id: 'a', depends_on: [] }, { id: 'b', depends_on: ['a'] }, { id: 'c', depends_on: ['a'] }, { id: 'd', depends_on: ['b', 'c'] }];
  assert.deepEqual(MC.computeStages(tasks), [['a'], ['b', 'c'], ['d']]);
});

test('soft pause: pausing flag set by the request, cleared only by run.paused', () => {
  const cmd = events.find((e) => e.type === 'user.command' && e.data.command === 'pause');
  const paused = events.find((e) => e.type === 'run.paused');
  const resumed = events.find((e) => e.type === 'run.resumed');
  assert.deepEqual([replay(cmd.seq).st.run.pausing, replay(cmd.seq).st.run.paused], [true, false]);
  assert.deepEqual([replay(paused.seq).st.run.pausing, replay(paused.seq).st.run.paused], [false, true]);
  assert.equal(replay(resumed.seq).st.run.paused, false);
});

test('effects: dispatch on running, report on done, verified on verified', () => {
  const { fx } = replay();
  const count = (k) => fx.filter((f) => f.kind === k).length;
  assert.equal(count('dispatch'), 7);  // 6 tasks + the QA retry
  assert.equal(count('report'), 6);
  assert.equal(count('verified'), 6);
  assert.equal(fx.filter((f) => f.kind === 'error').length, 1);
});

test('chat: orchestrator holds the prompt first; user message lands on the right agent', () => {
  const { st } = replay();
  assert.equal(st.chat.get('orchestrator')[0].role, 'user');
  const fe = st.chat.get('a_frontend');
  assert.ok(fe.some((m) => m.role === 'user' && /dark mode/.test(m.text)));
  assert.equal(fe.find((m) => m.role === 'user').seq < fe.filter((m) => m.role === 'agent').at(-1).seq, true);
});

test('halted display: when a run is stopped, running/queued agents show as halted', () => {
  const st = replay(30).st;
  const running = [...st.agents.values()].find((a) => a.status === 'running' || a.status === 'queued');
  assert.ok(running);
  st.run.status = 'stopped';
  assert.equal(MC.agentDisplayStatus(st, running), 'halted');
});

test('halted display also covers paused agents once the run is over', () => {
  const st = replay(30).st;
  const a = [...st.agents.values()][0];
  a.status = 'paused';
  assert.equal(MC.agentDisplayStatus(st, a), 'paused');       // run still going: a pause is just a pause
  st.run.status = 'stopped';
  assert.equal(MC.agentDisplayStatus(st, a), 'halted');       // run over: it will never resume
});

test('agent.removed deletes the agent and emits an effect; the model name is kept from run.started', () => {
  const st = MC.createState();
  let seq = 0;
  const ev = (type, data, extra = {}) => MC.applyEvent(st, { seq: ++seq, ts: '2026-01-01T00:00:00Z', run_id: 'r', type, data, ...extra });
  ev('run.started', { project_path: '/p', prompt: 'x', mock: false, model: 'ornith-1.5:9b' });
  assert.equal(st.run.model, 'ornith-1.5:9b');
  ev('plan.created', { version: 1, tasks: [{ id: 't1', title: 'a', role: 'r', goal: 'g', depends_on: [], owns: [], acceptance: ['x'] }] });
  ev('agent.created', { role: 'r', model: 'm', title: 'A', task_ids: ['t1'] }, { agent_id: 'a_t1' });
  assert.equal(st.agents.size, 1);
  const fx = ev('agent.removed', {}, { agent_id: 'a_t1' });
  assert.equal(st.agents.size, 0);
  assert.deepEqual(fx, [{ kind: 'agent-removed', agent: 'a_t1' }]);
});

test('logs, approval, attention, agent model changes and carried tasks', () => {
  const st = MC.createState();
  let seq = 0;
  const ev = (type, data, extra = {}) => MC.applyEvent(st, { seq: ++seq, ts: '2026-01-01T00:00:00Z', run_id: 'r', type, data, ...extra });
  ev('run.started', { project_path: '/p', prompt: 'x', mock: false, model: 'm', resumed: true, settings: { profile: 'small' } });
  assert.equal(st.run.resumed, true);
  assert.equal(st.run.settings.profile, 'small');
  ev('plan.created', { version: 1, tasks: [{ id: 't1', title: 'a', role: 'r', goal: 'g', depends_on: [], owns: [], acceptance: ['x'] }] });
  ev('agent.created', { role: 'r', model: 'm', title: 'A', task_ids: ['t1'] }, { agent_id: 'a_t1' });
  ev('approval.requested', { agents: { a_t1: 'm' }, models: ['m', 'big'], sizes: { big: '35B' } });
  assert.deepEqual(st.approval.models, ['m', 'big']);
  ev('agent.updated', { model: 'big' }, { agent_id: 'a_t1' });
  assert.equal(st.agents.get('a_t1').model, 'big');
  ev('approval.granted', { models: { a_t1: 'big' } });
  assert.equal(st.approval, null);
  ev('log', { level: 'error', source: 'task', message: 'boom', detail: 'evidence' }, { task_id: 't1', agent_id: 'a_t1' });
  ev('log', { level: 'debug', source: 'worker', message: 'call' });
  assert.deepEqual([st.logCounts.error, st.logCounts.debug, st.logs[0].detail, st.logs[0].task], [1, 1, 'evidence', 't1']);
  const fx = ev('task.status', { status: 'running', carried: true }, { task_id: 't1' });
  assert.equal(fx.filter((f) => f.kind === 'dispatch').length, 0);          // no hand-out animation for old work
  ev('attention.required', { title: 'a', summary: 's', reasons: ['r1'], suggestions: ['x'], options: ['retry', 'skip', 'stop'] }, { task_id: 't1' });
  assert.equal(st.attention.taskId, 't1');
  ev('attention.resolved', { decision: 'retry' }, { task_id: 't1' });
  assert.equal(st.attention, null);
  ev('attention.required', { title: 'a', summary: 's', reasons: [], suggestions: [] }, { task_id: 't1' });
  ev('run.finished', { status: 'stopped' });
  assert.equal(st.attention, null);                                        // a finished run never waits for anything
});

test('strengthOf mirrors the engine profiles', () => {
  assert.deepEqual(['9.2B', '35B', '70.6B', '500M'].map((x) => MC.strengthOf(x)), ['small', 'medium', 'large', 'small']);
  assert.equal(MC.strengthOf('', 'ornith-1.5:9b'), 'small');
  assert.equal(MC.strengthOf('', 'mystery'), 'medium');
});

test('tree: dirs first, alphabetical, nested', () => {
  const { st } = replay();
  const tree = MC.fileTree(st);
  assert.deepEqual(tree.dirs.map((d) => d.name), ['backend', 'contracts', 'docs', 'frontend', 'tests']);
  assert.deepEqual(tree.files.map((f) => f.name), ['README.md']);
  assert.deepEqual(tree.dirs[3].files.map((f) => f.name), ['app.js', 'index.html', 'robots.txt', 'sitemap.xml', 'style.css']);
});

test('usage is the last cumulative value; unknown events are ignored', () => {
  const { st } = replay();
  const lastUsage = events.filter((e) => e.type === 'usage.updated').at(-1).data;
  assert.deepEqual(st.usage, { in: lastUsage.tokens_in, out: lastUsage.tokens_out });
  assert.deepEqual(MC.applyEvent(st, { seq: 9999, ts: 'x', type: 'something.new', data: {} }), []);
});
