"use strict";
// DOM/API fixtures only: no project source execution, model or guest.
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
  matches(query) {
    const attribute = query.match(/^\[data-([a-z-]+)(?:="([^"]*)")?\]$/);
    if (attribute) { const key = attribute[1].replace(/-([a-z])/g, (_, char) => char.toUpperCase()); return Object.hasOwn(this.dataset, key) && (attribute[2] === undefined || String(this.dataset[key]) === attribute[2]); }
    return query.startsWith(".") ? (this.className || "").split(" ").includes(query.slice(1)) : this.tag === query;
  }
  querySelectorAll(query) { return this.children.flatMap(node => [...(node.matches(query) ? [node] : []), ...node.querySelectorAll(query)]); }
  querySelector(query) { return this.querySelectorAll(query)[0]; }
  scrollIntoView() { this.scrolls = (this.scrolls || 0) + 1; }
}
const clone = value => JSON.parse(JSON.stringify(value));
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return {promise, resolve}; };
function harness() {
  const nodes = Object.fromEntries([...html.matchAll(/\bid="([^"]+)"/g)].map(match => [match[1], new Node()]));
  const project = {name: "fixture", version: "v1", reader: "qwen35", experiments_enabled: true,
    files: [{id: "0", path: "library.py", lines: 4}, {id: "1", path: "tests/calls.py", lines: 6}, {id: "2", path: "README.md", lines: 1}], excluded: []};
  const source = {version: "v1", path: "tests/calls.py", lines: ["# calls", "clamp(", "    9007199254740993,", "    limit=10)", "clamp(1); clamp(2)", "obj.clamp(3)"], outline: {status: "available", items: []}};
  const match = {file: "1", path: source.path, source_sha256: "a".repeat(64), kind: "name", name: "clamp", start_line: 2, start_column: 0,
    end_line: 4, end_column: 13, name_line: 2, name_column: 0, preview: "clamp(\\n    9007199254740993,\\n    limit=10)"};
  const result = {version: "v1", mode: "calls", query: "clamp", column_unit: "utf8_bytes", semantics_verified: false,
    matches: [match], total_matches: 1, truncated: false, inspected_files: 2, skipped_files: {not_python: 1}, uninspected_files: 0, total_files: 3, stop_reason: null};
  const requests = [], timers = new Map(); let timerId = 0;
  const fixture = {result, source, searchOverride: null, sourceOverride: null}, response = data => ({ok: true, json: async () => clone(data)});
  const context = {URLSearchParams, AbortController, TextEncoder, location: {hash: "#token=fixture", pathname: "/", search: ""}, history: {replaceState() {}}, sessionStorage: {setItem() {}, getItem: () => null},
    document: {getElementById: id => nodes[id], createElement: tag => new Node(tag), createDocumentFragment: () => new Node("fragment"), createTextNode: text => { const node = new Node("#text"); node.textContent = text; return node; }},
    setTimeout: (callback, ms) => { timers.set(++timerId, {callback, ms}); return timerId; }, clearTimeout: id => timers.delete(id),
    fetch: async (route, options) => {
      requests.push({route, method: options.method}); assert.equal(options.method, "GET", "search/navigation cannot authorize a mutation");
      assert.equal(options.headers.Authorization, "Bearer fixture"); assert.equal(options.credentials, "omit"); assert.equal(options.redirect, "error");
      if (route.startsWith("/api/definitions?")) return fixture.searchOverride ? fixture.searchOverride(options) : response(fixture.result);
      if (route.startsWith("/api/source?")) return fixture.sourceOverride ? fixture.sourceOverride(options) : response(fixture.source);
      throw Error("unexpected request: " + route);
    }};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(root, "src/forge8/web/reading-note.js"), "utf8"), context);
  vm.runInContext(app.slice(0, app.lastIndexOf("(async () => {")), context);
  const run = script => vm.runInContext(script, context); context.projectFixture = project;
  run(`state.project = projectFixture; state.source = {file: projectFixture.files[0], version:'v1', path:'library.py', lines:['def clamp(value, limit=10):','    return min(value, limit)','class Tool:','    pass'],
      outline:{status:'available',items:[{name:'clamp',kind:'function',start_line:1,definition_line:1,end_line:2,stub:false},{name:'Tool',kind:'class',start_line:3,definition_line:3,end_line:4,stub:false}]}};
    state.focus = [{file:'0',path:'library.py',start:1,end:2}]; state.anchor = 1; state.end = 2;
    state.job = {id:'reading',status:'incomplete'}; state.history = [{id:'previous',status:'incomplete'}];
    const target = {file:'0',path:'library.py',version:'v1',entry:'clamp',source_sha256:'b'.repeat(64),source_bytes:84};
    const baseline = {...target,id:'A',input_text:'{"args":[1]}',result_text:'1',input_sha256:'c'.repeat(64),runtime_sha256:'d'.repeat(64),report_sha256:'e'.repeat(64),trace_lines:false};
    const inputComparison = {revision:4,baseline,current_id:'trial',can_pin:true,settling:false,outcome:'different',reason:null,current_report_sha256:'f'.repeat(64)};
    const job = {...target,id:'trial',status:'completed',elapsed_seconds:1,input_text:'{"args":[9007199254740993]}',result_text:'9007199254740993',
      source_unchanged:true,runtime_unchanged:true,input_comparison:inputComparison};
    state.experiment = {target,job,baseline,inputComparison,baselineRevision:4,input:'TRIAL_DRAFT',visible:true,prepareRequest:0,statusRequest:0,inputRevision:0};
    $('question').value='UNSENT_QUESTION'; $('answer').textContent='PRESERVED_ANSWER'; $('experiment-input').value='TRIAL_DRAFT';
    $('search-mode').value='calls'; searchModeChanged(); $('search-query').value='clamp'; controls();`);
  return {nodes, run, context, project, fixture, requests, timers, response};
}
const preserved = h => h.run("JSON.stringify([state.focus,state.job,state.history,state.selectedHistory,state.experiment,$('question').value,$('answer').textContent,$('experiment-input').value,$('change-slot').value])");
const readingState = h => h.run("JSON.stringify([state.source,state.anchor,state.end,state.page,state.rangeHint,state.readingTrail])") + preserved(h);
const search = h => h.run("searchSource()");
const hits = h => h.nodes["search-results"].querySelectorAll(".call-hit");

async function ordinaryWorkflow() {
  const h = harness(), before = readingState(h), stable = preserved(h);
  assert.equal(h.nodes["search-call-hint"].hidden, false);
  await search(h); assert.equal(readingState(h), before);
  assert.deepEqual(h.requests, [{route: "/api/definitions?q=clamp&version=v1&mode=calls", method: "GET"}]);
  assert.equal(hits(h).length, 1); assert.equal(hits(h)[0].dataset.callPath, "tests/calls.py");
  assert.equal(hits(h)[0].dataset.callStartLine, 2); assert(hits(h)[0].textContent.includes("直接名稱呼叫"));
  assert(h.nodes["search-summary"].textContent.includes("已檢查 2 / 3")); assert(h.nodes["search-summary"].textContent.includes("非 Python 1"));
  await hits(h)[0].listeners.click();
  assert.equal(h.requests.length, 2); assert.equal(h.requests[1].route, "/api/source?file=1&version=v1");
  assert.equal(h.run("state.source.path"), "tests/calls.py"); assert.equal(h.run("state.anchor"), 2); assert.equal(h.run("state.end"), 4);
  assert.equal(preserved(h), stable, "navigation must preserve trial target, raw input, current result and pinned A");
  assert.equal(h.run("state.readingTrail.length"), 1); assert.equal(h.timers.size, 0);
  await hits(h)[0].listeners.click(); assert.equal(h.requests.length, 2, "same-source navigation reuses the admitted display cache");
  h.run("renderOutline()"); // Caller file has no definitions, but call navigation was still valid.
  assert.equal(h.nodes["outline-list"].children.length, 0);
}

async function shortcutAndOccurrences() {
  const h = harness(); h.run("renderOutline()"); const stable = readingState(h);
  const shortcuts = h.nodes["outline-list"].querySelectorAll(".definition-calls"); assert.equal(shortcuts.length, 2);
  assert.equal(shortcuts[1].dataset.callQuery, "Tool"); assert(shortcuts[1].attributes["aria-label"].includes("不解析實際綁定"));
  await shortcuts[0].listeners.click(); assert.equal(readingState(h), stable, "definition shortcut searches only");
  h.fixture.result.query = "Tool"; h.fixture.result.matches = []; h.fixture.result.total_matches = 0;
  await shortcuts[1].listeners.click(); assert.equal(h.nodes["search-query"].value, "Tool"); assert.equal(readingState(h), stable);
  const row = clone(h.fixture.result); row.query = "clamp"; row.total_matches = 4;
  row.matches = [
    {start_line:5,start_column:0,end_line:5,end_column:8,name_line:5,name_column:0,kind:"name",preview:"clamp(1)"},
    {start_line:5,start_column:10,end_line:5,end_column:18,name_line:5,name_column:10,kind:"name",preview:"clamp(2)"},
    {start_line:6,start_column:0,end_line:6,end_column:12,name_line:6,name_column:4,kind:"attribute",preview:"obj.clamp(3)"},
    {start_line:5,start_column:0,end_line:5,end_column:18,name_line:5,name_column:0,kind:"name",preview:"clamp(clamp(2))"}
  ].map(item => ({file:"1",path:"tests/calls.py",source_sha256:"a".repeat(64),name:"clamp",...item}));
  h.fixture.result = row; h.nodes["search-query"].value = "clamp"; await search(h);
  assert.equal(hits(h).length, 4, "same-line and nested occurrences keep exact independent identities");
  assert(hits(h)[2].textContent.includes("屬性名稱呼叫（接收物件未解析）"));
  const trial = preserved(h); await hits(h)[1].listeners.click(); assert.equal(h.run("state.anchor"), 5); assert.equal(h.run("state.end"), 5); assert.equal(preserved(h), trial);
  assert(h.nodes["search-summary"].textContent.includes("UTF-8 位元組欄"));
  h.nodes["search-mode"].value = "keywords"; h.nodes["search-mode"].listeners.change(); assert.equal(h.nodes["search-call-hint"].hidden, true);
}

async function schemaAndCoverage() {
  const mutations = [r => {delete r.inspected_files;}, r => {r.extra=true;}, r => {r.version="wrong";}, r => {r.query="other";}, r => {r.mode="keywords";},
    r => {r.column_unit="utf16";}, r => {r.semantics_verified=true;}, r => {r.skipped_files={unknown:1};}, r => {r.skipped_files.not_python=-1;},
    r => {r.inspected_files=1;}, r => {r.total_files=4;}, r => {r.uninspected_files=true;}, r => {r.stop_reason="other";}, r => {r.stop_reason="time_limit";},
    r => {r.inspected_files=1;r.uninspected_files=1;}, r => {r.total_matches=3;r.truncated=true;},
    r => {r.total_matches=500001;}, r => {r.total_matches=0;}, r => {r.truncated=true;}, r => {r.matches.push(clone(r.matches[0]));r.total_matches=2;},
    r => {r.matches[0].path="../unadmitted.py";}, r => {r.matches[0].file="0";}, r => {r.matches[0].source_sha256="A".repeat(64);},
    r => {r.matches[0].kind="resolved";}, r => {r.matches[0].name="other";}, r => {r.matches[0].start_line=0;}, r => {r.matches[0].end_line=7;},
    r => {r.matches[0].start_column=-1;}, r => {r.matches[0].end_column=262145;}, r => {r.matches[0].name_line=1;},
    r => {r.matches[0].name_line=4;r.matches[0].name_column=12;}, r => {r.matches[0].name_column=true;},
    r => {r.matches[0].preview="x".repeat(241);}, r => {r.matches[0].preview="raw\nline";}, r => {r.matches[0].preview="spoof\u202e";}, r => {delete r.matches[0].name_column;},
    r => {r.matches=Array.from({length:41},()=>clone(r.matches[0]));r.total_matches=41;},
    r => {r.matches.push({...r.matches[0],start_column:1,name_column:1,source_sha256:"b".repeat(64)});r.total_matches=2;}];
  for (const mutate of mutations) {
    const h = harness(), before = readingState(h); mutate(h.fixture.result); await search(h);
    assert.equal(hits(h).length, 0, "invalid response must fail whole"); assert(h.nodes.error.textContent.includes("回報")); assert.equal(readingState(h), before); assert.equal(h.requests.length, 1);
  }
  const limited = harness(); Object.assign(limited.fixture.result,{inspected_files:1,skipped_files:{},uninspected_files:2,stop_reason:"node_budget",total_matches:42,truncated:true});
  limited.fixture.result.matches=Array.from({length:40},(_,i)=>({...limited.fixture.result.matches[0],start_column:i,name_column:i}));
  await search(limited); assert.equal(hits(limited).length, 40); assert(limited.nodes["search-summary"].textContent.includes("未檢查 2")); assert(limited.nodes["search-summary"].textContent.includes("總節點"));
  const empty = harness(); Object.assign(empty.fixture.result,{matches:[],total_matches:0,inspected_files:0,skipped_files:{not_python:1,syntax:2}});
  await search(empty); assert.equal(hits(empty).length,0); assert(empty.nodes["search-summary"].textContent.includes("0 / 0")); assert(empty.nodes["search-summary"].textContent.includes("語法／版本 2"));
  for (const query of ["obj.clamp","merge headers","x".repeat(129),"<script>"]) { const h=harness();h.nodes["search-query"].value=query;await search(h);assert.equal(h.requests.length,0); }
  const unicode = harness(); unicode.nodes["search-query"].value=unicode.fixture.result.query=unicode.fixture.result.matches[0].name="ｃｌａｍｐ";
  unicode.fixture.result.matches[0].preview="<img src=x onerror=alert(1)>"; await search(unicode);
  assert.equal(hits(unicode).length,1);assert.equal(hits(unicode)[0].querySelectorAll("img").length,0);assert(hits(unicode)[0].textContent.includes("<img"));
  const parenthesized=harness();parenthesized.fixture.result.matches[0].name_column=1;await search(parenthesized);assert.equal(hits(parenthesized).length,1);
  const oversized=harness(), longPath="x".repeat(2000)+".py";oversized.project.files[1].path=longPath;
  oversized.fixture.result.matches=Array.from({length:40},(_,i)=>({...oversized.fixture.result.matches[0],path:longPath,start_column:i,name_column:i}));oversized.fixture.result.total_matches=40;
  await search(oversized);assert.equal(hits(oversized).length,0);assert(oversized.nodes.error.textContent.includes("回報"));
  const browsing=harness();browsing.project.browse_only=true;browsing.project.reader=null;browsing.project.experiments_enabled=false;
  await search(browsing);await hits(browsing)[0].listeners.click();assert.equal(browsing.requests.length,2);assert.equal(browsing.timers.size,0);
}

async function searchRacesAndTimeouts() {
  for (const mutation of ["$('search-query').value='other'; $('search-query').listeners.input()", "$('search-mode').value='text';searchModeChanged()", "state.project={...state.project}", "state.refreshing=true", "state.project.version='v2'"]) {
    const h=harness(), pending=deferred();h.fixture.searchOverride=()=>pending.promise;const request=search(h);h.run(mutation);const before=readingState(h);
    pending.resolve(h.response(h.fixture.result));await request;assert.equal(hits(h).length,0);assert.equal(readingState(h),before);assert.equal(h.timers.size,0);
  }
  for (const stage of ["fetch","body"]) {
    const h=harness(), started=deferred();h.fixture.searchOverride=options=> {
      const pending=()=>new Promise((_,reject)=>{options.signal.addEventListener("abort",()=>reject(Error("aborted")));started.resolve();});
      return stage==="fetch"?pending():{ok:true,json:pending};
    };
    const request=search(h);await started.promise;[...h.timers.values()].find(timer=>timer.ms===10000).callback();await request;
    assert(h.nodes.error.textContent.includes("10 秒"));assert.equal(h.requests.length,1);assert.equal(h.timers.size,0);
  }
}

async function openingRacesAndBounds() {
  const changes=["$('search-query').value='other';$('search-query').listeners.input()", "$('search-mode').value='definitions';searchModeChanged()", "state.project={...state.project}",
    "state.sourceRequest++", "state.refreshing=true", "state.anchor=2", "state.focus=[]", "state.questionRevision++", "state.statusRequest++", "state.readingExportRevision++",
    "state.experiment.preparing=true", "state.experiment.target={...state.experiment.target}", "state.experiment.job={...state.experiment.job}",
    "state.experiment.job.result_text='other'", "state.experiment.inputComparison.revision++", "state.experiment.baseline={...state.experiment.baseline}",
    "$('experiment-input').value='NEW_DRAFT'", "state.experiment.inputRevision+=2", "state.contextRequest+=2", "state.callSelectionRevision+=2"];
  for(const mutation of changes){
    const h=harness();await search(h);const pending=deferred();h.fixture.sourceOverride=()=>pending.promise;const opening=hits(h)[0].listeners.click();h.run(mutation);const before=readingState(h);
    pending.resolve(h.response(h.fixture.source));await opening;assert.equal(readingState(h),before,mutation);assert.equal(h.run("state.source.path"),"library.py");assert.equal(h.timers.size,0);
  }
  const aba=harness();await search(aba);aba.run("renderCode()");const pending=deferred();aba.fixture.sourceOverride=()=>pending.promise;const opening=hits(aba)[0].listeners.click();
  aba.nodes.code.querySelector('[data-line="3"]').children[0].listeners.click({shiftKey:false});
  aba.nodes.code.querySelector('[data-line="1"]').children[0].listeners.click({shiftKey:false});
  aba.nodes.code.querySelector('[data-line="2"]').children[0].listeners.click({shiftKey:true});const unchanged=readingState(aba);
  pending.resolve(aba.response(aba.fixture.source));await opening;assert.equal(readingState(aba),unchanged,"same-source line-selection ABA invalidates pending navigation");
  for(const mutation of [s=>{s.version="wrong";},s=>{s.path="wrong.py";},s=>{s.lines.pop();},s=>{s.lines[0]=null;},s=>{s.lines[0]="x".repeat(6*262144);}]){
    const h=harness();await search(h);mutation(h.fixture.source);const before=readingState(h);await hits(h)[0].listeners.click();assert.equal(readingState(h),before);assert(h.nodes.error.textContent.includes("原碼"));
  }
  const timeout=harness();await search(timeout);const before=readingState(timeout), bodyStarted=deferred();
  timeout.fixture.sourceOverride=options=>({ok:true,json:()=>new Promise((_,reject)=>{options.signal.addEventListener("abort",()=>reject(Error("aborted")));bodyStarted.resolve();})});
  const loading=hits(timeout)[0].listeners.click();await bodyStarted.promise;[...timeout.timers.values()].find(timer=>timer.ms===10000).callback();await loading;
  assert.equal(readingState(timeout),before);assert(timeout.nodes.error.textContent.includes("10 秒"));assert.equal(hits(timeout)[0].disabled,false);assert.equal(timeout.timers.size,0);
  const large=harness();large.project.files[1].lines=100;large.fixture.source.lines=Array(100).fill("# retained");large.fixture.result.matches[0].end_line=100;
  await search(large);const stable=preserved(large);await hits(large)[0].listeners.click();assert.equal(large.run("state.anchor"),null);assert.equal(large.run("state.end"),null);
  assert(large.nodes["range-label"].textContent.includes("超過 80 行"));assert.equal(preserved(large),stable);
  const comparison=harness();comparison.project.comparison={head:"c".repeat(40),catalogue:{files:[]}};comparison.project.files[1].path=comparison.fixture.source.path=comparison.fixture.result.matches[0].path="before/tests/calls.py";
  await search(comparison);assert(hits(comparison)[0].textContent.includes("HEAD"));await hits(comparison)[0].listeners.click();assert.equal(comparison.run("state.source.path"),"before/tests/calls.py");
}

let finished = false;
process.on("beforeExit",()=>{if(!finished){console.error("call navigation fixture left an unsettled promise");process.exitCode=1;}});
(async()=>{
  assert.match(html,/<option value="calls">同名呼叫位置<\/option>/);
  assert(html.includes("同一行多次、巢狀、屬性或非字面值"));
  await ordinaryWorkflow();await shortcutAndOccurrences();await schemaAndCoverage();await searchRacesAndTimeouts();await openingRacesAndBounds();
  finished = true;
  console.log("same-spelling call navigation UI fixtures passed (no execution)");
})().catch(error=>{finished=true;console.error(error);process.exitCode=1;});
