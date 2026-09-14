"use strict";
// Owned DOM/API fixture only; never executes project source, a model or a guest.
const fs = require("node:fs"), path = require("node:path"), vm = require("node:vm"), assert = require("node:assert/strict");
const root = path.resolve(__dirname, ".."), app = fs.readFileSync(path.join(root, "src/forge8/web/app.js"), "utf8");
const html = fs.readFileSync(path.join(root, "src/forge8/web/index.html"), "utf8");
class Node {
  constructor(tag = "") { this.tag = tag; this.children = []; this.parentElement = null; this.listeners = {}; this.value = ""; this.dataset = {}; this._text = ""; this.scrollTop = this.scrollLeft = 0; this.classList = {toggle() {}}; }
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
  scrollIntoView() { this.scrolls = (this.scrolls || 0) + 1; }
}
const clone = value => JSON.parse(JSON.stringify(value));
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return {promise, resolve}; };
function harness() {
  const nodes = Object.fromEntries([...html.matchAll(/\bid="([^"]+)"/g)].map(match => [match[1], new Node()]));
  const project = {name: "fixture", version: "v1", reader: "qwen35", files: [{id: "0", path: "app.py", lines: 2}, {id: "1", path: "http/session.py", lines: 6}], excluded: []};
  const item = {name: "merge_setting", kind: "function", start_line: 2, definition_line: 2, end_line: 4, stub: false};
  const source = {version: "v1", path: "http/session.py", lines: ["DEFAULT = {}", "def merge_setting(headers):", "    # <img src=x onerror=alert(1)>", "    return headers", "", "# tail"], outline: {status: "available", items: [item]}};
  const result = {version: "v1", mode: "keywords", terms: ["merge", "headers"], matches: [{file: "1", path: source.path, ...item,
    matched_terms: [{term: "merge", field: "name", line: null}, {term: "headers", field: "source", line: 2}], preview: {line: 3, text: source.lines[2]}}],
    truncated: false, total_matches: 1, unavailable_files: 0};
  const requests = [], timers = new Map(); let timerId = 0;
  const fixture = {result, source, searchOverride: null, sourceOverride: null}, response = data => ({ok: true, json: async () => clone(data)});
  const context = {URLSearchParams, AbortController, TextEncoder, location: {hash: "#token=fixture", pathname: "/", search: ""}, history: {replaceState() {}},
    sessionStorage: {setItem() {}, getItem: () => null},
    document: {getElementById: id => nodes[id], createElement: tag => new Node(tag), createDocumentFragment: () => new Node("fragment"), createTextNode: text => { const node = new Node("#text"); node.textContent = text; return node; }},
    setTimeout: (callback, ms) => { timers.set(++timerId, {callback, ms}); return timerId; }, clearTimeout: id => timers.delete(id),
    fetch: async (route, options) => {
      requests.push({route, method: options.method}); assert.equal(options.method, "GET", "navigation cannot authorize a POST");
      assert.equal(options.headers.Authorization, "Bearer fixture"); assert.equal(options.credentials, "omit"); assert.equal(options.redirect, "error");
      if (route.startsWith("/api/definitions?")) {
        if (fixture.searchOverride) return fixture.searchOverride(options);
        const query = new URLSearchParams(route.split("?")[1]);
        return response(query.get("mode") === "keywords" ? fixture.result : {version: "v1", matches: [{file: "1", path: source.path, ...item}], truncated: false, unavailable_files: 0});
      }
      if (route.startsWith("/api/source?")) return fixture.sourceOverride ? fixture.sourceOverride(options) : response(fixture.source);
      if (route.startsWith("/api/search?")) return response({matches: [{file: "1", path: source.path, line: 2, text: source.lines[1]}], truncated: false});
      throw Error("unexpected request: " + route);
    }};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(root, "src/forge8/web/reading-note.js"), "utf8"), context);
  vm.runInContext(app.slice(0, app.lastIndexOf("(async () => {")), context);
  const run = script => vm.runInContext(script, context);
  context.projectFixture = project;
  run(`state.project = projectFixture; state.source = {file: projectFixture.files[0], version:'v1', path:'app.py', lines:['value = 1','# retained'],outline:{status:'available',items:[]}};
    state.focus = [{file:'0',path:'app.py',start:1,end:2}]; state.anchor = 1; state.end = 2;
    state.job = {id:'reading',status:'incomplete'}; state.history = [{id:'previous',status:'incomplete'}];
    state.experiment = {target:null,job:{id:'trial',status:'completed'},input:'TRIAL_DRAFT',visible:false,prepareRequest:0};
    $('question').value = 'UNSENT_QUESTION'; $('answer').textContent = 'PRESERVED_ANSWER'; $('experiment-input').value = 'TRIAL_DRAFT';
    $('search-mode').value = 'keywords'; searchModeChanged(); $('search-query').value = 'merge headers'; controls();`);
  return {nodes, run, context, project, fixture, requests, timers, response};
}
const readingState = h => h.run("JSON.stringify([state.source,state.focus,state.anchor,state.end,state.readingTrail,state.job,state.history,state.experiment,$('question').value,$('answer').textContent,$('experiment-input').value])");
const preserved = h => h.run("JSON.stringify([state.focus,state.job,state.history,state.experiment,$('question').value,$('answer').textContent,$('experiment-input').value])");
const search = h => h.run("searchSource()");
const hit = h => h.nodes["search-results"].children[0];

async function ordinaryWorkflow() {
  const h = harness(), before = readingState(h), stable = preserved(h);
  assert.equal(h.nodes["search-keyword-hint"].hidden, false);
  assert(h.nodes["search-query"].placeholder.includes("merge headers"));
  await search(h);
  assert.deepEqual(h.requests, [{route: "/api/definitions?q=merge+headers&version=v1&mode=keywords", method: "GET"}]);
  assert.equal(readingState(h), before); assert.equal(h.nodes["search-results"].children.length, 1);
  assert(hit(h).className.includes("keyword-hit")); assert.equal(hit(h).dataset.definitionPath, "http/session.py");
  assert(hit(h).textContent.includes("merge（名稱）")); assert(hit(h).textContent.includes("headers（原碼 L2）"));
  assert(hit(h).textContent.includes("<img src=x")); assert.equal(hit(h).querySelectorAll("img").length, 0);
  assert(h.nodes["search-summary"].textContent.includes("純字面"));
  await hit(h).listeners.click();
  assert.equal(h.requests.length, 2, "one source GET; opening the prepared source must not fetch twice");
  assert.equal(h.requests[1].route, "/api/source?file=1&version=v1");
  assert.equal(h.run("state.source.path"), "http/session.py"); assert.equal(h.run("state.anchor"), 2); assert.equal(h.run("state.end"), 4);
  assert.equal(preserved(h), stable); assert.equal(h.run("state.readingTrail.length"), 1);
  await hit(h).listeners.click(); assert.equal(h.requests.length, 2, "same-source explicit selection needs no new GET");
  h.nodes["search-mode"].value = "definitions"; h.nodes["search-mode"].listeners.change(); h.nodes["search-query"].value = "merge_setting";
  assert.equal(h.nodes["search-keyword-hint"].hidden, true); await search(h);
  assert.equal(h.requests.at(-1).route, "/api/definitions?q=merge_setting&version=v1");
  h.nodes["search-mode"].value = "text"; h.nodes["search-mode"].listeners.change(); await search(h);
  assert.equal(h.requests.at(-1).route, "/api/search?q=merge_setting", "legacy text request stays version/mode-free");
}

async function schemaAndLimits() {
  const changes = [result => {result.version = "wrong";}, result => {result.mode = "exact";}, result => {result.terms = [];},
    result => {result.terms = ["merge", "merge"];}, result => {result.terms = Array.from({length: 9}, (_, index) => `term${index}`);},
    result => {result.matches[0].path = "unadmitted.py";}, result => {result.matches[0].file = "0";},
    result => {result.matches[0].kind = "class";}, result => {result.matches[0].stub = 0;}, result => {result.matches[0].start_line = true;},
    result => {result.matches[0].definition_line = 7;}, result => {result.matches[0].end_line = 7;},
    result => {result.matches[0].preview.line = 1;}, result => {result.matches[0].preview.text = "x".repeat(241);},
    result => {result.matches[0].matched_terms.reverse();}, result => {result.matches[0].matched_terms[0].line = 2;},
    result => {result.matches[0].matched_terms[1].line = 5;}, result => {result.matches[0].matched_terms[1].field = "semantic";},
    result => {result.total_matches = 0;}, result => {result.truncated = true;}, result => {result.total_matches = 5; result.truncated = true;}, result => {result.unavailable_files = 3;},
    result => {result.matches[0].name = "x".repeat(65536);}, result => {result.matches[0].extra = "unexpected";},
    result => {result.matches.push(clone(result.matches[0])); result.total_matches = 2;}];
  for (const change of changes) {
    const h = harness(), before = readingState(h); change(h.fixture.result); await search(h);
    assert.equal(h.nodes["search-results"].children.length, 0, "malformed keyword response never renders a partial list");
    assert(h.nodes.error.textContent.includes("回報")); assert.equal(readingState(h), before); assert.equal(h.requests.length, 1);
  }
  const h = harness(); h.fixture.result.matches[0].matched_terms[1] = {term: "headers", field: "path", line: null};
  h.fixture.result.matches[0].preview.text = "🧪".repeat(240); h.fixture.result.matches[0].name = "long_".repeat(300);
  h.fixture.result.unavailable_files = 1;
  await search(h); assert.equal(h.nodes["search-results"].children.length, 1);
  assert(hit(h).textContent.includes("headers（路徑）")); assert(h.nodes["search-summary"].textContent.includes("1 / 1"));
  assert(h.nodes["search-summary"].textContent.includes("1 個 Python"));
  const empty = harness(); empty.fixture.result.matches = []; empty.fixture.result.total_matches = 0; await search(empty);
  assert(empty.nodes["search-summary"].textContent.includes("0 / 0"));
  const oversized = harness(); oversized.nodes["search-query"].value = "x".repeat(129); await search(oversized);
  assert.equal(oversized.requests.length, 0); assert.equal(oversized.nodes["search-query"].value.length, 129);
}

async function searchRacesAndTimeout() {
  for (const mutation of ["$('search-query').value='new'; $('search-query').listeners.input()", "$('search-mode').value='text'; searchModeChanged()",
    "state.project = {...state.project}", "state.refreshing = true", "state.project.version = 'v2'; state.searchRequest++"]) {
    const h = harness(), pending = deferred(); h.fixture.searchOverride = () => pending.promise;
    const request = search(h); h.run(mutation); const before = readingState(h);
    pending.resolve(h.response(h.fixture.result)); await request;
    assert.equal(h.nodes["search-results"].children.length, 0); assert.equal(readingState(h), before);
  }
  const h = harness(); h.fixture.searchOverride = options => new Promise((_, reject) => options.signal.addEventListener("abort", () => reject(Error("aborted"))));
  const request = search(h); [...h.timers.values()].find(timer => timer.ms === 10000).callback(); await request;
  assert(h.nodes.error.textContent.includes("10 秒")); assert.equal(h.requests.length, 1);
}

async function openingRacesAndBounds() {
  for (const mutation of ["$('search-query').value='other'; $('search-query').listeners.input()", "$('search-mode').value='definitions'; searchModeChanged()",
    "state.project = {...state.project}", "state.refreshing=true", "state.sourceRequest++", "state.anchor=2", "state.focus=[]",
    "state.questionRevision++", "state.experiment.preparing=true", "state.experiment.job={id:'another',status:'running'}"]) {
    const h = harness(); await search(h); const pending = deferred(); h.fixture.sourceOverride = () => pending.promise;
    const opening = hit(h).listeners.click(); h.run(mutation); const before = readingState(h);
    pending.resolve(h.response(h.fixture.source)); await opening;
    assert.equal(readingState(h), before, `obsolete source opening: ${mutation}`);
    assert.equal(h.run("state.source.path"), "app.py"); assert.equal(h.requests.length, 2);
  }
  for (const change of [source => {source.version="wrong";}, source => {source.path="wrong.py";}, source => {source.lines.pop();}, source => {source.outline.items[0].end_line=5;}]) {
    const h = harness(); await search(h); change(h.fixture.source); const before = readingState(h); await hit(h).listeners.click();
    assert.equal(readingState(h), before); assert(h.nodes.error.textContent.includes("座標"));
  }
  const h = harness(); await search(h); const before = readingState(h);
  h.fixture.sourceOverride = options => new Promise((_, reject) => options.signal.addEventListener("abort", () => reject(Error("aborted"))));
  const opening = hit(h).listeners.click(); [...h.timers.values()].find(timer => timer.ms === 10000).callback(); await opening;
  assert.equal(readingState(h), before); assert(h.nodes.error.textContent.includes("10 秒")); assert.equal(hit(h).disabled, false);
  const long = harness(); long.project.files[1].lines = 100; long.fixture.source.lines = ["DEFAULT = {}", "def merge_setting(headers):", ...Array(97).fill("    # retained"), "    return headers"];
  long.fixture.source.outline.items[0].end_line = long.fixture.result.matches[0].end_line = 100;
  await search(long); const stable = preserved(long); await hit(long).listeners.click();
  assert.equal(long.run("state.anchor"), 2); assert.equal(long.run("state.end"), 100);
  assert.equal(preserved(long), stable, "opening a packable definition does not add it to the question");
  assert.equal(long.nodes["add-selection"].disabled, false);
  const requests = long.requests.length;
  long.nodes["add-selection"].listeners.click();
  assert.deepEqual(JSON.parse(long.run("JSON.stringify(state.focus)")), [
    {file: "0", path: "app.py", start: 1, end: 2},
    {file: "1", path: "http/session.py", start: 2, end: 81},
    {file: "1", path: "http/session.py", start: 82, end: 100},
  ]);
  const full = preserved(long);
  await hit(long).listeners.click();
  assert.equal(long.run("state.anchor"), null); assert.equal(long.run("state.end"), null);
  assert(long.run("state.rangeHint").includes("剩餘 0 段"));
  assert.equal(preserved(long), full, "a full question cannot receive a clipped definition");
  assert.equal(long.requests.length, requests, "packing and same-source navigation make no requests");
}

(async () => {
  assert.match(html, /<option value="keywords">關鍵字找定義<\/option>/);
  await ordinaryWorkflow(); await schemaAndLimits(); await searchRacesAndTimeout(); await openingRacesAndBounds();
  console.log("keyword definition UI fixtures passed (no execution)");
})().catch(error => { console.error(error); process.exitCode = 1; });
