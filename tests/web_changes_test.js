"use strict";
// Dependency-free owned DOM/API fixture: node tests/web_changes_test.js
// No browser, Git, model or project source execution.
const fs = require("node:fs"), path = require("node:path"), vm = require("node:vm"), assert = require("node:assert/strict");
const root = path.resolve(__dirname, ".."), app = fs.readFileSync(path.join(root, "src/forge8/web/app.js"), "utf8");
const html = fs.readFileSync(path.join(root, "src/forge8/web/index.html"), "utf8");
class Node {
  constructor(tag = "") { this.tag = tag; this.children = []; this.parentElement = null; this.listeners = {}; this.value = ""; this.dataset = {}; this._text = ""; this.scrollTop = this.scrollLeft = 0; this.classList = {toggle() {}}; }
  set textContent(value) { this.replaceChildren(); this._text = String(value); }
  get textContent() { return this._text + this.children.map(node => node.textContent).join(""); }
  get data() { return this._text; }
  set data(value) { this._text = value; }
  appendData(value) { this._text += value; }
  set innerHTML(_) { throw Error("HTML injection"); }
  append(...children) { for (const child of children.flatMap(node => node.tag === "fragment" ? [...node.children] : [node])) { child.remove(); child.parentElement = this; this.children.push(child); } }
  replaceChildren(...children) { for (const child of this.children) child.parentElement = null; this.children = []; this._text = ""; this.append(...children); }
  remove() { if (this.parentElement) { const siblings = this.parentElement.children; siblings.splice(siblings.indexOf(this), 1); this.parentElement = null; } }
  after(node) { if (this.parentElement) { node.remove(); node.parentElement = this.parentElement; const siblings = this.parentElement.children; siblings.splice(siblings.indexOf(this) + 1, 0, node); } }
  closest(selector) { for (let node = this; node; node = node.parentElement) if (selector.startsWith(".") ? (node.className || "").split(" ").includes(selector.slice(1)) : node.tag === selector) return node; return null; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  setAttribute(name, value) { (this.attributes ||= {})[name] = String(value); }
  querySelectorAll(query) { return this.children.flatMap(node => [...((node.tag === query) || (query === "[data-line]" && node.dataset.line) ? [node] : []), ...node.querySelectorAll(query)]); }
  querySelector(query) { const line = query.match(/data-line="(\d+)"/)?.[1]; return this.querySelectorAll("[data-line]").find(node => String(node.dataset.line) === line); }
  scrollIntoView() {}
}
const clone = value => JSON.parse(JSON.stringify(value));
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return {promise, resolve}; };
const flush = () => new Promise(resolve => setImmediate(resolve));
const sourceTexts = {
  "before/cache.py": ['DEFAULT = {"label": "unknown"}', "", "def get_record(cache, key):", "    return cache.get(key) or DEFAULT"],
  "after/cache.py": ['DEFAULT = {"label": "unknown"}', "", "def get_record(cache, key):", "    return cache.get(key, DEFAULT)"],
  "after/app.py": ["from cache import get_record", "def label(cache):", '    return get_record(cache, "item")["label"]'],
  "before/app.py": ["from cache import get_record", "def label(cache):", '    return get_record(cache, "item")["label"]'],
  "comparison.json": ["{}"],
};
function comparisonProject() {
  return {name: "owned fixture", reader: "qwen35", version: "c".repeat(64), excluded: [],
    files: Object.entries(sourceTexts).map(([path, lines], index) => ({id: String(index), path, lines: lines.length})),
    comparison: {head: "a".repeat(40), original_snapshot_sha256: "b".repeat(64), scope: "retained comparison sources", excluded: {before: ["asset.bin"], after: [".venv"]},
      catalogue: {schema_version: 1, kind: "source_change_catalogue", semantics_verified: false, detail_limited: false,
        files: [{path: "cache.py", status: "modified", before_lines: 4, after_lines: 4, line_endings_only: false,
          detail: {hunks: "exact", units: "available", reasons: []},
          hunks: [{before: {start: 3, count: 1}, after: {start: 3, count: 1}}],
          units: [{id: "U1", name: "get_record", kind: "function", status: "modified", before: {start_line: 3, end_line: 4}, after: {start_line: 3, end_line: 4}}]}]}}};
}
function harness() {
  const nodes = Object.fromEntries([...html.matchAll(/\bid="([^"]+)"/g)].map(match => [match[1], new Node()]));
  const ordinary = {name: "owned fixture", reader: "qwen35", version: "ordinary", excluded: [], comparison: null, files: [{id: "0", path: "cache.py", lines: 4}]};
  const requests = [], stored = [], fixture = {serverProject: ordinary, nextProject: comparisonProject(), failure: null, sourceOverride: null, contextOverride: null, importOverride: null, job: {id: null, status: "idle"}, entries: []};
  const response = value => ({ok: true, json: async () => value});
  const context = {URLSearchParams, AbortController, TextEncoder, location: {hash: "#token=owned", pathname: "/", search: ""}, history: {replaceState() {}},
    sessionStorage: {setItem: (key, value) => stored.push([key, value]), getItem: () => null},
    document: {getElementById: id => nodes[id], createElement: tag => new Node(tag), createDocumentFragment: () => new Node("fragment"), createTextNode: value => { const node = new Node("#text"); node.data = value; return node; }},
    setTimeout: () => 1, clearTimeout() {}, fetch: async (route, options) => {
      requests.push({route, method: options.method, body: options.body ? JSON.parse(options.body) : null});
      assert.equal(options.headers.Authorization, "Bearer owned");
      assert.equal(options.credentials, "omit"); assert.equal(options.redirect, "error");
      if (route === "/api/refresh") {
        if (fixture.failure) throw Error(fixture.failure);
        fixture.serverProject = fixture.nextProject; fixture.entries = []; fixture.job = {id: null, status: "idle"};
        return response(fixture.serverProject);
      }
      if (route.startsWith("/api/source?")) {
        if (fixture.sourceOverride) return fixture.sourceOverride(route);
        const query = new URLSearchParams(route.split("?")[1]), file = fixture.serverProject.files.find(item => item.id === query.get("file"));
        assert(file, "only a host-admitted file ID may be requested");
        return response({version: fixture.serverProject.version, path: file.path,
          lines: sourceTexts[file.path] || sourceTexts["before/cache.py"], outline: {status: "available", items: []}});
      }
      if (route === "/api/context") {
        const body = JSON.parse(options.body);
        if (fixture.contextOverride) return fixture.contextOverride(body);
        const file = fixture.serverProject.files.find(item => item.id === body.file);
        assert(file); assert.equal(body.version, fixture.serverProject.version);
        const found = file.path.endsWith("cache.py") && body.start > 1;
        return response({...body, path: file.path, status: "available", reason: null, semantics_verified: false, scope: "same-file lexical scopes",
          bindings: found ? [{id: "B1", name: "DEFAULT", classification: "module", scope: "get_record", scope_line: 3, use_lines: [4], reason: null}] : [],
          candidates: found ? [{binding_id: "B1", name: "DEFAULT", kind: "assignment", start_line: 1, end_line: 1, use_lines: [4], conditional: false}] : []});
      }
      if (route === "/api/import-source" && fixture.importOverride) return fixture.importOverride(JSON.parse(options.body));
      if (route === "/api/jobs") {
        fixture.job = {id: "explicit-question", kind: "explain", status: "running", version: fixture.serverProject.version,
          question: JSON.parse(options.body).question, comparison: clone(fixture.serverProject.comparison), elapsed_seconds: 0};
        return response({id: fixture.job.id});
      }
      if (route === "/api/jobs/current") return response(fixture.job);
      if (route === "/api/history") return response({version: fixture.serverProject.version, entries: fixture.entries, evicted: 0});
      if (route === "/api/jobs/current-B/cancel") { fixture.job = {...fixture.job, status: "cancelled"}; return response({ok: true}); }
      throw Error("unexpected request: " + route);
    }};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(root, "src/forge8/web/reading-note.js"), "utf8"), context);
  vm.runInContext(app.slice(0, app.lastIndexOf("(async () => {")), context);
  const run = script => vm.runInContext(script, context), show = project => { context.projectFixture = project; fixture.serverProject = project; run("showProject(projectFixture)"); };
  show(ordinary);
  return {nodes, context, run, show, fixture, requests, stored, ordinary, response};
}
const focusOf = h => JSON.parse(h.run("JSON.stringify(state.focus)"));
const pairButton = h => h.nodes["changes-list"].querySelectorAll("button").find(button => button.textContent === "以這組定義取代全部選段");
const inspectButton = (h, index = 0) => h.nodes.selections.children[index].querySelectorAll("button").find(button => button.textContent.startsWith("查看名稱來源"));
const contextButton = (h, expand = false) => h.nodes["context-results"].querySelectorAll("button").find(button => expand ? button.textContent.startsWith("擴展此選段") : button.textContent === "查看");
const contextResult = (h, body, changes = {}) => {
  const candidates = (changes.candidates || [{name: "DEFAULT", kind: "assignment", start_line: 1, end_line: 1, use_lines: [body.end], conditional: false}])
    .map(candidate => ({binding_id: "B1", ...candidate}));
  return {...body, path: h.fixture.serverProject.files.find(file => file.id === body.file).path,
    status: "available", reason: null, semantics_verified: false, scope: "same-file lexical scopes",
    bindings: candidates.length ? [{id: "B1", name: candidates[0].name, classification: "module", scope: "get_record", scope_line: 3, use_lines: candidates[0].use_lines, reason: null}] : [],
    ...changes, candidates};
};
async function manual(h, role, path, start, end) {
  h.context.pathFixture = path; h.context.startFixture = start; h.context.endFixture = end;
  h.nodes["change-slot"].value = role;
  await h.run("openFile(state.project.files.find(file => file.path === pathFixture), state.project.version, startFixture, endFixture)");
  h.run("controls()"); assert.equal(h.nodes["add-selection"].disabled, false);
  await h.nodes["add-selection"].listeners.click();
}

async function pairedWorkflow() {
  const h = harness(), {nodes, run, fixture, requests} = h;
  nodes.question.value = "是哪種輸入改變了行為？"; run("controls()");
  await nodes["change-mode"].listeners.click();
  assert.deepEqual(requests[0], {route: "/api/refresh", method: "POST", body: {mode: "changes"}});
  assert.equal(nodes["change-mode"].attributes["aria-pressed"], "true");
  assert.equal(nodes.question.maxLength, 1300);
  for (const id of ["locate", "ask-project", "traceback-locate", "observation"]) assert.equal(nodes[id].hidden, true);
  assert.equal(nodes["changes-list"].querySelectorAll("img").length, 0);
  assert(!nodes.files.textContent.includes("comparison.json"), "host metadata is not a source-selection target");
  assert(nodes["excluded-summary"].textContent.includes("原始來源未納入比較：2 項"));
  assert(nodes["excluded-list"].textContent.includes("HEAD · asset.bin"));
  assert(nodes["excluded-list"].textContent.includes("目前已儲存 · .venv"));
  assert.equal(nodes.ask.disabled, true);
  const pair = pairButton(h); assert.equal(pair.disabled, false);
  await pair.listeners.click();
  assert.deepEqual(focusOf(h).map(item => item.role), ["before", "after"]);
  assert.deepEqual(focusOf(h).map(item => [item.start, item.end]), [[3, 4], [3, 4]], "definition pairing does not silently add DEFAULT");
  assert.equal(nodes["change-slot"].value, "caller"); assert.equal(nodes.ask.disabled, true);
  assert(nodes["changes-feedback"].textContent.includes("常數"));
  await manual(h, "caller", "after/app.py", 1, 3);
  assert.equal(nodes.ask.disabled, false);
  const caller = focusOf(h)[2];
  await manual(h, "before", "before/cache.py", 1, 4);
  await manual(h, "after", "after/cache.py", 1, 4);
  assert.deepEqual(focusOf(h)[2], caller, "adding required constant context preserves the selected caller");
  assert.equal(requests.filter(item => item.route === "/api/jobs").length, 0);
  assert.equal(requests.filter(item => item.method === "POST").length, 1, "only explicit mode refresh precedes the question");
  await run("submitQuestion('locate')"); await run("submitQuestion('project')"); await run("locateTraceback()");
  assert(!requests.some(item => ["/api/locate", "/api/project-question", "/api/traceback"].includes(item.route)));
  await run("submitQuestion()"); await flush();
  const submitted = requests.filter(item => item.route === "/api/jobs"); assert.equal(submitted.length, 1);
  assert.equal(submitted[0].body.question, nodes.question.value);
  assert.deepEqual(submitted[0].body.focus, [{file: "0", start: 1, end: 4}, {file: "1", start: 1, end: 4}, {file: "2", start: 1, end: 3}]);
  assert.equal(nodes["change-mode"].disabled, true); assert.equal(nodes["clear-change-selections"].disabled, true);
  fixture.job = {id: null, status: "idle"}; run("renderJob({id:null,status:'idle'})");
  await pair.listeners.click(); assert.equal(focusOf(h).length, 2, "only an explicit replace-all action removes the prior caller");
  assert(nodes["changes-feedback"].textContent.includes("取代全部選段"));
  const firstRemove = nodes.selections.children[0].querySelectorAll("button").find(button => button.textContent === "×");
  await firstRemove.listeners.click();
  assert.deepEqual(focusOf(h).map(item => item.role), ["after"], "removing HEAD cannot relabel after as HEAD");
  assert.equal(nodes.ask.disabled, true);
  await nodes["clear-change-selections"].listeners.click(); assert.deepEqual(focusOf(h), []);
  assert.deepEqual(h.stored, [["forge8.read.token.v1", "owned"]]);
}

async function failuresAndTransitions() {
  const h = harness(), {nodes, run, fixture} = h;
  nodes.question.value = "q".repeat(1500);
  run("state.focus=[{file:'0',path:'cache.py',start:1,end:4}]; renderSelections(); renderJob({id:'old',status:'answered',question:'old',result:{outcome:{answer:{claims:[{text:'OLD ANSWER',citations:[]}]}}}})");
  const oldFocus = focusOf(h), oldAnswer = nodes.answer.textContent, oldProject = run("state.project");
  fixture.failure = "comparison_preparation_unavailable";
  await nodes["change-mode"].listeners.click();
  assert.equal(run("state.project"), oldProject); assert.deepEqual(focusOf(h), oldFocus);
  assert.equal(nodes.answer.textContent, oldAnswer); assert.equal(nodes.question.value.length, 1500);
  assert.equal(nodes["changes-panel"].hidden, true); assert.equal(nodes.question.maxLength, 2000);
  assert(nodes.error.textContent.includes("Git 2.43")); assert(nodes.error.textContent.includes("一般閱讀仍可使用"));
  fixture.failure = null;
  await nodes["change-mode"].listeners.click();
  assert.equal(nodes.question.value.length, 1500, "mode switch must never truncate an existing draft");
  assert.equal(nodes.ask.disabled, true); assert(nodes["question-count"].textContent.includes("原稿保留"));
  let prevented = false; nodes.question.selectionStart = 0; nodes.question.selectionEnd = 1500;
  nodes.question.listeners.paste({clipboardData: {getData: () => "x".repeat(1301)}, preventDefault() {prevented = true;}});
  assert(prevented); assert.equal(nodes.question.value.length, 1500);
  nodes.question.value = "short"; nodes.question.listeners.input();
  await pairButton(h).listeners.click(); await manual(h, "caller", "after/app.py", 1, 3);
  const prior = focusOf(h);
  fixture.sourceOverride = async () => ({ok: true, json: async () => ({version: "wrong", path: "before/cache.py", lines: ["WRONG"]})});
  await pairButton(h).listeners.click(); assert.deepEqual(focusOf(h), prior);
  assert(nodes.error.textContent.includes("原有選段全部保留"));
  fixture.sourceOverride = null;
  const held = pairButton(h), next = clone(fixture.serverProject);
  fixture.nextProject = next;
  await nodes.refresh.listeners.click();
  assert.deepEqual(h.requests.filter(item => item.route === "/api/refresh").at(-1).body, {});
  assert.equal(run("inChanges()"), true); assert.deepEqual(focusOf(h), []);
  const requestCount = h.requests.length; await held.listeners.click(); assert.equal(h.requests.length, requestCount, "same-byte refresh invalidates old pairing controls");
  fixture.nextProject = h.ordinary;
  await nodes["change-mode"].listeners.click();
  assert.deepEqual(h.requests.filter(item => item.route === "/api/refresh").at(-1).body, {mode: "source"});
  assert.equal(nodes.question.maxLength, 2000); assert.equal(nodes["locate"].hidden, false);
  assert.equal(nodes["change-slot-control"].hidden, true); assert.equal(nodes["selection-limits"].hidden, false);
  assert.equal(nodes.question.value, "short");
}

async function catalogueLimitsAndStaleSelection() {
  const h = harness(), project = comparisonProject(); h.show(project);
  h.nodes.question.value = "question"; h.run("controls()");
  const wait = deferred(); h.fixture.sourceOverride = () => wait.promise;
  const selection = pairButton(h).listeners.click(); assert.equal(h.run("state.pairPending"), true);
  h.show(clone(project));
  wait.resolve(h.response({version: project.version, path: "before/cache.py", lines: sourceTexts["before/cache.py"]}));
  await selection; assert.deepEqual(focusOf(h), []); assert.equal(h.run("state.pairPending"), false);
  h.fixture.sourceOverride = null;
  const variants = [
    {status: "added", before_lines: 0, units: [], hunks: [{before: {start: 0, count: 0}, after: {start: 0, count: 4}}], detail: {hunks: "exact", units: "unavailable", reasons: ["unsupported_language"]}},
    {units: [], hunks: [{before: {start: 0, count: 4}, after: {start: 0, count: 4}}], detail: {hunks: "coarse", units: "unavailable", reasons: ["diff_work_limit"]}},
    {units: [], hunks: [], detail: {hunks: "unavailable", units: "unavailable", reasons: ["catalogue_limit"]}},
    {line_endings_only: true, units: [], hunks: [], detail: {hunks: "exact", units: "available", reasons: ["line_endings_only"]}},
  ];
  for (const change of variants) {
    const variant = comparisonProject(); Object.assign(variant.comparison.catalogue.files[0], change); h.show(variant);
    assert(!h.nodes["changes-list"].textContent.includes("L0"));
    if (change.status === "added") assert(h.nodes["changes-list"].textContent.includes("沒有可引用行"));
    if (change.detail.hunks === "coarse") assert(h.nodes["changes-list"].textContent.includes("內含未改行"));
    if (change.detail.hunks === "unavailable") assert(h.nodes["changes-list"].textContent.includes("此路徑仍完整列出"));
    if (change.line_endings_only) assert(h.nodes["changes-list"].textContent.includes("只有換行差異"));
  }
  const empty = comparisonProject(); empty.comparison.catalogue.files = []; h.show(empty);
  assert(h.nodes["changes-list"].textContent.includes("不代表被排除的檔案"));
  const literal = comparisonProject(); literal.comparison.catalogue.files[0].units[0].name = '<img src=x onerror="alert(1)">'; h.show(literal);
  assert(h.nodes["changes-list"].textContent.includes("<img")); assert.equal(h.nodes["changes-list"].querySelectorAll("img").length, 0);
  const before = h.run("state.project"), invalid = comparisonProject(); invalid.comparison.catalogue.semantics_verified = true;
  h.context.invalidProject = invalid;
  assert.throws(() => h.run("showProject(invalidProject)"), /比較來源或目錄資料不完整/);
  assert.equal(h.run("state.project"), before, "invalid mode data cannot partly reset the existing UI");
  assert(!h.requests.some(item => item.method === "POST"));
}

async function selectionConstraints() {
  const h = harness(), project = comparisonProject(); h.show(project);
  h.nodes.question.value = "question";
  await manual(h, "before", "before/app.py", 1, 3);
  await manual(h, "after", "after/app.py", 1, 3);
  await manual(h, "caller", "after/cache.py", 3, 4);
  assert.equal(h.nodes.ask.disabled, true); assert(h.nodes["change-selection-status"].textContent.includes("未修改"));
  await pairButton(h).listeners.click();
  await manual(h, "caller", "after/cache.py", 3, 4);
  assert.equal(h.nodes.ask.disabled, true); assert(h.nodes["change-selection-status"].textContent.includes("不能完全重複"));
  await manual(h, "caller", "after/app.py", 1, 3);
  assert.equal(h.nodes.ask.disabled, false);
  const saved = focusOf(h);
  h.fixture.sourceOverride = async route => {
    const file = project.files.find(file => file.id === new URLSearchParams(route.split("?")[1]).get("file"));
    const lines = [...sourceTexts[file.path]]; lines[2] = "x".repeat(4001);
    return h.response({version: project.version, path: file.path, lines, outline: {status: "available", items: []}});
  };
  await pairButton(h).listeners.click(); assert.deepEqual(focusOf(h), saved);
  assert(h.nodes.error.textContent.includes("4,000"));
  h.nodes["change-slot"].value = "before";
  await h.run("openFile(state.project.files[0], state.project.version, 3, 4)");
  assert.equal(h.nodes["add-selection"].disabled, true);
  assert(h.nodes["range-label"].textContent.includes("4,000"));
  const oversized = comparisonProject(); oversized.files[0].lines = oversized.files[1].lines = 81;
  oversized.comparison.catalogue.files[0].units[0].before = {start_line: 1, end_line: 81};
  oversized.comparison.catalogue.files[0].units[0].after = {start_line: 1, end_line: 81};
  h.show(oversized); assert.equal(pairButton(h).disabled, true);
  assert(h.nodes["changes-list"].textContent.includes("超過每段 80 行"));
  assert(!h.requests.some(item => item.method === "POST"));
}

async function capturedHistoryCitations() {
  const h = harness(), {nodes, run, fixture} = h, project = comparisonProject(); h.show(project);
  nodes.question.value = "NEXT DRAFT";
  await pairButton(h).listeners.click(); await manual(h, "caller", "after/app.py", 1, 3);
  const before = focusOf(h), references = [{evidence_id: "E1", path: "before/cache.py", start_line: 3, end_line: 4}, {evidence_id: "E2", path: "after/cache.py", start_line: 3, end_line: 4}];
  const prior = {id: "prior-A", kind: "explain", status: "answered", question: "Original comparison", elapsed_seconds: 9,
    version: project.version, comparison: clone(project.comparison), files: project.files, gpu: "released",
    result: {outcome: {answer: {claims: [{text: "A [E1:L3-L4] B [E2:L3-L4]", citations: references}]}}}};
  fixture.entries = [prior];
  fixture.job = {id: "current-B", kind: "explain", status: "running", question: "current", elapsed_seconds: 1, version: project.version, comparison: clone(project.comparison)};
  h.context.currentJob = fixture.job; run("renderJob(currentJob)"); await run("loadHistory()");
  nodes["history-select"].value = prior.id; nodes["history-select"].listeners.change();
  assert(nodes.answer.textContent.includes("第三段呼叫端固定使用目前版本"));
  assert.equal(run("state.job.id"), "current-B"); assert.equal(nodes.cancel.disabled, false);
  assert.equal(nodes.question.value, "NEXT DRAFT"); assert.deepEqual(focusOf(h), before);
  const citations = nodes.answer.querySelectorAll("button").filter(button => button.className === "citation");
  await citations[0].listeners.click(); await citations[1].listeners.click();
  const first = citations[0].parentElement.children[1], second = citations[1].parentElement.children[1];
  assert(first.textContent.includes("HEAD " + "a".repeat(12))); assert(first.textContent.includes("or DEFAULT"));
  assert(second.textContent.includes("目前已儲存")); assert(second.textContent.includes("get(key, DEFAULT)"));
  assert.equal(h.requests.filter(item => item.route === "/api/jobs").length, 0, "history source reads do not start inference");
  await nodes.cancel.listeners.click(); await flush();
  assert(h.requests.some(item => item.route === "/api/jobs/current-B/cancel"));
  assert(nodes.answer.textContent.includes("Original comparison"));
  h.context.staleJob = {...prior, comparison: {...prior.comparison, head: "d".repeat(40)}};
  run("renderAnswer(staleJob)"); assert(nodes.answer.textContent.includes("比較版本資訊不符"));
  assert.equal(nodes.answer.querySelectorAll("button").length, 0);
}

async function declarationContextWorkflow() {
  const h = harness(); h.show(comparisonProject()); h.nodes.question.value = "KEEP DRAFT";
  await pairButton(h).listeners.click(); await manual(h, "caller", "after/app.py", 1, 3);
  h.run("renderJob({id:'prior',status:'cancelled',question:'KEEP PRIOR',elapsed_seconds:1})");
  const original = focusOf(h), caller = h.run("state.focus[2]"), after = h.run("state.focus[1]"), job = h.run("state.job"), answer = h.nodes.answer.textContent;
  await inspectButton(h).listeners.click();
  assert.deepEqual(h.requests.filter(item => item.route === "/api/context").at(-1).body, {file: "0", version: "c".repeat(64), start: 3, end: 4});
  assert.equal(h.nodes["source-context"].hidden, false);
  assert(h.nodes["context-status"].textContent.includes("① HEAD 原碼"));
  assert(html.includes("不代表執行時的值、實際生效的宣告或完整依賴"));
  assert(html.includes("區域名稱可能遮蔽")); assert(html.includes("跨檔案"));
  assert(h.nodes["context-results"].textContent.includes("DEFAULT"));
  const reads = h.requests.filter(item => item.route.startsWith("/api/source?")).length;
  await contextButton(h).listeners.click();
  assert.equal(h.requests.filter(item => item.route.startsWith("/api/source?")).length, reads + 1, "candidate navigation fetches exactly one retained source, not a second unbound read");
  assert.equal(h.run("state.source.path"), "before/cache.py"); assert.equal(h.run("state.anchor"), 1);
  assert.deepEqual(focusOf(h), original, "view is not add or expand");
  await contextButton(h, true).listeners.click();
  assert.deepEqual(focusOf(h).map(item => [item.start, item.end]), [[1, 4], [3, 4], [1, 3]]);
  assert.equal(h.run("state.focus[1]"), after); assert.equal(h.run("state.focus[2]"), caller);
  assert.equal(h.nodes["context-results"].children.length, 0, "expansion invalidates results for the prior selection");
  assert(h.nodes["context-status"].textContent.includes("共用預算仍在模型載入前檢查"));
  await inspectButton(h, 1).listeners.click(); assert(h.nodes["context-status"].textContent.includes("目前已儲存"));
  await contextButton(h, true).listeners.click();
  assert.deepEqual(focusOf(h).map(item => [item.start, item.end]), [[1, 4], [1, 4], [1, 3]]);
  assert.equal(h.run("state.focus[2]"), caller); assert.equal(h.run("state.job"), job);
  assert.equal(h.nodes.answer.textContent, answer); assert.equal(h.nodes.question.value, "KEEP DRAFT");
  assert.equal(h.requests.filter(item => item.route === "/api/jobs").length, 0);
  assert(h.requests.filter(item => item.method === "POST").every(item => item.route === "/api/context"));
}

async function contextLimitsAndOrdinaryMode() {
  const h = harness(); h.nodes.question.value = "ordinary question"; await manual(h, "", "cache.py", 3, 4);
  const original = focusOf(h);
  await inspectButton(h).listeners.click(); assert(h.nodes["context-status"].textContent.includes("cache.py · L3–L4"));
  assert(!h.nodes["context-status"].textContent.includes("HEAD"));
  for (const [status, reason, note] of [["available", null, "不代表上下文完整"], ["limited", "name_limit", "未提供部分清單"], ["unsupported", "language", "目前只支援 Python"], ["unavailable", "syntax_or_version", "無法解析"]]) {
    h.fixture.contextOverride = async body => h.response(contextResult(h, body, {status, reason, candidates: []}));
    await inspectButton(h).listeners.click();
    assert.equal(h.nodes["context-results"].children.length, 0); assert(h.nodes["context-status"].textContent.includes(note));
    assert.deepEqual(focusOf(h), original);
  }
  h.fixture.contextOverride = async body => {
    const result = contextResult(h, body), candidate = result.candidates[0];
    result.candidates = [{...candidate, name: '<img src=x onerror="alert(1)">', conditional: true}, {...candidate, name: '<img src=x onerror="alert(1)">', start_line: 2, end_line: 2}];
    result.bindings[0].name = result.candidates[0].name;
    return h.response(result);
  };
  await inspectButton(h).listeners.click();
  assert.equal(h.nodes["context-results"].children.length, 1, "multiple origins belong under their one name-use scope");
  assert.equal(h.nodes["context-results"].children[0].children.filter(node => node.className === "context-candidate").length, 2);
  assert(h.nodes["context-results"].textContent.includes("有多個同名宣告"));
  assert(h.nodes["context-results"].textContent.includes("未證明會執行")); assert(h.nodes["context-results"].textContent.includes("<img"));
  assert.equal(h.nodes["context-results"].querySelectorAll("img").length, 0);
  for (const change of [{version: "wrong"}, {path: "elsewhere.py"}, {semantics_verified: true}, {scope: "resolved dependencies"}, {status: "limited", reason: "candidate_limit"}]) {
    h.fixture.contextOverride = async body => h.response(contextResult(h, body, change));
    await inspectButton(h).listeners.click(); assert(h.nodes.error.textContent.includes("版本或座標不一致"));
    assert.equal(h.nodes["context-results"].children.length, 0); assert.deepEqual(focusOf(h), original);
  }
  h.fixture.contextOverride = async body => h.response(contextResult(h, body, {candidates: [{name: "DEFAULT", kind: "assignment", start_line: 0, end_line: 1, use_lines: [4], conditional: false}]}));
  await inspectButton(h).listeners.click(); assert.equal(h.nodes["context-results"].children.length, 0);
  h.fixture.contextOverride = null; await inspectButton(h).listeners.click(); await contextButton(h, true).listeners.click();
  assert.deepEqual(focusOf(h), [{file: "0", path: "cache.py", start: 1, end: 4}]);
  assert.equal(h.nodes.question.value, "ordinary question"); assert.equal(h.requests.filter(item => item.route === "/api/jobs").length, 0);
}

async function scopedContextGroups() {
  const h = harness(), lines = ["VALUE = 1", "def outer():", "    captured = 2", "    def inner(parameter):",
    "        local = parameter", "        return local, captured, VALUE, missing", "    return inner", "class Demo:",
    "    label = VALUE", "    hint: UnknownType", "", ""];
  const project = {...h.ordinary, files: [{id: "0", path: "cache.py", lines: lines.length}]}; h.show(project);
  h.fixture.sourceOverride = async () => h.response({version: project.version, path: "cache.py", lines});
  await manual(h, "", "cache.py", 4, 10); h.nodes.question.value = "KEEP SCOPED DRAFT";
  h.run("renderJob({id:'scope-prior',status:'cancelled',question:'KEEP PRIOR',elapsed_seconds:1})");
  const original = focusOf(h), prior = h.run("state.job"), answer = h.nodes.answer.textContent;
  const binding = (id, name, classification, scope, scope_line, use_lines, reason = null) => ({id, name, classification, scope, scope_line, use_lines, reason});
  const bindings = [binding("B1", "parameter", "parameter", "outer.inner", 4, [5]),
    binding("B2", "local", "local", "outer.inner", 4, [6]), binding("B3", "captured", "free", "outer.inner", 4, [6]),
    binding("B4", "VALUE", "module", "outer.inner", 4, [6]), binding("B5", "missing", "unresolved", "outer.inner", 4, [6], "scope_mapping"),
    binding("B6", "VALUE", "class", "Demo", 8, [9], "class_namespace_lookup"),
    binding("B7", "UnknownType", "annotation", "Demo", 8, [10], "annotation_scope")];
  const origin = (index, kind, line) => ({binding_id: bindings[index].id, name: bindings[index].name, kind, start_line: line, end_line: line,
    use_lines: bindings[index].use_lines, conditional: false});
  const candidates = [origin(0, "parameter", 4), origin(1, "assignment", 5), origin(2, "assignment", 3), origin(3, "assignment", 1), origin(5, "assignment", 1)];
  h.fixture.contextOverride = async body => h.response(contextResult(h, body, {bindings, candidates}));
  await inspectButton(h).listeners.click();
  const groups = h.nodes["context-results"].children;
  assert.equal(groups.length, 7); assert.deepEqual(groups.map(group => group.dataset.bindingId), bindings.map(item => item.id));
  assert(groups[0].textContent.includes("函式參數")); assert(groups[1].textContent.includes("區域名稱")); assert(groups[2].textContent.includes("外層作用域名稱"));
  assert(groups[3].textContent.includes("模組名稱查找")); assert(groups[5].textContent.includes("類別本體名稱查找"));
  assert(groups[6].textContent.includes("註記中的潛在名稱來源"));
  assert(groups[0].textContent.includes("已在選段內")); assert.equal(groups[0].querySelectorAll("button").length, 1);
  assert.equal(groups[0].querySelectorAll("button")[0].textContent, "查看");
  assert.equal(groups[4].querySelectorAll("button").length, 0); assert(groups[4].textContent.includes("不以同名宣告代替"));
  assert.equal(groups[6].querySelectorAll("button").length, 0);
  assert(!h.nodes["context-results"].textContent.includes("有多個同名宣告"), "same spelling in two use scopes is not competing declarations");
  const requests = h.requests.length; h.context.insideCandidate = candidates[0];
  await h.run("contextSourceAction(state.context,insideCandidate,true)");
  assert.equal(h.requests.length, requests, "inside-selection expansion is a no-op even through a stale/direct handler");
  await groups[0].querySelectorAll("button")[0].listeners.click();
  assert.equal(h.run("state.anchor"), 4); assert.deepEqual(focusOf(h), original);
  assert.equal(h.run("state.job"), prior); assert.equal(h.nodes.answer.textContent, answer); assert.equal(h.nodes.question.value, "KEEP SCOPED DRAFT");
  assert(h.requests.filter(item => item.method === "POST").every(item => item.route === "/api/context"));

  h.fixture.contextOverride = async body => h.response(contextResult(h, body, {bindings: [bindings[4]], candidates: []}));
  await inspectButton(h).listeners.click();
  assert(h.nodes["context-status"].textContent.includes("1 組來源未確定"));
  assert.equal(h.nodes["context-results"].children.length, 1); assert.equal(h.nodes["context-results"].querySelectorAll("button").length, 0);

  const imported = binding("B1", "VALUE", "module", '<img src=x onerror="alert(1)">', 4, [6], "__proto__");
  h.fixture.contextOverride = async body => h.response(contextResult(h, body, {bindings: [imported],
    candidates: [{binding_id: "B1", name: "VALUE", kind: "from import", start_line: 1, end_line: 1, use_lines: [6], conditional: false}]}));
  await inspectButton(h).listeners.click();
  assert(h.nodes["context-results"].textContent.includes("<img")); assert.equal(h.nodes["context-results"].querySelectorAll("img").length, 0);
  assert(h.nodes["context-results"].textContent.includes("靜態分析限制")); assert(h.nodes["context-results"].textContent.includes("僅為匯入宣告，不是目標實作"));
  await contextButton(h).listeners.click(); assert.equal(h.run("state.source.path"), "cache.py", "import candidates remain same-file declarations");
  assert.deepEqual(focusOf(h), original);
}

async function scopedContextValidation() {
  const h = harness(); await manual(h, "", "cache.py", 3, 4); h.nodes.question.value = "KEEP VALIDATION DRAFT";
  const original = focusOf(h), body = {file: "0", version: h.ordinary.version, start: 3, end: 4}, valid = contextResult(h, body);
  const binding = valid.bindings[0], candidate = valid.candidates[0];
  const malformed = [
    {...valid, bindings: undefined}, {...valid, bindings: null}, {...valid, bindings: [binding, binding]},
    {...valid, bindings: [{...binding, id: "B01"}]}, {...valid, bindings: [{...binding, id: "B65"}]},
    {...valid, bindings: [{...binding, classification: "runtime-proven"}]},
    {...valid, bindings: [{...binding, scope: ""}]}, {...valid, bindings: [{...binding, scope_line: 0}]},
    {...valid, bindings: [{...binding, scope_line: 5}]}, {...valid, bindings: [{...binding, scope_line: 3.5}]},
    {...valid, bindings: [{...binding, use_lines: [2]}]}, {...valid, bindings: [{...binding, use_lines: [4, 3]}]},
    {...valid, bindings: [{...binding, use_lines: [4, 4]}]}, {...valid, bindings: [{...binding, reason: {}}]},
    {...valid, bindings: [{...binding, extra: true}]}, {...valid, bindings: [{...binding, classification: "unresolved"}]},
    {...valid, candidates: [{...candidate, binding_id: "B2"}]}, {...valid, candidates: [{...candidate, name: "OTHER"}]},
    {...valid, candidates: [{...candidate, use_lines: [3]}]}, {...valid, candidates: [{...candidate, extra: true}]},
    {...valid, candidates: [{...candidate, kind: "unknown declaration"}]},
    {...valid, candidates: [{...candidate, conditional: "false"}]},
    {...valid, candidates: [{...candidate, end_line: 5}]}, {...valid, extra: true},
    {...valid, candidates: Array(65).fill(candidate)}, {...valid, bindings: Array(65).fill(binding)},
    {...valid, status: "limited", candidates: [], bindings: [binding]},
    {...valid, bindings: [{...binding, scope: "字".repeat(23000)}]},
  ];
  for (const result of malformed) {
    h.fixture.contextOverride = async () => h.response(result); await inspectButton(h).listeners.click();
    assert.equal(h.nodes["context-results"].children.length, 0); assert(h.nodes.error.textContent.includes("版本或座標不一致"));
    assert.deepEqual(focusOf(h), original); assert.equal(h.nodes.question.value, "KEEP VALIDATION DRAFT");
  }
  for (const kind of ["parameter", "loop target", "with target", "exception target", "pattern target", "named assignment", "augmented assignment"]) {
    h.fixture.contextOverride = async () => h.response({...valid, candidates: [{...candidate, kind}]});
    await inspectButton(h).listeners.click(); assert.equal(h.nodes["context-results"].children.length, 1);
  }
  h.fixture.contextOverride = async () => h.response({...valid, status: "unavailable", reason: "__proto__", candidates: [], bindings: []});
  await inspectButton(h).listeners.click(); assert(h.nodes["context-status"].textContent.includes("仍可手動查看原碼"));
  h.fixture.contextOverride = async () => h.response(valid); await inspectButton(h).listeners.click();
  const wait = deferred(); h.fixture.sourceOverride = () => wait.promise;
  const previousSource = h.run("state.source"), pending = contextButton(h, true).listeners.click();
  h.run("state.experiment.job={id:'owned-active-trial',status:'running'}; controls()");
  assert.equal(inspectButton(h).disabled, true); assert.equal(contextButton(h, true).disabled, true);
  wait.resolve(h.response({version: h.ordinary.version, path: "cache.py", lines: sourceTexts["before/cache.py"]})); await pending;
  assert.deepEqual(focusOf(h), original); assert.equal(h.run("state.source"), previousSource, "a pending context action cannot change selection/source once a trial starts");
  const previousRequests = h.requests.length;
  await h.run("inspectSourceContext(state.focus[0])"); assert.equal(h.requests.length, previousRequests);
  h.run("state.experiment.job=null; controls()");
  assert(h.requests.filter(item => item.method === "POST").every(item => item.route === "/api/context"));
}

async function staleContextRequests() {
  const h = harness(); h.show(comparisonProject()); h.nodes.question.value = "KEEP";
  await pairButton(h).listeners.click(); await manual(h, "caller", "after/app.py", 1, 3);
  let wait = deferred(), result;
  h.fixture.contextOverride = body => { result = contextResult(h, body); return wait.promise; };
  let pending = inspectButton(h).listeners.click();
  const remove = h.nodes.selections.children[2].querySelectorAll("button").find(button => button.textContent === "×");
  await remove.listeners.click(); const changed = focusOf(h);
  wait.resolve(h.response(result)); await pending;
  assert.equal(h.nodes["source-context"].hidden, true); assert.deepEqual(focusOf(h), changed);
  wait = deferred(); pending = inspectButton(h).listeners.click();
  h.fixture.failure = "owned refresh failure"; await h.nodes.refresh.listeners.click();
  wait.resolve(h.response(result)); await pending;
  assert.equal(h.nodes["source-context"].hidden, true, "even failed refresh discards in-flight context; it does not discard focus");
  assert.deepEqual(focusOf(h), changed); assert.equal(h.nodes.question.value, "KEEP");
  h.fixture.failure = null;
  wait = deferred(); pending = inspectButton(h).listeners.click();
  h.show(clone(h.fixture.serverProject)); wait.resolve(h.response(result)); await pending;
  assert.equal(h.nodes["source-context"].hidden, true); assert.deepEqual(focusOf(h), []);
  await pairButton(h).listeners.click();
  wait = deferred(); pending = inspectButton(h).listeners.click(); const oldResult = result;
  h.fixture.contextOverride = async body => h.response(contextResult(h, body, {candidates: []}));
  await inspectButton(h, 1).listeners.click();
  wait.resolve(h.response(oldResult)); await pending;
  assert(h.nodes["context-status"].textContent.includes("② 目前對應原碼")); assert.equal(h.nodes["context-results"].children.length, 0);
  h.fixture.contextOverride = async () => { throw Error("owned context failure"); };
  const saved = focusOf(h); await inspectButton(h).listeners.click();
  assert(h.nodes.error.textContent.includes("owned context failure")); assert.deepEqual(focusOf(h), saved); assert.equal(h.nodes.question.value, "KEEP");
  assert.equal(h.requests.filter(item => item.route === "/api/jobs").length, 0);
}

async function contextSourceBoundsAndLifecycle() {
  const h = harness(); h.show(comparisonProject()); h.nodes.question.value = "KEEP";
  await pairButton(h).listeners.click(); await manual(h, "caller", "after/app.py", 1, 3);
  const saved = focusOf(h);
  for (const change of [{lines: []}, {version: "wrong"}, {path: "after/cache.py"}, {lines: ["x".repeat(4001), "", "def f():", "    return DEFAULT"]}, {lines: ["constant", "", 3, "return"]}]) {
    await inspectButton(h).listeners.click();
    h.fixture.sourceOverride = async () => h.response({version: "c".repeat(64), path: "before/cache.py", lines: sourceTexts["before/cache.py"], ...change});
    await contextButton(h, true).listeners.click();
    assert.deepEqual(focusOf(h), saved); assert(h.nodes.error.textContent.includes("原有選段全部保留"));
  }
  h.fixture.sourceOverride = null;
  for (const expand of [true, false]) {
    await inspectButton(h).listeners.click();
    const wait = deferred(); h.fixture.sourceOverride = () => wait.promise;
    const source = h.run("state.source"), pending = contextButton(h, expand).listeners.click();
    h.run("state.focus = state.focus.map(item => ({...item})); renderSelections()");
    wait.resolve(h.response({version: "c".repeat(64), path: "before/cache.py", lines: sourceTexts["before/cache.py"]})); await pending;
    assert.deepEqual(focusOf(h), saved); assert.equal(h.run("state.source"), source, "stale candidate reads cannot replace the source panel");
    assert.equal(h.nodes["source-context"].hidden, true); h.fixture.sourceOverride = null;
  }
  await inspectButton(h).listeners.click();
  const wait = deferred(); h.fixture.sourceOverride = () => wait.promise;
  const pending = contextButton(h, true).listeners.click();
  h.fixture.job = {id: "current-B", kind: "explain", status: "running", version: "c".repeat(64), elapsed_seconds: 0};
  h.context.jobFixture = h.fixture.job; h.run("renderJob(jobFixture)");
  assert.equal(inspectButton(h).disabled, true); assert.equal(contextButton(h, true).disabled, true);
  assert.equal(h.nodes.cancel.disabled, false); assert.equal(h.nodes["history-select"].disabled, false);
  wait.resolve(h.response({version: "c".repeat(64), path: "before/cache.py", lines: sourceTexts["before/cache.py"]})); await pending;
  assert.deepEqual(focusOf(h), saved, "a newly active job prevents a pending expansion from mutating input");
  await h.nodes.cancel.listeners.click(); await flush(); assert(h.requests.some(item => item.route === "/api/jobs/current-B/cancel"));
  h.fixture.sourceOverride = null;
  const large = comparisonProject(); large.files[0].lines = 90; h.show(large); await manual(h, "before", "before/cache.py", 3, 4);
  h.fixture.contextOverride = async body => h.response(contextResult(h, body, {candidates: [{name: "helper", kind: "function", start_line: 1, end_line: 90, use_lines: [4], conditional: false}]}));
  await inspectButton(h).listeners.click();
  assert.equal(contextButton(h, true).disabled, true); assert.equal(contextButton(h).disabled, false);
  assert(h.nodes["context-results"].textContent.includes("只能查看，不裁切"));
  h.fixture.sourceOverride = async () => h.response({version: large.version, path: "before/cache.py", lines: Array.from({length: 90}, (_, index) => `owned line ${index + 1}`)});
  await contextButton(h).listeners.click(); assert.equal(h.run("state.end"), 90); assert.equal(h.nodes["add-selection"].disabled, true);
  assert.equal(h.requests.filter(item => item.route === "/api/jobs").length, 0);
}

const importButton = h => h.nodes["context-results"].querySelectorAll("button").find(button => button.textContent === "追蹤匯入來源");
const importSteps = h => h.nodes["context-results"].querySelectorAll("button").filter(button => button.textContent === "查看來源");
async function importHarness(comparison = true, side = "after") {
  const h = harness(), project = comparison ? comparisonProject() : {...h.ordinary, files: [
    {id: "0", path: "app.py", lines: 3}, {id: "1", path: "cache.py", lines: 4}]};
  h.show(project);
  if (comparison) { await pairButton(h).listeners.click(); await manual(h, "caller", "after/app.py", 1, 3); }
  const prefix = comparison ? `${side}/` : "", file = project.files.find(file => file.path === `${prefix}app.py`);
  const target = project.files.find(file => file.path === `${prefix}cache.py`);
  const sources = {"app.py": sourceTexts["after/app.py"], "cache.py": sourceTexts["after/cache.py"]};
  h.fixture.sourceOverride = async route => {
    const query = new URLSearchParams(route.split("?")[1]), item = h.fixture.serverProject.files.find(item => item.id === query.get("file"));
    assert(item); return h.response({version: query.get("version"), path: item.path, lines: sources[item.path] || sourceTexts[item.path], outline: {status: "available", items: []}});
  };
  await manual(h, comparison ? side === "before" ? "before" : "caller" : "", file.path, 2, 3);
  const selectionIndex = focusOf(h).findIndex(item => item.path === file.path);
  const candidate = {binding_id: "B1", name: "get_record", kind: "from import", start_line: 1, end_line: 1, use_lines: [3], conditional: false};
  h.fixture.contextOverride = async body => h.response(contextResult(h, body, {bindings: [{id: "B1", name: "get_record", classification: "module", scope: "label", scope_line: 2, use_lines: [3], reason: null}], candidates: [candidate]}));
  const origin = {file: file.id, path: file.path, name: candidate.name, kind: candidate.kind, start_line: 1, end_line: 1, conditional: false};
  const destination = {file: target.id, path: target.path, name: "get_record", kind: "function", start_line: 3, end_line: 4, conditional: false};
  const result = body => ({...body, path: file.path, scope: "snapshot import candidates", semantics_verified: false,
    status: "available", reason: null, routes: [{outcome: "declaration", reason: null, steps: [origin, destination]}]});
  h.fixture.importOverride = async body => h.response(result(body));
  await inspectButton(h, selectionIndex).listeners.click();
  return {...h, file, target, candidate, origin, destination, result, selectionIndex, sources};
}
async function importNavigationWorkflow() {
  for (const [comparison, side] of [[true, "after"], [true, "before"], [false, "after"]]) {
    const h = await importHarness(comparison, side), {nodes, run} = h;
    nodes.question.value = "KEEP IMPORT DRAFT"; nodes["change-slot"].value = "after";
    run("state.history=[{id:'retained-history',status:'cancelled'}]; state.selectedHistory='retained-history'; state.continuation=state.project.comparison ? null : {parent_id:'keep-parent',parent_question:'KEEP PARENT',version:state.project.version,scope:{origin:'user_focus',focus:['app.py:2-3'],supplements:[]}}");
    nodes.answer.textContent = "KEEP EXISTING ANSWER";
    const saved = focusOf(h), source = run("state.source"), previousAnchor = run("state.anchor"), previousEnd = run("state.end"), history = run("state.history"), continuation = run("state.continuation");
    assert.equal(importSteps(h).length, 0); assert.equal(h.requests.filter(item => item.route === "/api/import-source").length, 0);
    await importButton(h).listeners.click();
    assert.deepEqual(h.requests.filter(item => item.route === "/api/import-source").at(-1).body,
      {file: h.file.id, version: h.fixture.serverProject.version, start: 2, end: 3, binding_id: "B1", declaration_start: 1, declaration_end: 1});
    assert.equal(run("state.source"), source, "tracing never auto-opens a target"); assert.equal(importSteps(h).length, 2);
    assert(nodes["context-results"].textContent.includes("不證明 Python 匯入成功")); assert(nodes["context-results"].textContent.includes("不求值 __all__"));
    const beforeReads = h.requests.filter(item => item.route.startsWith("/api/source?")).length;
    await importSteps(h)[1].listeners.click();
    assert.equal(h.requests.filter(item => item.route.startsWith("/api/source?")).length, beforeReads + 1, "validated source is passed to openFile without a second read");
    assert.equal(run("state.source.path"), h.target.path); assert.equal(run("state.anchor"), 3); assert.equal(run("state.end"), 4);
    assert.deepEqual(focusOf(h), saved); assert.equal(nodes.question.value, "KEEP IMPORT DRAFT"); assert.equal(nodes["change-slot"].value, "after");
    assert.equal(run("state.history"), history); assert.equal(run("state.selectedHistory"), "retained-history"); assert.equal(run("state.continuation"), continuation); assert.equal(nodes.answer.textContent, "KEEP EXISTING ANSWER");
    assert.equal(nodes["source-back"].disabled, false); await nodes["source-back"].listeners.click();
    assert.equal(run("state.source.path"), h.file.path); assert.equal(run("state.anchor"), previousAnchor); assert.equal(run("state.end"), previousEnd);
    assert.equal(importSteps(h).length, 2, "source/back navigation preserves the originating name-source panel");
    assert(h.requests.filter(item => item.method === "POST").every(item => ["/api/context", "/api/import-source"].includes(item.route)));
    assert.equal(h.stored.length, 1, "import navigation never persists source or answers into browser storage");
  }
}
async function importAlternativesAndBounds() {
  const h = await importHarness(), saved = focusOf(h);
  const moduleStep = {...h.destination, name: "cache", kind: "module", start_line: null, end_line: null};
  h.fixture.importOverride = async body => h.response({...h.result(body), routes: [
    {outcome: "module", reason: "submodule_candidate", steps: [h.origin, moduleStep]},
    {outcome: "unresolved", reason: "not_in_snapshot", steps: [h.origin]},
    {outcome: "cycle", reason: "cycle", steps: [h.origin, h.origin]}]});
  await importButton(h).listeners.click();
  assert(h.nodes["context-results"].textContent.includes("3 條候選路徑")); assert(h.nodes["context-results"].textContent.includes("不代表 Python 環境沒有"));
  assert(h.nodes["context-results"].textContent.includes("循環匯入候選"));
  await importSteps(h)[1].listeners.click(); assert.equal(h.run("state.source.path"), h.target.path);
  assert.equal(h.run("state.anchor"), null); assert.equal(h.run("state.end"), null); assert.equal(h.nodes["add-selection"].disabled, true);
  h.target.lines = 0; h.sources[h.target.path] = [];
  await importSteps(h)[1].listeners.click(); assert.equal(h.run("state.source.lines.length"), 0); assert.equal(h.run("state.anchor"), null);
  h.target.lines = 260; h.sources[h.target.path] = Array.from({length: 260}, (_, index) => `line ${index + 1}`);
  h.fixture.importOverride = async body => h.response({...h.result(body), routes: [{outcome: "declaration", reason: null,
    steps: [h.origin, {...h.destination, name: "large_target", start_line: 205, end_line: 260}]}]});
  await importButton(h).listeners.click(); await importSteps(h)[1].listeners.click();
  assert.equal(h.run("state.page"), 1); assert.equal(h.run("state.anchor"), 205); assert.equal(h.run("state.end"), 260);
  h.fixture.importOverride = async body => h.response({...h.result(body), routes: [{outcome: "declaration", reason: null,
    steps: [h.origin, {...h.destination, name: "large_target", start_line: 175, end_line: 260}]}]});
  await importButton(h).listeners.click(); await importSteps(h)[1].listeners.click();
  assert.equal(h.run("state.page"), 0); assert.equal(h.run("state.anchor"), null); assert.equal(h.run("state.end"), null);
  assert(h.nodes["range-label"].textContent.includes("超過 80 行；僅瀏覽")); assert.equal(h.nodes["add-selection"].disabled, true);
  assert.deepEqual(focusOf(h), saved);
  for (const status of ["unavailable", "limited", "unsupported"]) {
    h.fixture.importOverride = async body => h.response({...h.result(body), status, reason: status === "limited" ? "route_limit" : "syntax_or_version", routes: []});
    await importButton(h).listeners.click(); assert.equal(importSteps(h).length, 0); assert.deepEqual(focusOf(h), saved);
  }
  h.fixture.importOverride = async body => h.response({...h.result(body), routes: [{outcome: "unresolved", reason: '<img src=x onerror="alert(1)">', steps: [h.origin]}]});
  await importButton(h).listeners.click(); assert(h.nodes["context-results"].textContent.includes("<img")); assert.equal(h.nodes["context-results"].querySelectorAll("img").length, 0);
}
async function importReexportAliases() {
  const h = await importHarness(), bridge = {id: "exported", path: "after/exported.py", lines: 1};
  h.fixture.serverProject.files.push(bridge);
  h.sources[h.file.path] = ["from exported import public_record as get_record", ...sourceTexts[h.file.path].slice(1)];
  h.sources[bridge.path] = ["from cache import get_record as public_record"];
  const hop = {...bridge, name: "public_record", kind: "from import", start_line: 1, end_line: 1, conditional: false};
  delete hop.id; delete hop.lines; hop.file = bridge.id;
  h.fixture.importOverride = async body => h.response({...h.result(body), routes: [{outcome: "declaration", reason: null, steps: [h.origin, hop, h.destination]}]});
  const saved = focusOf(h); await importButton(h).listeners.click();
  assert.equal(importSteps(h).length, 3); assert(h.nodes["context-results"].textContent.includes("public_record"));
  await importSteps(h)[1].listeners.click(); assert.equal(h.run("state.source.path"), bridge.path); assert.equal(h.run("state.anchor"), 1);
  await importSteps(h)[2].listeners.click(); assert.equal(h.run("state.source.path"), h.target.path); assert.equal(h.run("state.anchor"), 3);
  await h.nodes["source-back"].listeners.click(); assert.equal(h.run("state.source.path"), bridge.path);
  assert.deepEqual(focusOf(h), saved); assert.equal(h.requests.filter(item => item.route === "/api/import-source").length, 1, "all re-export hops are explicit GET navigation, not new resolution or model requests");
}
async function importResponseValidation() {
  const h = await importHarness(), saved = focusOf(h), original = h.run("state.source");
  const body = {file: h.file.id, version: h.fixture.serverProject.version, start: 2, end: 3, binding_id: "B1", declaration_start: 1, declaration_end: 1};
  const valid = h.result(body), route = valid.routes[0], step = h.destination;
  const badStep = change => ({...valid, routes: [{...route, steps: [h.origin, {...step, ...change}]}]});
  const malformed = [
    {...valid, extra: true}, {...valid, version: "wrong"}, {...valid, file: h.target.id}, {...valid, path: h.target.path},
    {...valid, start: 1}, {...valid, end: 2}, {...valid, binding_id: "B2"}, {...valid, declaration_start: 2}, {...valid, declaration_end: 2},
    {...valid, scope: "runtime import"}, {...valid, semantics_verified: true}, {...valid, status: "ready"}, {...valid, reason: {}},
    {...valid, routes: null}, {...valid, routes: Array(33).fill(route)}, {...valid, status: "limited"},
    {...valid, routes: [{...route, extra: true}]}, {...valid, routes: [{...route, outcome: "resolved"}]}, {...valid, routes: [{...route, reason: {}}]},
    {...valid, routes: [{...route, steps: []}]}, {...valid, routes: [{...route, steps: Array(9).fill(h.origin)}]},
    {...valid, routes: [{...route, steps: [{...h.origin, name: "wrong"}, step]}]},
    {...valid, routes: [{...route, steps: [{...h.origin, conditional: true}, step]}]},
    {...valid, routes: [{...route, steps: [{...h.origin, kind: "import"}, step]}]},
    badStep({file: "unknown"}), badStep({path: "../outside.py"}), badStep({file: "0", path: "before/cache.py"}),
    badStep({name: ""}), badStep({kind: "__proto__"}), badStep({conditional: "false"}), badStep({start_line: 0}), badStep({start_line: 3.5}),
    badStep({end_line: 5}), badStep({start_line: null, end_line: null}), badStep({extra: true}),
    badStep({kind: "module"}), {...valid, routes: [{...route, outcome: "module"}]},
    {...valid, reason: "字".repeat(45000)},
  ];
  for (const result of malformed) {
    h.fixture.importOverride = async () => h.response(result); await importButton(h).listeners.click();
    assert.equal(importSteps(h).length, 0); assert(h.nodes.error.textContent.includes("版本、路徑或座標不一致"));
    assert.deepEqual(focusOf(h), saved); assert.equal(h.run("state.source"), original);
  }
  h.fixture.importOverride = async () => { throw Error("owned import response loss"); }; await importButton(h).listeners.click();
  assert.equal(importSteps(h).length, 0); assert(h.nodes.error.textContent.includes("owned import response loss"));
  const count = h.requests.length; await flush(); assert.equal(h.requests.length, count, "lost read-only POST never auto-retries");
}
async function importStaleResponses() {
  for (const change of ["selection", "refresh", "same-version", "source", "job", "trial", "pair"]) {
    const h = await importHarness(), wait = deferred(); let value;
    h.nodes.question.value = "KEEP STALE IMPORT";
    h.fixture.importOverride = body => { value = h.result(body); return wait.promise; };
    const pending = importButton(h).listeners.click(); assert.equal(importButton(h).disabled, true);
    if (change === "selection") h.run("state.focus=state.focus.map(item=>({...item})); renderSelections()");
    if (change === "refresh") { h.fixture.failure = "owned refresh failure"; await h.nodes.refresh.listeners.click(); }
    if (change === "same-version") h.show(clone(h.fixture.serverProject));
    if (change === "source") await h.run("openFile(state.project.files.find(file=>file.path==='after/cache.py'),state.project.version,1)");
    if (change === "job") h.run("renderJob({id:'other-job',status:'running'})");
    if (change === "trial") h.run("state.experiment.job={id:'other-trial',status:'running'}; controls()");
    if (change === "pair") h.run("state.pairPending=true; controls()");
    const saved = focusOf(h), source = h.run("state.source");
    wait.resolve(h.response(value)); await pending;
    assert.equal(importSteps(h).length, 0, change); assert.deepEqual(focusOf(h), saved, change); assert.equal(h.run("state.source"), source, change);
    assert.equal(h.nodes.question.value, "KEEP STALE IMPORT"); assert.equal(h.requests.filter(item => item.route === "/api/import-source").length, 1);
    assert(!h.nodes["context-results"].textContent.includes("正在追蹤"), "discarded late response must not leave a forever-pending row");
  }
}
async function importTargetSourceGuards() {
  for (const change of ["version", "path", "length", "nontext", "selection", "source", "back", "refresh", "same-version", "job", "trial", "pair"]) {
    const h = await importHarness(); h.nodes.question.value = "KEEP TARGET DRAFT";
    await importButton(h).listeners.click();
    const wait = deferred(), previousSource = h.fixture.sourceOverride;
    h.fixture.sourceOverride = () => wait.promise;
    const pending = importSteps(h)[1].listeners.click(); assert.equal(importSteps(h)[1].disabled, true);
    h.fixture.sourceOverride = previousSource;
    if (change === "selection") h.run("state.focus=state.focus.map(item=>({...item})); renderSelections()");
    if (change === "source") await h.run("openFile(state.project.files.find(file=>file.path==='after/cache.py'),state.project.version,1)");
    if (change === "back") await h.nodes["source-back"].listeners.click();
    if (change === "refresh") { h.fixture.failure = "owned refresh failure"; await h.nodes.refresh.listeners.click(); }
    if (change === "same-version") h.show(clone(h.fixture.serverProject));
    if (change === "job") h.run("renderJob({id:'other-job',status:'running'})");
    if (change === "trial") h.run("state.experiment.job={id:'other-trial',status:'running'}; controls()");
    if (change === "pair") h.run("state.pairPending=true; controls()");
    const saved = focusOf(h), source = h.run("state.source"), result = {version: h.fixture.serverProject.version, path: h.target.path, lines: sourceTexts[h.target.path]};
    if (change === "version") result.version = "wrong";
    if (change === "path") result.path = "before/cache.py";
    if (change === "length") result.lines = [];
    if (change === "nontext") result.lines = ["constant", "", 3, "return"];
    wait.resolve(h.response(result)); await pending;
    assert.equal(h.run("state.source"), source, change); assert.deepEqual(focusOf(h), saved, change); assert.equal(h.nodes.question.value, "KEEP TARGET DRAFT");
    assert.equal(h.requests.filter(item => item.route === "/api/refresh").length, change === "refresh" ? 1 : 0);
    const allowed = ["/api/context", "/api/import-source", ...(change === "refresh" ? ["/api/refresh"] : [])];
    assert(h.requests.filter(item => item.method === "POST").every(item => allowed.includes(item.route)), change);
  }
}

(async () => {
  await pairedWorkflow(); await failuresAndTransitions(); await catalogueLimitsAndStaleSelection(); await selectionConstraints(); await capturedHistoryCitations();
  await declarationContextWorkflow(); await contextLimitsAndOrdinaryMode(); await scopedContextGroups(); await scopedContextValidation(); await staleContextRequests(); await contextSourceBoundsAndLifecycle();
  await importNavigationWorkflow(); await importAlternativesAndBounds(); await importReexportAliases(); await importResponseValidation(); await importStaleResponses(); await importTargetSourceGuards();
  process.stdout.write("PASS paired change roles, explicit replacement/context editing, no-model navigation, transactional mode refresh, catalogue limits, captured history citations, scope-aware declarations and explicit version-bound import-source routes.\n");
})().catch(error => { process.stderr.write(error.stack + "\n"); process.exitCode = 1; });
