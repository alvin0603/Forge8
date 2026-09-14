"use strict";
// Dependency-free DOM/API fixture only. No browser, guest, model or project execution.
const fs = require("node:fs"), path = require("node:path"), vm = require("node:vm"), assert = require("node:assert/strict");
const root = path.resolve(__dirname, ".."), app = fs.readFileSync(path.join(root, "src/forge8/web/app.js"), "utf8");
const html = fs.readFileSync(path.join(root, "src/forge8/web/index.html"), "utf8");
class Node {
  static created = 0;
  constructor(tag = "") { Node.created++; this.tag = tag; this.children = []; this.parentElement = null; this.listeners = {}; this.value = ""; this.dataset = {}; this._text = ""; this.scrollTop = this.scrollLeft = 0; this.classList = {toggle() {}}; }
  set textContent(value) { this.replaceChildren(); this._text = String(value); }
  get textContent() { return this._text + this.children.map(node => node.textContent).join(""); }
  set innerHTML(_) { throw Error("HTML injection"); }
  append(...children) { for (const child of children) { child.remove(); child.parentElement = this; this.children.push(child); } }
  replaceChildren(...children) { for (const child of this.children) child.parentElement = null; this.children = []; this._text = ""; this.append(...children); }
  remove() { if (this.parentElement) { this.parentElement.children.splice(this.parentElement.children.indexOf(this), 1); this.parentElement = null; } }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  setAttribute(name, value) { (this.attributes ||= {})[name] = String(value); }
  querySelectorAll(query) { return this.children.flatMap(node => [...(node.tag === query ? [node] : []), ...node.querySelectorAll(query)]); }
  querySelector() { return null; }
  scrollIntoView() { throw Error("trace must not scroll the main source"); }
}
const clone = value => JSON.parse(JSON.stringify(value));
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return {promise, resolve}; };
const lines = ["def probe(items):", "    total = 0", "    for item in items:", "        if item:", "            total += item", "    return total", "", "# <img src=x onerror=alert(1)>", "# 原碼", "# end"];
const originalInput = '{"args":[[9007199254740993,0]],"kwargs":{}}';
function harness() {
  const nodes = Object.fromEntries([...html.matchAll(/\bid="([^"]+)"/g)].map(match => [match[1], new Node()]));
  const project = {name: "fixture", version: "v1", reader: "qwen35", experiments_enabled: true, files: [{id: "0", path: "probe.py", lines: lines.length}], excluded: []};
  const target = {file: "0", path: "probe.py", version: "v1", entry: "probe", source_sha256: "a".repeat(64), source_bytes: 160};
  const terminal = {...target, id: "trial1", status: "completed", input_text: originalInput, trace_lines: true, elapsed_seconds: 1,
    source_unchanged: true, runtime_unchanged: true, result_text: '{"return":9007199254740993}',
    reported_trace: {line_events: [2,3,4,5,3,4,3,6], truncated: false, hook_intact: true}};
  const requests = [], timers = new Map(); let timerId = 0;
  const fixture = {job: clone(terminal), source: null, lost: false, status: null}, response = data => ({ok: true, json: async () => clone(data)});
  const context = {URLSearchParams, AbortController, TextEncoder, location: {hash: "#token=fixture", pathname: "/", search: ""}, history: {replaceState() {}},
    sessionStorage: {setItem() {}, getItem: () => null},
    document: {getElementById: id => nodes[id], createElement: tag => new Node(tag), createDocumentFragment: () => new Node("fragment"), createTextNode: text => { const node = new Node("#text"); node.textContent = text; return node; }},
    setTimeout: (callback, ms) => { timers.set(++timerId, {callback, ms}); return timerId; }, clearTimeout: id => timers.delete(id),
    fetch: async (route, options) => {
      const body = options.body ? JSON.parse(options.body) : null; requests.push({route, method: options.method, body});
      assert.equal(options.headers.Authorization, "Bearer fixture"); assert.equal(options.credentials, "omit"); assert.equal(options.redirect, "error");
      if (route.startsWith("/api/source?")) {
        assert.equal(route, "/api/source?file=0&version=v1"); assert.equal(options.method, "GET");
        return fixture.source ? fixture.source(options) : response({version: "v1", path: "probe.py", lines});
      }
      if (route === "/api/experiment/current") return fixture.status ? fixture.status() : response(fixture.job);
      if (route === "/api/experiment/baseline/clear") {
        assert.deepEqual(body, {id: fixture.job.input_comparison.baseline.id, version: "v1", revision: fixture.job.input_comparison.revision});
        fixture.job.input_comparison = {...fixture.job.input_comparison, revision: fixture.job.input_comparison.revision + 1, baseline: null, reason: "no_baseline"};
        return response(fixture.job.input_comparison);
      }
      if (route === "/api/experiment/run") {
        if (fixture.reject) return {ok: false, status: 400, json: async () => ({error: "watch rejected"})};
        fixture.job = {...target, id: "trial2", status: "running", input_text: body.input_text, elapsed_seconds: 0,
          ...(body.trace_lines ? {trace_lines: true} : {}), ...(body.watch_names ? {watch_names: clone(body.watch_names)} : {})};
        if (fixture.lost) throw Error("lost POST response");
        return response({id: fixture.job.id});
      }
      throw Error("unexpected request: " + route);
    }};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(root, "src/forge8/web/reading-note.js"), "utf8"), context);
  vm.runInContext(app.slice(0, app.lastIndexOf("(async () => {")), context);
  const run = script => vm.runInContext(script, context), show = job => {
    context.fixtureJob = clone(job); context.fixtureTarget = clone(target); context.fixtureProject = clone(project); context.fixtureLines = [...lines];
    run(`state.project = fixtureProject; state.source = {file: fixtureProject.files[0], path: 'probe.py', version: 'v1', lines: fixtureLines};
      state.focus = [{file:'0',path:'probe.py',start:1,end:2}]; state.anchor = 1; state.end = 2;
      state.job = {id:'reading',status:'incomplete'}; state.history = [{id:'previous-reading',status:'incomplete'}];
      state.experiment = {target: fixtureJob, baseTarget: fixtureTarget, job: fixtureJob, input: fixtureJob.input_text, visible: true,
        preparing: false, submitting: false, unknown: false, checking: false, moduleDraft: [], traceDraft: fixtureJob.trace_lines === true,
        watchDraft: (fixtureJob.watch_names || []).join(', '), watchRevision: 0,
        traceRevision: 0, inputRevision: 0, prepareRequest: 0, statusRequest: 0, lastId: fixtureJob.id};
      $('question').value = 'UNSENT_QUESTION'; $('experiment-input').value = fixtureJob.input_text; $('answer').textContent = 'PRESERVED_ANSWER'; renderExperiment();`);
  };
  show(terminal);
  return {nodes, run, context, fixture, requests, timers, target, terminal, response, show};
}
const readState = h => h.run("JSON.stringify([state.source,state.focus,state.anchor,state.end,state.readingTrail,state.job,state.history,state.selectedHistory,$('question').value,$('answer').textContent])");
const click = (h, id) => h.nodes[id].listeners.click();

async function navigationAndBindings() {
  const h = harness(), before = readState(h);
  assert.equal(h.requests.length, 0, "rendering never fetches source or executes anything");
  assert.equal(h.nodes["experiment-trace-input-text"].textContent, originalInput);
  assert(h.nodes["experiment-trace-summary"].textContent.includes("依序保留重複"));
  assert.equal(h.nodes["experiment-trace-source"].hidden, true);
  await click(h, "experiment-trace-view");
  assert.equal(h.requests.length, 1);
  assert.equal(h.nodes["experiment-trace-source"].textContent, lines.slice(0, 5).map((line, index) => `${String(index + 1).padStart(4)}│ ${line}`).join("\n"));
  assert.equal(h.nodes["experiment-trace-source"].children.find(node => node.className === "experiment-trace-current").dataset.traceLine, 2);
  await click(h, "experiment-trace-next"); await click(h, "experiment-trace-next");
  assert(h.nodes["experiment-trace-position"].textContent.includes("第 3 / 8 次 → probe.py:L4"));
  h.nodes["experiment-trace-step"].value = "5"; await h.nodes["experiment-trace-step"].listeners.change();
  assert(h.nodes["experiment-trace-position"].textContent.includes("第 5 / 8 次 → probe.py:L3"));
  assert.equal(h.requests.length, 1, "later steps use only the checked retained source");
  assert(h.nodes["experiment-trace-source"].children.length <= 7);
  await click(h, "experiment-trace-prev");
  assert(h.nodes["experiment-trace-position"].textContent.includes("第 4 / 8 次 → probe.py:L5"));
  assert(h.nodes["experiment-trace-source"].textContent.includes("<img src=x"));
  assert.equal(h.nodes["experiment-trace-source"].querySelectorAll("img").length, 0);
  const sourceNode = h.nodes["experiment-trace-source"].children[0]; h.run("renderExperiment()");
  assert.equal(h.nodes["experiment-trace-source"].children[0], sourceNode, "poll renders do not rebuild the excerpt");
  assert.equal(readState(h), before);
  h.nodes["experiment-trace-step"].value = "0"; await click(h, "experiment-trace-view");
  assert(h.nodes["experiment-trace-note"].textContent.includes("整數次序")); assert.equal(h.requests.length, 1);
}

async function validationAndUnavailable() {
  for (const trace of [{line_events: [0], truncated: false, hook_intact: true}, {line_events: [11], truncated: false, hook_intact: true},
    {line_events: [true], truncated: false, hook_intact: true}, {line_events: [1.5], truncated: false, hook_intact: true},
    {line_events: ["2"], truncated: false, hook_intact: true}, {line_events: Array(1001).fill(2), truncated: true, hook_intact: true},
    {line_events: [2], truncated: 0, hook_intact: true}, {line_events: [2], truncated: true, hook_intact: true}, {line_events: [2], truncated: false, hook_intact: "yes"},
    {line_events: [2], truncated: false, hook_intact: true, locals: {secret: 42}}]) {
    const h = harness(); h.fixture.job.reported_trace = trace; await h.run("pollExperiment()");
    assert.equal(h.run("state.experiment.unknown"), true); assert.equal(h.run("state.experiment.job.reported_trace.line_events[0]"), 2);
    assert.equal(h.requests.filter(request => request.method === "POST").length, 0);
  }
  for (const change of [{reported_trace: null}, {status: "cancelled", reported_trace: undefined}, {source_unchanged: false}, {runtime_unchanged: false}, {status: "incomplete"}, {cleanup_unknown: true}]) {
    const h = harness(); h.show({...h.terminal, ...change});
    assert.equal(h.nodes["experiment-trace-controls"].hidden, true); assert.equal(h.nodes["experiment-trace-source"].hidden, true);
  }
  const h = harness(); h.show({...h.terminal, reported_trace: {line_events: [], truncated: false, hook_intact: false}});
  assert(h.nodes["experiment-trace-summary"].textContent.includes("不代表沒有執行程式"));
  assert(h.nodes["experiment-trace-summary"].textContent.includes("可能中斷"));
  const created = Node.created; h.show({...h.terminal, reported_trace: {line_events: Array(1000).fill(3), truncated: true, hook_intact: true}});
  assert(h.nodes["experiment-trace-summary"].textContent.includes("截斷"));
  assert(Node.created - created < 20, "1,000 visits use fixed controls, not a node per event");
  h.nodes["experiment-trace-step"].value = "1000"; await click(h, "experiment-trace-view");
  assert(h.nodes["experiment-trace-position"].textContent.includes("第 1000 / 1000")); assert.equal(h.nodes["experiment-trace-next"].disabled, true);
}

async function draftsOptionsAndRecovery() {
  const h = harness(), draft = '{"args":[[9007199254740995]],"kwargs":{}}';
  h.nodes["experiment-input"].value = draft; h.nodes["experiment-input"].listeners.input();
  h.nodes["experiment-trace-lines"].checked = false; h.nodes["experiment-trace-lines"].listeners.change();
  await h.run("pollExperiment()");
  assert.equal(h.nodes["experiment-input"].value, draft); assert.equal(h.nodes["experiment-trace-lines"].checked, false);
  assert.equal(h.nodes["experiment-trace-input-text"].textContent, originalInput); assert.equal(h.run("state.experiment.job.trace_lines"), true);
  assert.equal(h.nodes["experiment-input-state"].hidden, false);
  await click(h, "experiment-run");
  const ordinary = h.requests.find(request => request.route === "/api/experiment/run").body;
  assert.deepEqual(Object.keys(ordinary).sort(), ["allow_execution", "entry", "file", "input_text", "source_sha256", "version"]);
  assert.equal(ordinary.input_text, draft); assert.equal(h.nodes["experiment-trace"].hidden, true);
  assert.equal(h.nodes["experiment-result"].hidden, true, "a new running job must not inherit the previous input's result");
  const traced = harness(); traced.fixture.lost = true;
  await click(traced, "experiment-run");
  assert.equal(traced.requests.filter(request => request.method === "POST").length, 1, "lost execution response is never resubmitted");
  assert.equal(traced.requests[0].body.trace_lines, true); assert.equal(traced.run("state.experiment.job.id"), "trial2");
  assert.equal(traced.run("state.experiment.job.trace_lines"), true);
  // Simulated reload starts without a local job; the only recovery action is GET.
  const reload = harness(); reload.run("state.experiment.job = null; state.experiment.target = null; state.experiment.unknown = true; state.experiment.traceDraft = false");
  await reload.run("pollExperiment()");
  assert.equal(reload.nodes["experiment-trace-lines"].checked, true); assert.equal(reload.nodes["experiment-trace-input-text"].textContent, originalInput);
  assert.equal(reload.requests.length, 1); assert.equal(reload.requests[0].method, "GET");
  const unchanged = harness(); unchanged.fixture.job.trace_lines = false; unchanged.fixture.job.reported_trace = null; await unchanged.run("pollExperiment()");
  assert.equal(unchanged.run("state.experiment.unknown"), true, "same job may not change the submitted trace option");
  const changed = harness(); changed.fixture.job.reported_trace.line_events = [6]; await changed.run("pollExperiment()");
  assert.equal(changed.run("state.experiment.unknown"), true, "terminal trace cannot silently change");
  const module = harness(); module.run("state.experiment.moduleDraft = ['other']; experimentControls()");
  assert.equal(module.nodes["experiment-trace-option"].hidden, true); assert.equal(module.nodes["experiment-trace-lines"].disabled, true);
  module.context.invalid = {...module.terminal, module_set: {}}; assert.equal(module.run("validExperimentTrace(invalid, state.project)"), false);
  module.context.invalid = {...module.terminal, mode: "head_current"}; assert.equal(module.run("validExperimentTrace(invalid, state.project)"), false);
}

async function sourceRacesAndBounds() {
  const mutations = ["$('experiment-input').value = 'edited'; $('experiment-input').listeners.input()",
    "$('experiment-trace-lines').checked = false; $('experiment-trace-lines').listeners.change()",
    "$('question').value = 'NEW'; state.questionRevision++", "state.sourceRequest++", "state.anchor = 3", "state.focus = []",
    "state.selectedHistory = 'previous-reading'", "state.historyRequest++", "state.refreshing = true", "state.project = {...state.project}",
    "state.job = {id:'new-question',status:'running'}", "state.experiment.visible = false", "state.experiment.job = {...state.experiment.job,id:'new-trial'}",
    "state.project.model_policy = {mode:'resident'}; state.modelReleasePending = true"];
  for (const mutation of mutations) {
    const h = harness(), pending = deferred(); h.fixture.source = () => pending.promise;
    const reading = click(h, "experiment-trace-view"); h.run(mutation + "; controls()");
    pending.resolve(h.response({version: "v1", path: "probe.py", lines})); await reading;
    assert.equal(h.nodes["experiment-trace-source"].hidden, true, `stale response: ${mutation}`);
    assert.equal(h.requests.length, 1); assert.equal(h.requests[0].method, "GET");
  }
  for (const source of [{version: "wrong", path: "probe.py", lines}, {version: "v1", path: "wrong.py", lines},
    {version: "v1", path: "probe.py", lines: lines.slice(1)}, {version: "v1", path: "probe.py", lines: Array(10).fill("x".repeat(40000))}]) {
    const h = harness(); h.fixture.source = () => Promise.resolve(h.response(source)); await click(h, "experiment-trace-view");
    assert.equal(h.nodes["experiment-trace-source"].hidden, true); assert.equal(h.requests.length, 1);
  }
  const long = harness(); long.fixture.source = () => Promise.resolve(long.response({version: "v1", path: "probe.py", lines: lines.map((line, i) => i === 1 ? "x".repeat(4001) : line)}));
  await click(long, "experiment-trace-view"); long.run("renderExperiment()");
  assert.equal(long.nodes["experiment-trace-source"].hidden, true); assert(long.nodes["experiment-trace-note"].textContent.includes("未裁切"));
  const timeout = harness(); timeout.fixture.source = options => new Promise((_, reject) => options.signal.addEventListener("abort", () => { const error = Error("timeout"); error.name = "AbortError"; reject(error); }));
  const reading = click(timeout, "experiment-trace-view");
  const timer = [...timeout.timers.values()].find(timer => timer.ms === 10000); assert(timer); timer.callback(); await reading;
  assert(timeout.nodes["experiment-trace-note"].textContent.includes("10 秒")); assert.equal(timeout.nodes["experiment-trace-view"].disabled, false);
  assert.equal(timeout.requests.length, 1);
  const replaced = harness(), first = deferred(), second = deferred(); let calls = 0;
  replaced.fixture.source = () => (++calls === 1 ? first.promise : second.promise);
  const oldRead = click(replaced, "experiment-trace-view");
  replaced.nodes["experiment-input"].value = "new draft"; replaced.nodes["experiment-input"].listeners.input();
  const newRead = click(replaced, "experiment-trace-view");
  first.resolve(replaced.response({version: "v1", path: "probe.py", lines})); await oldRead;
  second.resolve(replaced.response({version: "v1", path: "probe.py", lines})); await newRead;
  assert.equal(replaced.nodes["experiment-trace-source"].hidden, false);
  assert.equal(replaced.nodes["experiment-trace-note"].textContent, "", "an obsolete read cannot overwrite a newer read's note");
  assert.equal(replaced.nodes["experiment-input"].value, "new draft");
}

function watchedJob(h) {
  const value = json => ({state: "value", json});
  return {...h.terminal, watch_names: ["total", "item"], reported_trace: {
    ...h.terminal.reported_trace, watch: {names: ["total", "item"], truncated: false, events: [
      {event: "line", line: 2, call_id: 1, values: {total: value("9007199254740993"), item: {state: "unbound"}}},
      {event: "line", line: 2, call_id: 2, values: {total: value("99"), item: value('"<img src=x onerror=alert(1)>"')}},
      {event: "return", line: 6, call_id: 2, values: {total: value("99"), item: {state: "unsupported"}}},
      {event: "line", line: 5, call_id: 1, values: {total: value("9007199254740995"), item: {state: "limited"}}},
      {event: "exception", line: 5, call_id: 1, values: {total: {state: "unsupported"}, item: {state: "unbound"}}},
      {event: "return", line: 6, call_id: 1, values: {total: value("9007199254740995"), item: {state: "unbound"}}}
    ]}}};
}
function watchDraft(h, value) {
  h.nodes["experiment-watch-names"].value = value;
  h.nodes["experiment-watch-names"].listeners.input();
}
async function watchedNavigationAndSchema() {
  const h = harness(), job = watchedJob(h); h.show(job);
  const before = readState(h), box = h.nodes["experiment-watch-values"];
  assert.equal(h.requests.length, 0);
  assert.equal(box.hidden, false); assert(box.textContent.includes("9007199254740993")); assert(box.textContent.includes("尚未綁定"));
  await click(h, "experiment-trace-view");
  await click(h, "experiment-trace-next");
  assert(box.textContent.includes("<img src=x")); assert.equal(box.querySelectorAll("img").length, 0);
  assert(box.children[0].textContent.includes("無可比較的前值"), "another invocation is not the preceding snapshot");
  await click(h, "experiment-trace-next"); assert(box.textContent.includes("此值型別不支援"));
  await click(h, "experiment-trace-next");
  assert(box.children[0].textContent.includes("前次快照：9007199254740993"));
  assert(box.children[0].textContent.includes("目前快照：9007199254740995"));
  assert(!box.children[0].textContent.includes("前次快照：99"));
  assert(box.textContent.includes("超過值的深度")); assert(h.nodes["experiment-trace-position"].textContent.includes("呼叫 #1"));
  const retained = box.children[0]; h.run("renderExperiment()"); assert.equal(box.children[0], retained);
  await click(h, "experiment-trace-next"); assert(h.nodes["experiment-trace-position"].textContent.includes("不代表未被捕捉"));
  await click(h, "experiment-trace-next"); assert(h.nodes["experiment-trace-position"].textContent.includes("不代表成功"));
  assert.equal(h.requests.length, 1); assert.equal(readState(h), before);
  const invalid = [
    j => j.watch_names = [], j => j.watch_names = ["total", "total"], j => j.watch_names = ["class"],
    j => j.trace_lines = false, j => j.generator_steps = 1, j => j.module_set = {}, j => j.mode = "head_current",
    j => j.reported_trace.watch.names.reverse(),
    j => j.reported_trace.watch.events[0].values = {item: {state: "unbound"}, total: {state: "value", json: "1"}},
    j => j.reported_trace.watch.events[0].values.total = {state: "value", json: 9007199254740992},
    j => j.reported_trace.watch.events[0].values.total = {state: "value", json: '"非 ASCII"'},
    j => j.reported_trace.watch.events[0].values.total = {state: "limited", json: "1"},
    j => j.reported_trace.watch.events[0].values.total = {state: "missing"},
    j => j.reported_trace.watch.events[0].values.total.json = "x".repeat(2049),
    j => j.reported_trace.watch.events[0].line = 11,
    j => j.reported_trace.watch.events[0].call_id = 2,
    j => j.reported_trace.watch.events[0].event = "call",
    j => j.reported_trace.watch.events = Array(129).fill(j.reported_trace.watch.events[0]),
    j => j.reported_trace.watch.truncated = 1,
    j => j.reported_trace.watch.extra = "unknown"
  ];
  for (const mutate of invalid) {
    const bad = clone(job); mutate(bad); h.context.badWatchJob = bad;
    assert.equal(h.run("validExperimentTrace(badWatchJob,state.project)"), false, String(mutate));
  }
  const unrequested = clone(job); delete unrequested.watch_names; h.context.badWatchJob = unrequested;
  assert.equal(h.run("validExperimentTrace(badWatchJob,state.project)"), false);
  for (const changes of [{status: "incomplete"}, {source_unchanged: false}, {runtime_unchanged: false}, {cleanup_unknown: true}, {reported_trace: null}]) {
    h.show({...job, ...changes}); assert.equal(box.hidden, true, JSON.stringify(changes));
  }
}
async function watchedDraftsAndRecovery() {
  const h = harness(), job = watchedJob(h); h.fixture.job = clone(job); h.show(job);
  watchDraft(h, "item"); await h.run("pollExperiment()");
  assert.equal(h.nodes["experiment-watch-names"].value, "item");
  assert(h.nodes["experiment-watch-submitted"].textContent.includes("total, item"));
  assert.equal(h.nodes["experiment-input-state"].hidden, false);
  await click(h, "experiment-run");
  const posted = h.requests.find(request => request.route === "/api/experiment/run").body;
  assert.deepEqual(posted.watch_names, ["item"]); assert.equal(posted.trace_lines, true);
  assert.equal(posted.input_text, originalInput);
  assert.equal(h.nodes["experiment-watch-values"].hidden, true, "new running job cannot retain old snapshots");
  const off = harness(); off.show(watchedJob(off)); watchDraft(off, "");
  await click(off, "experiment-run");
  assert.equal("watch_names" in off.requests.find(request => request.route === "/api/experiment/run").body, false);
  const lost = harness(); lost.fixture.job = watchedJob(lost); lost.show(lost.fixture.job); lost.fixture.lost = true;
  await click(lost, "experiment-run");
  assert.equal(lost.requests.filter(request => request.method === "POST").length, 1);
  assert.deepEqual(clone(lost.run("state.experiment.job.watch_names")), ["total", "item"]);
  for (const responseKind of ["previous", "idle", "wrong_names"]) {
    const uncertain = harness(); uncertain.show(watchedJob(uncertain)); uncertain.fixture.lost = true;
    const previous = clone(uncertain.run("state.experiment.job"));
    uncertain.fixture.status = () => uncertain.response(responseKind === "idle" ? {id: null, status: "idle"} :
      responseKind === "previous" ? previous : {...uncertain.fixture.job, watch_names: ["item"]});
    await click(uncertain, "experiment-run");
    assert.equal(uncertain.run("state.experiment.unknown"), true, responseKind);
    assert.equal(uncertain.requests.filter(request => request.method === "POST").length, 1);
  }
  for (const edit of [null, "item", ""]) {
    const reload = harness(); reload.fixture.job = watchedJob(reload);
    reload.run("state.experiment.job = null; state.experiment.target = null; state.experiment.unknown = true; state.experiment.watchDraft = ''; state.experiment.watchRevision = 0");
    if (edit !== null) { reload.run("state.experiment.watchDraft = 'item'; state.experiment.watchRevision = 1"); reload.run("state.experiment.watchDraft = " + JSON.stringify(edit) + "; state.experiment.watchRevision++"); }
    await reload.run("pollExperiment()");
    assert.equal(reload.nodes["experiment-watch-names"].value, edit === null ? "total, item" : edit);
    assert(reload.nodes["experiment-watch-submitted"].textContent.includes("total, item"));
    assert.equal(reload.requests.length, 1); assert.equal(reload.requests[0].method, "GET");
  }
  const late = harness(), statusReply = deferred(); late.fixture.status = () => statusReply.promise;
  const recovering = late.run("pollExperiment()");
  watchDraft(late, "item"); watchDraft(late, "");
  statusReply.resolve(late.response({...watchedJob(late), id: "recovered-watch"})); await recovering;
  assert.equal(late.nodes["experiment-watch-names"].value, "", "late new-job GET cannot overwrite an ABA-edited draft");
  assert(late.nodes["experiment-watch-submitted"].textContent.includes("total, item"));
  assert.equal(late.requests.length, 1); assert.equal(late.requests[0].method, "GET");
  const race = harness(), pending = deferred(); race.show(watchedJob(race)); race.fixture.source = () => pending.promise;
  const opening = click(race, "experiment-trace-view");
  watchDraft(race, "item"); watchDraft(race, "total, item");
  pending.resolve(race.response({version: "v1", path: "probe.py", lines})); await opening;
  assert.equal(race.nodes["experiment-trace-source"].hidden, true, "ABA-edited watch draft invalidates delayed source GET");
  assert.equal(race.requests.length, 1);
  const module = harness(); module.show(watchedJob(module));
  module.run("state.experiment.moduleDraft = ['other']; experimentControls()");
  assert.equal(module.nodes["experiment-run"].disabled, true);
  await click(module, "experiment-run"); assert.equal(module.requests.length, 0);
  const rejected = harness(); rejected.fixture.job = watchedJob(rejected); rejected.show(rejected.fixture.job); rejected.fixture.reject = true;
  await click(rejected, "experiment-run");
  assert(rejected.nodes["experiment-status"].textContent.includes("watch rejected"));
  assert(rejected.nodes["experiment-status"].textContent.includes("未重送"));
  assert.equal(rejected.run("state.experiment.unknown"), false);
  assert.equal(rejected.requests.filter(request => request.method === "POST").length, 1);
}
async function watchedBaseline() {
  const h = harness(), job = watchedJob(h);
  const baseline = {...h.target, id: "ordinary-a", input_text: originalInput, result_text: '{"return":9007199254740993}',
    input_sha256: "b".repeat(64), runtime_sha256: "c".repeat(64), report_sha256: "d".repeat(64), trace_lines: false};
  job.input_comparison = {revision: 1, baseline, current_id: job.id, can_pin: false, settling: false,
    outcome: "unavailable", reason: "current_unavailable", current_report_sha256: null};
  h.fixture.job = clone(job); h.show(job); await h.run("pollExperiment()");
  assert.equal(h.nodes["experiment-baseline-a"].hidden, false); assert.equal(h.nodes["experiment-baseline-clear"].disabled, false);
  assert.equal(h.nodes["experiment-baseline-pin"].disabled, true);
  assert.equal(h.nodes["experiment-baseline-a-result"].textContent, baseline.result_text);
  watchDraft(h, ""); h.run("renderExperiment()");
  assert.equal(h.nodes["experiment-baseline-pin"].disabled, true, "changing the next draft cannot make a watched result pinnable");
  await click(h, "experiment-baseline-pin"); assert.equal(h.requests.filter(request => request.method === "POST").length, 0);
  for (const change of [{can_pin: true}, {current_report_sha256: "d".repeat(64)}, {outcome: "same", reason: null}]) {
    h.context.badWatchJob = {...job, input_comparison: {...job.input_comparison, ...change}};
    assert.equal(h.run("validExperimentInputComparison(badWatchJob,state.project)"), false);
  }
  await click(h, "experiment-baseline-clear");
  assert.equal(h.nodes["experiment-baseline-a"].hidden, true);
  assert.equal(h.requests.filter(request => request.route === "/api/experiment/run").length, 0);
  assert.equal(h.requests.filter(request => request.method === "POST").length, 1);
  const ordinary = harness(), ordinaryJob = {...ordinary.terminal, input_comparison: {...job.input_comparison, baseline: null,
    reason: "no_baseline", can_pin: true, current_report_sha256: "d".repeat(64)}};
  ordinary.fixture.job = ordinaryJob; ordinary.show(ordinaryJob); await ordinary.run("pollExperiment()");
  assert.equal(ordinary.nodes["experiment-baseline-pin"].disabled, false);
  watchDraft(ordinary, "total"); ordinary.run("renderExperiment()");
  assert.equal(ordinary.nodes["experiment-baseline-pin"].disabled, true, "render cannot re-enable pin while next draft requests locals");
}


(async () => {
  assert.match(html, /id="experiment-trace-lines"[^>]*type="checkbox"/);
  for (const id of ["experiment-trace-prev", "experiment-trace-next", "experiment-trace-view"]) assert.match(html, new RegExp(`id="${id}"[^>]*type="button"`));
  await navigationAndBindings(); await validationAndUnavailable(); await draftsOptionsAndRecovery(); await sourceRacesAndBounds();
  await watchedNavigationAndSchema(); await watchedDraftsAndRecovery(); await watchedBaseline();
  console.log("isolated trial line-visit UI fixtures passed (no guest execution)");
})().catch(error => { console.error(error); process.exitCode = 1; });
