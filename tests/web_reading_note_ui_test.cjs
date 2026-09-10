"use strict";
// Owned ordinary-result/DOM fixtures only. Never execute project source or inference.
const fs = require("node:fs"), path = require("node:path"), vm = require("node:vm"), assert = require("node:assert/strict");
const root = path.resolve(__dirname, ".."), app = fs.readFileSync(path.join(root, "src/forge8/web/app.js"), "utf8");
const html = fs.readFileSync(path.join(root, "src/forge8/web/index.html"), "utf8");
const clone = value => JSON.parse(JSON.stringify(value)), SHA = "a".repeat(64);
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return {promise, resolve}; };
class Node {
  constructor(tag = "", clicked = () => {}) { this.tag = tag; this.clicked = clicked; this.children = []; this.parentElement = null; this.listeners = {}; this.value = ""; this.dataset = {}; this._text = ""; this.scrollTop = this.scrollLeft = 0; this.classList = {toggle() {}}; }
  set textContent(value) { this.replaceChildren(); this._text = String(value); }
  get textContent() { return this._text + this.children.map(node => node.textContent).join(""); }
  set innerHTML(_) { throw Error("HTML injection"); }
  append(...children) { for (const child of children.flatMap(node => node.tag === "fragment" ? [...node.children] : [node])) { child.remove(); child.parentElement = this; this.children.push(child); } }
  replaceChildren(...children) { for (const child of this.children) child.parentElement = null; this.children = []; this._text = ""; this.append(...children); }
  remove() { if (this.parentElement) { this.parentElement.children.splice(this.parentElement.children.indexOf(this), 1); this.parentElement = null; } }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  setAttribute(name, value) { (this.attributes ||= {})[name] = String(value); }
  querySelectorAll(query) { return this.children.flatMap(node => [...(node.tag === query || query === "[data-line]" && node.dataset.line ? [node] : []), ...node.querySelectorAll(query)]); }
  querySelector(query) { const line = query.match(/data-line="(\d+)"/)?.[1]; return this.querySelectorAll("[data-line]").find(node => String(node.dataset.line) === line); }
  scrollIntoView() {}
  click() { this.clicked(this); }
}
function fixture() {
  const files = [{id: "0", path: "app.py", lines: 4}, {id: "1", path: "helper.py", lines: 2}];
  const job = {id: "history-a", kind: "explain", reader: "qwen35", question: "QUESTION_A\n  Preserve spacing.\n", version: SHA,
    status: "answered", gpu: "released", elapsed_seconds: 1, files,
    reading_scope: {origin: "user_focus", focus: ["app.py:1-2", "app.py:3-4", "helper.py:1-1"], supplements: []},
    result: {kind: "forge8.explain", status: "answered", ok: true, semantic_claims_verified: false, repository_code_executed: false, source_write_attempted: false,
      outcome: {status: "answered", ok: true, source_unchanged: true, snapshot_unchanged: true, acceptance: {ok: true},
        answer: {format: "cited_prose", citation_scope: "document_references_only", claims: [{type: "inference", text: "ANSWER_A [E1:L1-L2]\n```\n<img src=remote>\n```", citations: [
          {evidence_id: "E1", path: "app.py", start_line: 1, end_line: 2, file_sha256: "b".repeat(64), file_size_bytes: 100, snapshot_inventory_sha256: SHA, artifact_sha256: "c".repeat(64)}]}]},
        coverage: {admitted: {files: 2, lines: 6}, observed: {files: 2, lines: 5, ranges: [{path: "app.py", ranges: [{start_line: 1, end_line: 4}]}, {path: "helper.py", ranges: [{start_line: 1, end_line: 1}]}]},
          cited: {files: 1, lines: 2, ranges: [{path: "app.py", ranges: [{start_line: 1, end_line: 2}]}]}, unread: {files: 1, lines: 1}, excluded: {entries: 0}}}}};
  job.result.question = job.result.outcome.question = job.question;
  job.result.private_path = "PRIVATE_REPORT_PATH";
  return {job, project: {name: "owned fixture", version: SHA, reader: "qwen35", files: clone(files), excluded: [], model_policy: {mode: "resident"}},
    sources: [{version: SHA, path: "app.py", lines: ["def first(value):", "    return value", "def second():", "    return '<img src=remote>'"]},
      {version: SHA, path: "helper.py", lines: ["UNCITED_SELECTED_SOURCE = 1", "UNREAD_SOURCE = 2"]}]};
}
function resident(f) {
  const checkpoint = {schema_version: 1, scope: "resident_request", server_session_id: "past-session-a", request_completed: true,
    slot_idle: true, transport_secret_cleared: true, session_finalization: "pending"};
  f.job.gpu = "resident"; f.job.result.kind = "forge8.explain.request";
  f.job.request_completion = clone(checkpoint); f.job.result.request_completion = clone(checkpoint);
  f.job.result.server = {scope: "resident_request", server_session_id: checkpoint.server_session_id, request_completion: clone(checkpoint)};
  f.job.result.outcome.request_completion = clone(checkpoint); f.job.result.outcome.acceptance.evidence = clone(checkpoint);
  return f;
}
function projectFixture() {
  const f = fixture(), focus = [{path: "app.py", start_line: 1, end_line: 2}, {path: "app.py", start_line: 3, end_line: 3},
    {path: "app.py", start_line: 4, end_line: 4}, {path: "helper.py", start_line: 1, end_line: 1}];
  f.job.kind = "project";
  f.job.reading_scope = {origin: "project_candidates", focus: focus.map(span => `${span.path}:${span.start_line}-${span.end_line}`), supplements: ["helper.py:1-1"]};
  f.job.result.project_reading = {answer_attempted: true, focus, context: {added: [focus[3]], skipped: {}}, discovery: {ok: true, status: "located", acceptance: {ok: true},
    source_unchanged: true, snapshot_unchanged: true, ingress_unchanged: true, snapshot_sha256: SHA, candidates: [{name: "first", path: "app.py", start_line: 1, end_line: 2}]}};
  return f;
}
function changesFixture() {
  const f = fixture(), files = [{id: "0", path: "before/app.py", lines: 2}, {id: "1", path: "after/app.py", lines: 2}, {id: "2", path: "after/caller.py", lines: 1}];
  f.job.kind = "changes"; f.job.files = files; delete f.job.reading_scope;
  f.job.comparison = {head: "d".repeat(40), original_snapshot_sha256: "e".repeat(64), caller_held_fixed: true, ranges: files.map(file => `${file.path}:1-${file.lines}`)};
  f.project.files = clone(files); f.project.comparison = {...clone(f.job.comparison), catalogue: {files: [], detail_limited: false}};
  const out = f.job.result.outcome; f.job.result.question = out.question = "EXPANDED_COMPARISON_QUESTION";
  out.answer.claims[0].citations[0].path = files[0].path;
  out.coverage.admitted = {files: 3, lines: 5}; out.coverage.observed = {files: 3, lines: 5, ranges: files.map(file => ({path: file.path, ranges: [{start_line: 1, end_line: file.lines}]}))};
  out.coverage.cited.ranges[0].path = files[0].path; out.coverage.unread = {files: 0, lines: 0};
  f.sources = files.map(file => ({version: SHA, path: file.path, lines: Array(file.lines).fill(`RETAINED ${file.path}`)}));
  return f;
}
function harness(f = fixture(), current = false) {
  const nodes = Object.fromEntries([...html.matchAll(/\bid="([^"]+)"/g)].map(match => [match[1], new Node()]));
  const requests = [], timers = new Map(), downloads = [], blobs = [], revoked = [], urls = new Map(), storage = [];
  let timerId = 0;
  const options = {source: null, history: null, refresh: null, clickFailure: false}, response = value => ({ok: true, json: async () => clone(value)});
  const clicked = node => { assert.equal(node.tag, "a"); assert.equal(node.parentElement, body); assert.equal(node.hidden, true);
    assert(urls.has(node.href)); if (options.clickFailure) throw Error("owned browser download refusal");
    downloads.push({name: node.download, url: node.href, text: urls.get(node.href).text, type: urls.get(node.href).type}); };
  const body = new Node("body"), context = {URLSearchParams, AbortController, TextEncoder,
    location: {hash: "#token=fixture", pathname: "/", search: ""}, history: {replaceState() {}},
    sessionStorage: {setItem: (...args) => storage.push(args), getItem: () => null}, localStorage: {setItem() { throw Error("new persistence"); }},
    document: {body, getElementById: id => nodes[id], createElement: tag => new Node(tag, clicked), createDocumentFragment: () => new Node("fragment"),
      createTextNode: text => { const node = new Node("#text"); node.textContent = text; return node; }},
    Blob: class { constructor(parts, init) { assert(parts.every(part => typeof part === "string")); this.text = parts.join(""); this.type = init.type; blobs.push(this); } },
    URL: {createObjectURL: blob => { const url = `blob:owned-${blobs.length}`; urls.set(url, blob); return url; }, revokeObjectURL: url => { revoked.push(url); urls.delete(url); }},
    setTimeout: (callback, ms) => { timers.set(++timerId, {callback, ms}); return timerId; }, clearTimeout: id => timers.delete(id),
    fetch: async (route, init) => {
      requests.push({route, method: init.method, signal: init.signal});
      assert.equal(init.headers.Authorization, "Bearer fixture"); assert.equal(init.credentials, "omit"); assert.equal(init.redirect, "error"); assert.equal(init.cache, "no-store");
      if (route === "/api/refresh" && options.refresh) { assert.equal(init.method, "POST"); return options.refresh(init); }
      assert.equal(init.method, "GET", "note export must not submit questions, cancel jobs, or start experiments"); assert.equal(init.body, undefined);
      if (route.startsWith("/api/source?")) {
        const query = new URLSearchParams(route.split("?")[1]), file = f.project.files.find(file => file.id === query.get("file"));
        assert.equal(query.get("version"), SHA); assert(file);
        return options.source ? options.source(file, init) : response(f.sources.find(source => source.path === file.path));
      }
      if (route === "/api/history" && options.history) return response(options.history);
      throw Error("unexpected request: " + route);
    }};
  vm.createContext(context); vm.runInContext(fs.readFileSync(path.join(root, "src/forge8/web/reading-note.js"), "utf8"), context);
  vm.runInContext(app.slice(0, app.lastIndexOf("(async () => {")), context);
  const run = script => vm.runInContext(script, context);
  Object.assign(context, {fixtureProject: f.project, fixtureJob: f.job, fixtureSource: f.sources.at(-1), currentFixture: current});
  run(`state.project=fixtureProject; state.history=[fixtureJob];
    state.job=currentFixture ? fixtureJob : {id:'current-b',kind:'explain',status:'running',question:'CURRENT_QUESTION_B',version:fixtureProject.version,elapsed_seconds:1};
    state.selectedHistory=currentFixture ? null : fixtureJob.id; state.modelUnknown=true;
    state.source={...fixtureSource,file:fixtureProject.files.at(-1),outline:{status:'available',items:[]}};
    state.focus=[{file:state.source.file.id,path:state.source.path,start:1,end:1}]; state.anchor=1; state.end=1;
    state.experiment={target:null,job:{id:'old-trial',status:'completed'},input:'TRIAL_DRAFT_C',visible:false,prepareRequest:0,statusRequest:0};
    $('question').value='UNSENT_DRAFT_C'; $('experiment-input').value='TRIAL_DRAFT_C'; $('answer').textContent='PRESERVED_ANSWER_VIEW';
    controls();`);
  const fireTimer = ms => { const entry = [...timers].find(([, timer]) => timer.ms === ms); assert(entry, `timer ${ms} exists`); timers.delete(entry[0]); entry[1].callback(); };
  return {f, nodes, context, run, requests, timers, downloads, blobs, revoked, urls, storage, body, options, response, fireTimer};
}
const preserved = h => h.run("JSON.stringify([state.project,state.source,state.focus,state.anchor,state.end,state.job,state.history,state.selectedHistory,state.model,state.modelUnknown,state.experiment,state.continuation,$('question').value,$('experiment-input').value,$('answer').textContent])");
const exportNote = h => h.nodes["export-reading-note"].listeners.click();
function holdSources(h) {
  const wait = deferred(), signals = [];
  h.options.source = async (file, init) => { signals.push(init.signal); await wait.promise; return h.response(h.f.sources.find(source => source.path === file.path)); };
  return {resolve: wait.resolve, signals};
}
function assertNoDownload(h) { assert.equal(h.downloads.length, 0); assert.equal(h.blobs.length, 0); assert.equal(h.urls.size, 0); }

async function historyCurrentDraftIsolation() {
  const h = harness(resident(fixture())), before = preserved(h), initialStorage = clone(h.storage), held = holdSources(h);
  assert.equal(h.nodes["export-reading-note"].disabled, false, "historical checkpoint is independent of unknown live model and current running B");
  assert.equal(h.nodes.cancel.disabled, false); const work = exportNote(h);
  assert.equal(h.requests.length, 2, "three ranges in two files cause two GETs"); assertNoDownload(h);
  assert.equal(h.nodes["export-reading-note"].disabled, true); await exportNote(h); assert.equal(h.requests.length, 2);
  held.resolve(); await work;
  assert.equal(preserved(h), before); assert.deepEqual(h.storage, initialStorage); assert.equal(h.body.children.length, 0);
  assert.equal(h.downloads.length, 1); const note = h.downloads[0];
  assert.equal(note.name, "forge8-reading-note.md"); assert.equal(note.type, "text/markdown;charset=utf-8");
  for (const expected of [h.f.job.question, h.f.job.result.outcome.answer.claims[0].text, "UNCITED_SELECTED_SOURCE", "session cleanup was pending", "past-session-a", "unavailable: selected file was not cited"]) assert(note.text.includes(expected));
  for (const absent of ["CURRENT_QUESTION_B", "UNSENT_DRAFT_C", "TRIAL_DRAFT_C", "PRIVATE_REPORT_PATH", "UNREAD_SOURCE"]) assert(!note.text.includes(absent));
  assert(held.signals.every(signal => signal.aborted), "request controller always closes after success");
  assert(![...h.timers.values()].some(timer => timer.ms === 10000)); assert.equal(h.revoked.length, 0);
  h.fireTimer(1000); assert.deepEqual(h.revoked, [note.url]); assert.equal(h.urls.size, 0);
  assert(h.nodes["reading-note-status"].textContent.includes("交由瀏覽器")); assert.equal(h.nodes["export-reading-note"].disabled, false);
}

async function eligibleKindsAndHistoricalReplacement() {
  const continued = fixture(); continued.job.kind = "continue"; continued.job.continuation = {parent_id: "parent-id", parent_question: "PARENT_QUESTION_ONLY"};
  for (const [f, expected] of [[fixture(), "original one-shot answer"], [continued, "PARENT_QUESTION_ONLY"], [projectFixture(), "app.py:4-4"], [changesFixture(), "EXPANDED_COMPARISON_QUESTION"]]) {
    const h = harness(f, true), before = preserved(h); assert.equal(h.nodes["export-reading-note"].disabled, false, f.job.kind);
    await exportNote(h); assert.equal(h.downloads.length, 1, f.job.kind); assert(h.downloads[0].text.includes(expected)); assert.equal(preserved(h), before);
    assert.equal(h.requests.length, f.sources.length); assert(h.requests.every(request => request.method === "GET"));
    if (f.job.kind === "project") { assert.equal(h.run("readingNotePlan().ranges.length"), 4); assert(h.downloads[0].text.includes("helper.py:1-1")); }
    if (f.job.kind === "changes") for (const label of ["BEFORE", "AFTER", "FIXED CALLER", "synthetic_snapshot_sha256"]) assert(h.downloads[0].text.includes(label));
  }
  const h = harness(), held = holdSources(h), work = exportNote(h);
  h.options.history = {version: SHA, entries: clone([h.f.job]), evicted: 0}; await h.run("loadHistory()");
  assert.equal(h.run("state.readingExport.pending"), true, "same retained data may be replaced by a fresh history response");
  h.run("state.job={...state.job,elapsed_seconds:2}; $('question').value='LATER_UNSENT_DRAFT'; state.questionRevision++; controls();");
  const before = preserved(h); held.resolve(); await work;
  assert.equal(h.downloads.length, 1); assert(!h.downloads[0].text.includes("LATER_UNSENT_DRAFT")); assert.equal(preserved(h), before);
}

async function invalidationBeforeDownload() {
  for (const mutate of [
    h => { h.nodes["history-select"].value = ""; h.nodes["history-select"].listeners.change(); h.nodes["history-select"].value = "history-a"; h.nodes["history-select"].listeners.change(); },
    h => h.run("state.project={...state.project}; readingNoteControls();"),
    h => h.run("state.history[0].result.outcome.source_unchanged=false; readingNoteControls();"),
    async h => { h.options.history = {version: SHA, entries: [], evicted: 1}; await h.run("loadHistory()"); }
  ]) {
    const h = harness(), held = holdSources(h), work = exportNote(h); await mutate(h);
    assert(held.signals.every(signal => signal.aborted), "identity/lifecycle changes synchronously abort the shared source reads");
    const afterAction = preserved(h); held.resolve(); await work; assertNoDownload(h); assert.equal(preserved(h), afterAction);
    assert.equal(h.run("state.job.id"), "current-b"); assert.equal(h.nodes.question.value, "UNSENT_DRAFT_C");
  }
  const h = harness(fixture(), true); h.run("state.modelUnknown=false; state.project.model_policy={mode:'one_shot'}; controls();");
  const held = holdSources(h), work = exportNote(h); h.options.refresh = async () => ({ok: false, status: 409, json: async () => ({error: "owned refresh refusal"})});
  const refresh = h.nodes.refresh.listeners.click(); assert(held.signals.every(signal => signal.aborted), "refresh invalidates before awaiting its response");
  await refresh; const afterRefresh = preserved(h); held.resolve(); await work; assertNoDownload(h); assert.equal(preserved(h), afterRefresh);
  assert.deepEqual(h.requests.map(request => request.method), ["GET", "GET", "POST"], "the only POST belongs to the separate explicit refresh");
}

async function sameIdentityAnswerReplacement() {
  const h = harness(); h.run("renderViewedAnswer()");
  assert(h.nodes.answer.textContent.includes("ANSWER_A"));
  const held = holdSources(h), work = exportNote(h), replacement = clone(h.f.job);
  replacement.result.outcome.answer.claims[0].text = "REPLACED_ANSWER_A [E1:L1-L2]";
  h.options.history = {version: SHA, entries: [replacement], evicted: 0}; await h.run("loadHistory()");
  assert(held.signals.every(signal => signal.aborted));
  assert(h.nodes.answer.textContent.includes("REPLACED_ANSWER_A"), "same-ID/status history replacement must update the displayed answer itself");
  assert(!h.nodes.answer.textContent.includes("<img src=remote>"));
  held.resolve(); await work; assertNoDownload(h);
  assert.equal(h.nodes["export-reading-note"].disabled, false); h.options.source = null;
  const before = preserved(h); await exportNote(h);
  assert.equal(h.downloads.length, 1); assert(h.downloads[0].text.includes(replacement.result.outcome.answer.claims[0].text));
  assert(!h.downloads[0].text.includes(h.f.job.result.outcome.answer.claims[0].text)); assert.equal(preserved(h), before);
  assert.equal(h.run("state.job.id"), "current-b"); assert.equal(h.nodes.question.value, "UNSENT_DRAFT_C");
}

async function strictEligibility() {
  const changes = [f => {f.job.status = "running";}, f => {f.job.status = "cancelled";},
    f => {f.job.kind = "locate"; f.job.result.kind = "forge8.locate";},
    f => {f.job.status = "incomplete"; f.job.result.status = f.job.result.outcome.status = "insufficient_evidence"; f.job.result.ok = f.job.result.outcome.ok = false; f.job.result.outcome.answer = null; f.job.result.outcome.failure_reason = "Need more source.";},
    f => {f.job.result.outcome.source_unchanged = false;}, f => {f.job.result.outcome.acceptance.ok = false;},
    f => {f.job.result.semantic_claims_verified = true;}, f => {f.job.version = "f".repeat(64);},
    f => {resident(f); f.job.result.server.request_completion.slot_idle = false;},
    f => {resident(f); f.job.result.outcome.acceptance.evidence.transport_secret_cleared = false;},
    f => {resident(f); f.job.request_completion.session_finalization = "complete";}];
  for (const change of changes) {
    const f = fixture(); change(f); const h = harness(f), before = preserved(h);
    assert.equal(h.nodes["export-reading-note"].disabled, true); await exportNote(h); assertNoDownload(h); assert.equal(h.requests.length, 0); assert.equal(preserved(h), before);
  }
  const forgedProject = projectFixture(); forgedProject.job.result.project_reading.discovery.ingress_unchanged = false;
  const h = harness(forgedProject); assert.equal(h.nodes["export-reading-note"].disabled, true); await exportNote(h); assert.equal(h.requests.length, 0);
}

async function completeSourceFailureAndTimeout() {
  for (const alter of [sources => {sources[1].version = "f".repeat(64);}, sources => {sources[1].path = "wrong.py";},
    sources => {sources[1].lines.pop();}, sources => {sources[1].lines[0] = null;}, sources => {sources[1].lines[0] = "embedded\nline";},
    sources => {sources[1].lines[0] = "\u202eunsafe";}, sources => {sources[0].lines[0] = "界".repeat(90000);}]) {
    const h = harness(), originals = clone(h.f.sources); alter(h.f.sources);
    // Serve malformed paths by file ID; do not accidentally turn the fixture into a missing-file failure.
    h.options.source = file => h.response(h.f.sources[originals.findIndex(source => source.path === file.path)]);
    const before = preserved(h); await exportNote(h); assertNoDownload(h); assert.equal(preserved(h), before);
    assert(h.nodes["reading-note-status"].textContent.includes("未匯出")); assert.equal(h.requests.length, 2);
    assert(h.requests.every(request => request.signal.aborted)); assert.equal(h.nodes["export-reading-note"].disabled, false);
  }
  const h = harness(), before = preserved(h);
  h.options.source = (_, init) => new Promise((_, reject) => init.signal.addEventListener("abort", () => reject(Error("owned aborted GET"))));
  const work = exportNote(h); h.fireTimer(10000); await work;
  assertNoDownload(h); assert.equal(preserved(h), before); assert(h.requests.every(request => request.signal.aborted)); assert.equal(h.requests.length, 2);
  assert(h.nodes["reading-note-status"].textContent.includes("等待上限")); assert.equal(h.nodes["export-reading-note"].disabled, false); assert.equal(h.timers.size, 0);
  h.options.source = null; await exportNote(h); assert.equal(h.downloads.length, 1); assert.equal(h.requests.length, 4, "retry happens only after another explicit click");
  const refused = harness(), stable = preserved(refused); refused.options.clickFailure = true; await exportNote(refused);
  assert.equal(refused.downloads.length, 0); assert.equal(refused.body.children.length, 0); assert.equal(preserved(refused), stable);
  assert(refused.nodes["reading-note-status"].textContent.includes("未匯出")); refused.fireTimer(1000); assert.equal(refused.urls.size, 0);
}

(async () => {
  assert(html.indexOf('src="/reading-note.js"') >= 0 && html.indexOf('src="/reading-note.js"') < html.indexOf('src="/app.js"'));
  await historyCurrentDraftIsolation(); await eligibleKindsAndHistoricalReplacement(); await invalidationBeforeDownload();
  await sameIdentityAnswerReplacement(); await strictEligibility(); await completeSourceFailureAndTimeout();
  console.log("reading-note UI fixtures passed (ordinary metadata only; no source execution or inference)");
})().catch(error => { console.error(error); process.exitCode = 1; });
