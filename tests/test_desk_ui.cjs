"use strict";
// Optional dependency-free UI regression: node tests/test_desk_ui.cjs
const fs = require("node:fs"), path = require("node:path"), vm = require("node:vm"), assert = require("node:assert/strict");
const root = path.resolve(__dirname, ".."), app = fs.readFileSync(path.join(root, "src/forge8/web/app.js"), "utf8");
const html = fs.readFileSync(path.join(root, "src/forge8/web/index.html"), "utf8");
assert.match(html, /id="ask-project"/, "project questions need an explicit action separate from manual explain and locate");
assert.match(html, /手動最多 3 段，自動選材最多 6 段/);
assert.match(html, /240 行，兩者字元預算相同/);
assert.match(html, /id="traceback-locate"[^>]*type="button"/, "pasted traceback navigation must not submit a model question");
assert.match(html, /id="expand-definition"[^>]*type="button"[^>]*aria-describedby="definition-target"/);
assert.match(html, /id="experiment-from-call"[^>]*type="button"/, "carrying source literals must not submit an execution");
assert.match(html, /id="experiment-call-note"/);
assert.match(html, /id="continue-source"[^>]*type="button"/, "source continuation selection must not submit a model question");
assert.match(html, /id="replace-source"[^>]*type="button"/, "manual recovery must not submit a model question");
assert.match(html, /id="replace-source-note"[^>]*role="status"/);
assert.match(html, /id="continuation-clear"[^>]*type="button"/);
assert.match(html, /id="model-release"[^>]*type="button"/);
assert.match(html, /id="model-countdown"[^>]*aria-live="off"/, "countdown ticks must not become repeated live announcements");
for (const id of ["ai-discovery-actions", "reading-tools", "selection-budget", "traceback-tools"]) {
  const disclosure = html.match(new RegExp(`<details\\b[^>]*\\bid="${id}"[^>]*>`));
  assert(disclosure, `${id} uses native progressive disclosure`);
  assert(!/\bopen(?:\s|>|=)/.test(disclosure[0]), `${id} starts collapsed in the ordinary reader`);
}
assert(html.indexOf('id="answer"') < html.indexOf('id="reading-tools"'), "the answer must precede optional history/export tools");
for (const id of ["ask-project", "locate", "traceback-locate", "continue-source", "export-reading-note"])
  assert.match(html, new RegExp(`id="${id}"[^>]*type="button"`), "opening or using a secondary tool must not implicitly submit a form");
const questionForm = html.slice(html.indexOf('<form id="question-form"'), html.indexOf('</form>', html.indexOf('<form id="question-form"')));
assert.equal((questionForm.match(/<form\b/g) || []).length, 1, "progressive disclosures cannot introduce nested forms");
assert(questionForm.includes('id="ask" type="submit"'));
assert(html.includes("① 開啟檔案 → ② 選行並加入選段 → ③ 提問"));
class Node {
  constructor(tag = "") { this.tag = tag; this.nodeType = tag === "#text" ? 3 : 1; this.children = []; this.parentElement = null; this.listeners = {}; this.value = ""; this.dataset = {}; this._text = ""; this.scrollTop = this.scrollLeft = 0; this.classList = {toggle() {}}; }
  set textContent(value) { this.replaceChildren(); this._text = String(value); }
  get textContent() { return this._text + this.children.map(node => node.textContent).join(""); }
  get data() { return this._text; }
  set data(value) { this._text = value; }
  appendData(value) { this._text += value; this.appendCalls = (this.appendCalls || 0) + 1; }
  set innerHTML(_) { throw Error("HTML injection"); }
  append(...children) { for (const child of children.flatMap(node => node.tag === "fragment" ? [...node.children] : [node])) { child.remove(); child.parentElement = this; this.children.push(child); } }
  replaceChildren(...children) { for (const child of this.children) child.parentElement = null; this.children = []; this._text = ""; this.append(...children); }
  remove() { if (this.parentElement) { const siblings = this.parentElement.children; siblings.splice(siblings.indexOf(this), 1); this.parentElement = null; } }
  after(node) { if (this.parentElement) { node.remove(); node.parentElement = this.parentElement; const siblings = this.parentElement.children; siblings.splice(siblings.indexOf(this) + 1, 0, node); } }
  closest(selector) { for (let node = this; node; node = node.parentElement) if (selector.startsWith(".") ? (node.className || "").split(" ").includes(selector.slice(1)) : node.tag === selector) return node; return null; }
  contains(target) { return this === target || this.children.some(node => node.contains(target)); }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  setAttribute(name, value) { (this.attributes ||= {})[name] = String(value); }
  querySelectorAll(query) { return this.children.flatMap(node => [...((node.tag === query) || (query === "[data-line]" && node.dataset.line) || (query === "[data-experiment-entry]" && node.dataset.experimentEntry !== undefined) ? [node] : []), ...node.querySelectorAll(query)]); }
  querySelector(query) { const line = query.match(/data-line="(\d+)"/)?.[1]; return this.querySelectorAll("[data-line]").find(node => String(node.dataset.line) === line); }
  scrollIntoView() { this.scrollCalls = (this.scrollCalls || 0) + 1; }
  focus() { this.focusCalls = (this.focusCalls || 0) + 1; }
}
const answered = (id, text) => ({id, status: "answered", question: id === "previous" ? "PREVIOUS_QUESTION" : "NEW_QUESTION", result: {outcome: {answer: {claims: [{text, citations: []}]}}}});
function checkProgressiveDisclosure(context, nodes) {
  const run = source => vm.runInContext(source, context), originalFetch = context.fetch;
  let requests = 0;
  context.fetch = async () => { requests++; throw Error("disclosure cannot fetch or submit"); };
  try {
    run('showProject({name:"simple",version:"v1",reader:"qwen35",files:[{id:"0",path:"a.py",lines:2}],excluded:[]})');
    assert.equal(nodes["question-editor"].open, true, "the primary question editor is ready without another disclosure");
    for (const id of ["ai-discovery-actions", "reading-tools", "selection-budget", "traceback-tools"]) assert.equal(nodes[id].open, false);
    assert.equal(nodes["ai-discovery-actions"].hidden, false); assert.equal(nodes["traceback-tools"].hidden, false);
    nodes.question.value = "KEEP MY QUESTION";
    run('state.focus=[{file:"0",path:"a.py",start:1,end:2}]');
    const before = run('JSON.stringify([state.focus,state.job,state.history,state.experiment,$("question").value])');
    for (const id of ["ai-discovery-actions", "reading-tools", "selection-budget", "traceback-tools"]) nodes[id].open = true;
    run('controls();renderHistory();controls()');
    assert.equal(run('JSON.stringify([state.focus,state.job,state.history,state.experiment,$("question").value])'), before);
    for (const id of ["ai-discovery-actions", "reading-tools", "selection-budget", "traceback-tools"]) assert.equal(nodes[id].open, true, "poll/control rendering must preserve the user's open disclosure");
    nodes["reading-tools"].open = false;
    run('state.history=[{id:"kept",version:"v1",status:"incomplete",question:"Earlier question",elapsed_seconds:1}];renderHistory()');
    assert(nodes["reading-tools-summary"].textContent.includes("1 題")); assert.equal(nodes["reading-tools"].open, false);
    run('state.selectedHistory="kept";renderHistory()');
    assert(nodes["reading-tools-summary"].textContent.includes("正在回看"));
    run('state.historyError="History unavailable";renderHistory()');
    assert(nodes["reading-tools-summary"].textContent.includes("紀錄讀取失敗")); assert.equal(nodes["reading-tools"].open, false);
    run('showProject({name:"browse",version:"v1",browse_only:true,reader:null,experiments_enabled:false,files:[{id:"0",path:"a.py",lines:2}],excluded:[]})');
    assert.equal(nodes["traceback-tools"].open, true, "traceback remains a primary accessible action in no-AI mode");
    assert.equal(nodes["ai-discovery-actions"].hidden, true); assert.equal(nodes["ai-reading-results"].hidden, true);
    run('state.project.comparison={};renderReadingMode()');
    for (const id of ["ai-discovery-actions", "traceback-tools", "selection-budget"]) assert.equal(nodes[id].hidden, true, "comparison must not leave an empty unsupported tool disclosure");
    assert.equal(requests, 0, "disclosure and mode rendering never trigger network requests");
  } finally { context.fetch = originalFetch; }
}
async function checkReferenceFormats(context, nodes) {
  const reference = {evidence_id: "E1", path: "a.py", start_line: 10, end_line: 15};
  const claims = [{text: "", citations: [reference, {...reference, end_line: 10}]}];
  context.referenceJob = {id: "formats", status: "answered", version: "v1", files: [{id: "0", path: "a.py"}], result: {outcome: {answer: {claims}}}};
  const render = text => {
    claims[0].text = text; vm.runInContext("renderAnswer(referenceJob)", context);
    const paragraph = nodes.answer.children.find(node => node.className === "answer-prose");
    assert.equal(paragraph.textContent, text, "reference display must preserve the original model text");
    return paragraph;
  };
  const valid = ["[E1:L10-L15]", "[E1:10-15]", "[E1:L10]", "[E1:10]", "[E1:10-15]"];
  const paragraph = render("before " + valid.join("") + " after");
  const buttons = paragraph.querySelectorAll("button");
  assert.deepEqual(buttons.map(button => button.textContent), valid, "both exact forms, single lines, adjacent and repeated citations must stay clickable");
  let requests = 0;
  context.fetch = async route => {
    requests++; assert.equal(route, "/api/source?file=0&version=v1");
    return {ok: true, json: async () => ({path: "a.py", version: "v1", lines: Array.from({length: 20}, (_, i) => `line ${i + 1}`)})};
  };
  vm.runInContext("state.source = null", context);
  await buttons[0].listeners.click(); await buttons[1].listeners.click();
  const previews = paragraph.querySelectorAll("span").filter(node => node.className === "citation-wrap").map(node => node.children[1]);
  assert.equal(previews[0].textContent, previews[1].textContent, "canonical and bare references must open identical accepted metadata and exact source");
  assert.equal(previews[0].parentElement, buttons[0].parentElement, "ordinary prose excerpts remain beside their own citation");
  assert(previews[1].textContent.includes("a.py:10–15")); assert.equal(requests, 2);
  const contextToggle = previews[1].querySelectorAll("button").find(node => node.className.includes("source-context"));
  const exactCode = previews[1].querySelectorAll("pre")[0].textContent;
  const acceptedBefore = JSON.stringify(claims);
  contextToggle.listeners.click();
  const contextLines = previews[1].querySelectorAll("span").filter(node => node.className.includes("context-line"));
  assert.equal(contextLines.length, 20, "context must clamp at both file bounds");
  assert.equal(contextLines[0].dataset.contextLine, 1); assert.equal(contextLines.at(-1).dataset.contextLine, 20);
  assert.deepEqual(contextLines.filter(node => node.className.includes("cited-line")).map(node => node.dataset.contextLine), [10,11,12,13,14,15]);
  assert(previews[1].textContent.includes("不擴大引用或選段"));
  assert.equal(contextToggle.attributes["aria-pressed"], "true");
  assert.equal(JSON.stringify(claims), acceptedBefore); assert.equal(requests, 2, "context must reuse the already checked source, not fetch live content");
  contextToggle.listeners.click();
  assert.equal(previews[1].querySelectorAll("pre")[0].textContent, exactCode);
  assert.equal(contextToggle.attributes["aria-pressed"], "false");
  const invalid = ["[E1:L10-15]", "[E1:10-L15]", "[E1:010-15]", "[E1:L010-L15]", "[E1:10-015]", "[E01:10]", "[e1:10]", "[E1:l10]", "[E1:]", "[E1:-15]", "[E1:10-]", "[E1:10 -15]", "[E1:0]", "[E1:L0]", "[E1:15-10]", "[E1:10-16]", "[E2:10-15]"];
  assert.equal(render(invalid.join(" ")).querySelectorAll("button").length, 0, "malformed, reversed, unknown and unaccepted ranges must remain literal, never guessed");
  assert.deepEqual(render(invalid.join(" ") + " [E1:10] tail").querySelectorAll("button").map(button => button.textContent), ["[E1:10]"]);

  const natural = ["E1 L10-L15", "E1 L10", "E1\tL10-L15", "E1 L10-L15"];
  const naturalBody = render(`（${natural[0]}）依據${natural[1]}可知；(${natural[2]})，重複 ${natural[3]}。`);
  assert.deepEqual(naturalBody.querySelectorAll("button").map(button => button.textContent), natural);
  assert.equal(requests, 2, "rendering natural references makes no HTTP or inference request");
  const bracketed = ["[E1] L10-L15", "[E1] L10", "[E1]\tL10-L15", "[E1] L10-L15"];
  assert.deepEqual(render(bracketed.join("；") + "。L10-L15 保持原文。").querySelectorAll("button").map(button => button.textContent), bracketed);
  const naturalInvalid = ["E1 L10-15", "E1 10-L15", "E1 L010-L15", "E1 L10-L015", "E01 L10", "e1 L10", "E1 l10", "E1 L0", "E1 L15-L10", "E1 L10-L16", "E2 L10-L15", "E1\nL10", "E1 L10–L15", "E1 L10—L15", "E1 L10-L15-L20", "E1 L10 -L15", "E1 L10\n-L15", "E1 L10 -15", "E1 L10 -", "E1 L10.5", "E1 L10-L15.5", "XE1 L10", "_E1 L10", "E1 L10x", "E1 L10:L15", "[E1 L10]"];
  for (const text of naturalInvalid) assert.equal(render(text).querySelectorAll("button").length, 0, `natural reference must not guess a partial token: ${text}`);
  const bracketedInvalid = ["[E1 L10", "E1] L10", "[E01] L10", "[e1] L10", "[E1] l10", "[E1] L10-15", "[E1] L010-L15", "[E1] L10-L015", "[E1] L10-", "[E1] L10-L15x", "[E1] L10-L15-L20", "[E1]\nL10", "[E1] L10 -L15", "[E1] L10.5", "[E1] L10-L15.5", "[E1] L10–L15", "[E1] L10-L15000000", "[E2] L10", "[E1] L15-L10", "[E1] L10-L16", "[[E1] L10]", "[E1] L0"];
  for (const text of bracketedInvalid) assert.equal(render(text).querySelectorAll("button").length, 0, `bracketed ID must retain complete explicit coordinates: ${text}`);
  // Formatting removes blank lines and list markers; test the guard against
  // the original following text, even when matching metadata exists elsewhere.
  for (const text of ["E1 L10\n- L15", "E1 L10\n\n- 15", "[E1] L10\n- L15", "[E1] L10\n\n- 15"]) {
    claims[0].text = text; vm.runInContext("renderAnswer(referenceJob)", context);
    assert.equal(nodes.answer.children.find(node => node.className === "answer-prose").querySelectorAll("button").length, 0);
  }
  claims[0].text = "E1 L10\n- Next item"; vm.runInContext("renderAnswer(referenceJob)", context);
  assert.deepEqual(nodes.answer.children.find(node => node.className === "answer-prose").querySelectorAll("button").map(button => button.textContent), ["E1 L10"]);
  claims[0].text = "`E1 L10-L15` and `[E1] L10-L15`\n\n```text\nE1 L10\n[E1] L10\n```"; vm.runInContext("renderAnswer(referenceJob)", context);
  assert.equal(nodes.answer.children.find(node => node.className === "answer-prose").querySelectorAll("button").length, 0, "natural references in code remain inert");

  claims[0].citations.push({evidence_id: "E3", path: "src/requests/sessions.py", start_line: 490, end_line: 493});
  context.referenceJob.files.push({id: "1", path: "src/requests/sessions.py"});
  const actual = render("標頭合併（[E3] L490-L493）。"), actualButton = actual.querySelectorAll("button")[0];
  const rawBefore = JSON.stringify(claims), sourceFetch = context.fetch; let actualRequests = 0;
  context.fetch = async route => {
    actualRequests++; assert.equal(route, "/api/source?file=1&version=v1");
    return {ok: true, json: async () => ({path: "src/requests/sessions.py", version: "v1", lines: Array.from({length: 500}, (_, i) => `line ${i + 1}`)})};
  };
  await actualButton.listeners.click();
  const actualPreview = actual.querySelectorAll("span").find(node => node.className === "citation-preview");
  assert.equal(actualPreview.querySelectorAll("pre")[0].textContent, [490, 491, 492, 493].map(line => `${String(line).padStart(4)}│ line ${line}`).join("\n"));
  await actualButton.listeners.click(); await actualButton.listeners.click();
  assert.equal(actualRequests, 1, "natural references reuse the existing exact-version preview cache");
  assert.equal(JSON.stringify(claims), rawBefore, "natural-reference rendering must not rewrite model text or accepted metadata");
  context.fetch = sourceFetch;
}
async function checkAnswerFormatting(context, nodes) {
  const reference = {evidence_id: "E1", path: "a.py", start_line: 10, end_line: 15};
  const raw = ["## 路徑 [E1:10-15]", "", "### 細節", "**重點 `name` [E1:L10-L15]** 保留相鄰文字。", "", "- 第一項 `code`", "- 第二項 [E1:10-15]", "", "3. 三", "5. 五", "", "```python", "<script>alert(1)</script>", "**not bold** [E1:10-15]", "```", "", "| 項目 | 位置 | 說明 |", "| --- | --- | --- |", "| `name` | [E1:L10-L15] | **重點** |", "", '<img src=x onerror=alert(1)> [link](javascript:alert(1)) ![image](https://example.invalid/x)'].join("\n");
  context.formatted = {status: "answered", version: "v1", files: [{id: "0", path: "a.py"}], result: {outcome: {answer: {claims: [{text: raw, citations: [reference]}]}}}};
  const before = JSON.stringify(context.formatted);
  const render = text => {
    context.formatted.result.outcome.answer.claims[0].text = text;
    vm.runInContext("renderAnswer(formatted)", context);
    return nodes.answer.children.find(node => node.className === "answer-prose");
  };
  const body = render(raw);
  assert.equal(JSON.stringify(context.formatted), before, "formatting must never rewrite the model response or citation metadata");
  assert.equal(body.querySelectorAll("h4").length, 1); assert.equal(body.querySelectorAll("h5").length, 1);
  assert.equal(body.querySelectorAll("ul")[0].children.length, 2);
  assert.deepEqual(body.querySelectorAll("ol")[0].children.map(node => node.value), [3, 5], "numbered items retain the model's stated numbers");
  assert(body.querySelectorAll("strong")[0].textContent.includes("重點 name [E1:L10-L15]"));
  assert(body.textContent.includes("保留相鄰文字"));
  assert.equal(body.querySelectorAll("table").length, 1); assert.equal(body.querySelectorAll("th").length, 3); assert.equal(body.querySelectorAll("td").length, 3);
  const code = body.querySelectorAll("pre")[0];
  assert.equal(code.textContent, "<script>alert(1)</script>\n**not bold** [E1:10-15]");
  assert.equal(code.querySelectorAll("button").length, 0); assert.equal(code.querySelectorAll("strong").length, 0);
  for (const tag of ["script", "img", "a", "iframe", "style"]) assert.equal(body.querySelectorAll(tag).length, 0, `${tag} must never be created from model text`);
  assert(body.textContent.includes('<img src=x onerror=alert(1)> [link](javascript:alert(1)) ![image](https://example.invalid/x)'));
  assert.equal(body.querySelectorAll("button").length, 4, "only accepted references outside inert code are interactive, including bold/table/heading");
  const table = body.children.find(node => node.className === "answer-table"), tableButton = table.querySelectorAll("button")[0];
  let tableRequests = 0; const originalFetch = context.fetch;
  context.fetch = async (...args) => { tableRequests++; return originalFetch(...args); };
  const priorSource = vm.runInContext("state.source", context);
  await tableButton.listeners.click();
  const preview = body.children[body.children.indexOf(table) + 1];
  assert.equal(preview.className, "citation-preview"); assert.equal(preview.parentElement, body);
  assert.equal(table.querySelectorAll("span").filter(node => node.className === "citation-preview").length, 0, "table source excerpts must not occupy narrow cells");
  assert(preview.textContent.includes("a.py:10–15")); assert.equal(tableButton.attributes["aria-expanded"], "true");
  assert.equal(vm.runInContext("state.source", context), priorSource, "moving a table preview must not navigate away");
  await tableButton.listeners.click(); assert.equal(preview.hidden, true);
  await tableButton.listeners.click(); assert.equal(preview.hidden, false); assert.equal(tableRequests, 1);
  assert.equal(body.children[body.children.indexOf(table) + 1], preview, "reopening preserves placement and the same exact-source cache");
  context.formatted.version = "v2";
  const mismatch = render(raw); context.fetch = async () => ({ok: true, json: async () => ({path: "a.py", version: "v1", lines: ["WRONG_SNAPSHOT"]})});
  await mismatch.querySelectorAll("td")[1].querySelectorAll("button")[0].listeners.click();
  const errorPreview = mismatch.children.find(node => node.className === "citation-preview");
  assert(errorPreview.textContent.includes("版本或範圍不一致")); assert(!errorPreview.textContent.includes("WRONG_SNAPSHOT"));
  context.formatted.version = "v1"; context.fetch = originalFetch;
  const malformed = "unclosed `code and **bold\n***unsupported***\n``double`` [E1:L10-15]";
  assert.equal(render(malformed).textContent, malformed);
  assert.equal(render(malformed).querySelectorAll("button").length, 0);
  assert.equal(render("`[E1:10-15] **literal**`").querySelectorAll("button").length, 0, "inline code remains inert too");
  const unclosed = "```python\n<script>literal</script> [E1:10-15]";
  assert.equal(render(unclosed).textContent, unclosed); assert.equal(render(unclosed).querySelectorAll("button").length, 0);
  for (const table of ["| A | B |\n| -- | --- |\n| x | y |", "| A | B |\n| --- | --- |\n| x | y | z |", "| A | B | C |\n| --- | --- | --- |\n| `a|b` | c |", "| A | B |\n| --- | --- |\n| a\\|b | c |", "| A | B | C |\n| --- | --- | --- |\n| ``a|b`` | c |"] ) {
    const literal = render(table); assert.equal(literal.querySelectorAll("table").length, 0); assert.equal(literal.textContent, table, "ambiguous/malformed table must retain its full literal text");
  }
  for (const list of ["- parse\n  - convert parameter\n- invoke", "  - indented\n  - same indent", "- parse\n    - deeply nested\n- invoke", "- parse\n\t- tab nested\n- invoke", "- parse\n\n  - nested\n\n- invoke", "- parse\n1. convert\n- invoke", "* parse\n- invoke", "1. parse\n2) invoke"]) {
    const prose = render(list); assert.equal(prose.querySelectorAll("li").length, 0, "explicit nested/mixed markers must not become invented peer items");
    assert.equal(prose.children[0].className, "answer-list-prose"); assert.equal(prose.querySelectorAll("pre").length, 0);
    assert.equal(prose.textContent, list, "preserve every marker, indentation, blank line and original item order");
  }
  for (const list of ["- parse\n  continuation prose\n- invoke", "- parse\n\n  continuation after blank\n- invoke", "1.  parent\n    *   child\n        child continuation\n    parent continuation\n2.  next", "9. parent\n   aligned\n10. next\n    aligned"]) {
    const prose = render(list);
    assert.equal(prose.children[0].className, "answer-list-prose", "exact marker-content alignment is readable list prose");
    assert.equal(prose.querySelectorAll("pre").length, 0); assert.equal(prose.querySelectorAll("li").length, 0);
    assert.equal(prose.textContent, list, "preserve continuation indentation, blank lines, markers and numbers without inventing a list tree");
  }
  const aligned = "1.  **第一步**\n    正常說明 `name`。\n    *   [E1:L10-L15] 定義。\n    *   E1 L10-L15 分工。\n\n2.  **第二步**\n    說明內容。\n    *   [E1:10-15] 邊界。\n    *   [E1] L10-L15 原碼。\n\n3.  **第三步**\n    最後說明。\n    *   [E1:L10-L15] 解析。\n    *   [E1:10-15] 回傳。";
  let alignedRequests = 0;
  context.fetch = async route => {
    alignedRequests++; assert.equal(route, "/api/source?file=0&version=v1");
    return {ok: true, json: async () => ({path: "a.py", version: "v1", lines: Array.from({length: 20}, (_, i) => `line ${i + 1}`)})};
  };
  const alignedBody = render(aligned), alignedButtons = alignedBody.querySelectorAll("button");
  assert.equal(alignedBody.querySelectorAll("pre").length, 0);
  assert.equal(alignedBody.textContent, aligned.replaceAll("**", "").replaceAll("`", ""));
  assert.equal(alignedBody.querySelectorAll("strong").length, 3); assert.equal(alignedBody.querySelectorAll("code").length, 1);
  assert.deepEqual(alignedButtons.map(button => button.textContent), ["[E1:L10-L15]", "E1 L10-L15", "[E1:10-15]", "[E1] L10-L15", "[E1:L10-L15]", "[E1:10-15]"]);
  assert.equal(alignedRequests, 0, "rendering aligned prose must not make HTTP/inference calls");
  assert.equal(context.formatted.result.outcome.answer.claims[0].text, aligned);
  assert.deepEqual(context.formatted.result.outcome.answer.claims[0].citations, [reference]);
  await alignedButtons[0].listeners.click();
  assert.equal(alignedRequests, 1);
  assert.equal(alignedBody.querySelectorAll("pre")[0].textContent, [10,11,12,13,14,15].map(line => `${String(line).padStart(4)}│ line ${line}`).join("\n"));
  context.fetch = originalFetch;
  const hostile = render("1.  entry\n    <script>alert(1)</script><img src=x onerror=alert(1)> `E1 L10-L15` [E2:10-15] [E1:L10-15]\n    *   [E1:10-15]");
  assert.equal(hostile.querySelectorAll("script").length, 0); assert.equal(hostile.querySelectorAll("img").length, 0);
  assert.equal(hostile.querySelectorAll("code")[0].textContent, "E1 L10-L15");
  assert.deepEqual(hostile.querySelectorAll("button").map(button => button.textContent), ["[E1:10-15]"]);
  for (const list of ["- parse\n  ## nested heading\n- invoke", "- parse\nwrapped continuation\n- invoke", "- parse\n    print('E1 L10-L15')\n- invoke", "- parse\n    ```python\n    - E1 L10-L15\n    ```\n- invoke", "- parse\n    ~~~text\n    - E1 L10-L15\n    ~~~\n- invoke", "- parse\n  - ``` E1 L10-L15\n- invoke", "1.  parse\n        print('E1 L10-L15')", "1.  parse\n   misaligned E1 L10-L15", "1.  parse\n\tcontinuation E1 L10-L15", "1.  parse\n    ## nested E1 L10-L15", "1.  parse\n    > quote E1 L10-L15", "1.  parse\n    | table | E1 L10-L15 |", "1.  parse\n    - child\n     misaligned E1 L10-L15", "1.     print('code')\n       E1 L10-L15", "  - root\n    continuation E1 L10-L15"]) {
    const literal = render(list); assert.equal(literal.querySelectorAll("li").length, 0, "ambiguous list regions must not become invented peer items");
    assert.equal(literal.querySelectorAll("pre").length, 1); assert.equal(literal.textContent, list, "preserve the complete list region, indentation and continuation lines");
    assert.equal(literal.querySelectorAll("button").length, 0, "fenced/unfenced code and ambiguous continuations remain inert");
  }
  const nested = "*   **第一層**\n    1.  查原碼（E1 L10-L15）。\n    *   `E1 L10-L15`\n\n*   **第二層**\n    *   <img src=x onerror=alert(1)> [E1:10-15]";
  const nestedBody = render(nested);
  assert.equal(nestedBody.textContent, nested.replaceAll("**", "").replaceAll("`", ""));
  assert.equal(nestedBody.querySelectorAll("strong").length, 2); assert.equal(nestedBody.querySelectorAll("code").length, 1);
  assert.deepEqual(nestedBody.querySelectorAll("button").map(button => button.textContent), ["E1 L10-L15", "[E1:10-15]"]);
  assert.equal(nestedBody.querySelectorAll("img").length, 0); assert.equal(nestedBody.querySelectorAll("li").length, 0);
  for (const list of ["- parse\n- invoke", "* parse\n* invoke", "+ parse\n+ invoke"]) assert.equal(render(list).querySelectorAll("ul")[0].children.length, 2);
  assert.deepEqual(render("3) parse\n5) invoke").querySelectorAll("ol")[0].children.map(node => node.value), [3, 5]);
  const separate = render("- parse\n- invoke\n## Next block\nparagraph");
  assert.equal(separate.querySelectorAll("li").length, 2); assert.equal(separate.querySelectorAll("h4").length, 1);
  const long = Array.from({length: 40}, (_, i) => `### 段落 ${i}\n說明 **重點** [E1:10-15]。`).join("\n\n");
  assert.equal(render(long).querySelectorAll("h5").length, 40); assert.equal(render(long).querySelectorAll("button").length, 40);
}
async function checkStructuredReadingDelivery(context, nodes) {
  const run = code => vm.runInContext(code, context), originalFetch = context.fetch, requests = [];
  const files = [{id: "0", path: "a.py", lines: 3}, {id: "1", path: "b.py", lines: 1}];
  const citations = [{evidence_id: "E1", path: "a.py", start_line: 1, end_line: 2},
    {evidence_id: "E3", path: "b.py", start_line: 1, end_line: 1}];
  const raw = '模型解釋 [E1:L1-L2]。未列入引用的子範圍 [E1:L1]、擴大範圍 [E1:L1-L3]、已讀但未引用 E2 L1、未知 [E99:L1]。<img src=x onerror=alert(1)>';
  const job = {id: "structured-delivery", kind: "explain", status: "answered", version: "delivery-v1", files,
    question: "本題原始問題", elapsed_seconds: 20, gpu: "released",
    result: {kind: "forge8.explain", status: "answered", ok: true,
      outcome: {status: "answered", ok: true, source_unchanged: true, snapshot_unchanged: true,
        acceptance: {ok: true}, semantic_claims_verified: false,
        inference: {reading_recipe: {format: "structured_v1", reasoning_budget_tokens: 2048, reasoning_budget_scope: "per_thinking_block"}},
        evidence: [{evidence_id: "E2", path: "a.py", start_line: 1, end_line: 3}],
        answer: {format: "cited_prose", citation_scope: "document_references_only", claims: [{type: "inference", text: raw, citations}]}}}};
  context.deliveryJob = job; context.deliveryFiles = files;
  context.fetch = async (route, options) => {
    requests.push({route, method: options.method});
    assert.equal(route, "/api/source?file=1&version=delivery-v1"); assert.equal(options.method, "GET");
    return {ok: true, json: async () => ({version: "delivery-v1", path: "b.py", lines: ["RETURNED_SOURCE_ONLY"]})};
  };
  run('showProject({name:"delivery",version:"delivery-v1",files:deliveryFiles,excluded:[],reader:"qwen35"}); state.focus=[{file:"0",path:"a.py",start:1,end:2}]');
  nodes.question.value = "下一題草稿，不得送出";
  const original = JSON.stringify(job), focus = run("JSON.stringify(state.focus)");
  run('renderJob({id:deliveryJob.id,kind:"explain",status:"running",version:deliveryJob.version,question:deliveryJob.question,preview:deliveryJob.result.outcome.answer.claims[0].text})');
  assert(nodes.answer.textContent.includes(raw)); assert.equal(nodes.answer.querySelectorAll("button").length, 0, "decoded streaming text never authorizes a citation");
  run("renderJob(deliveryJob)");
  const body = nodes.answer.children.find(node => node.className === "answer-prose");
  const heading = nodes.answer.children.find(node => node.tag === "h3");
  assert.equal(heading.textContent, "模型解讀 · 來源可核對，內容未驗證");
  assert(nodes.answer.children.indexOf(heading) < nodes.answer.children.indexOf(body), "the semantic warning is visible before the final answer, not only after a long response");
  assert.equal(body.textContent, raw, "decoded final prose is not repaired or supplemented");
  assert.deepEqual(body.querySelectorAll("button").map(button => button.textContent), ["[E1:L1-L2]"], "only exact final citation-array members become inline links, not evidence IDs or subranges");
  assert.equal(body.querySelectorAll("img").length, 0); assert.equal(body.querySelectorAll("a").length, 0);
  assert.equal(run("draft"), null); assert(!nodes.answer.children.some(node => node.className === "draft-label"));
  const references = nodes.answer.querySelectorAll("button").filter(button => button.className === "citation reference");
  assert.deepEqual(references.map(button => button.textContent), ["a.py:1–2", "b.py:1–1"], "a validated array citation stays accessible even if the model never mentions its ID");
  assert.equal(requests.length, 0, "rendering must not fetch source, infer, or execute");
  await references[1].listeners.click();
  assert(references[1].parentElement.textContent.includes("   1│ RETURNED_SOURCE_ONLY"));
  assert.equal(requests.length, 1);
  run('state.history=[deliveryJob]; state.selectedHistory=deliveryJob.id; state.rendered=""; renderViewedAnswer()');
  assert.equal(nodes.answer.querySelectorAll("h3")[0].textContent, heading.textContent, "history retains the same interpretation boundary");
  assert.equal(nodes.answer.children.find(node => node.className === "answer-prose").textContent, raw);
  assert.equal(requests.length, 1, "history viewing does not automatically refetch citations or ask another question");
  assert.equal(nodes.question.value, "下一題草稿，不得送出"); assert.equal(run("JSON.stringify(state.focus)"), focus);
  assert.equal(JSON.stringify(job), original, "rendering and navigation cannot alter the accepted text, citations, recipe or semantic flag");
  run('state.selectedHistory=null; renderJob({id:"cancelled-delivery",status:"cancelled",preview:deliveryJob.result.outcome.answer.claims[0].text})');
  assert(!nodes.answer.textContent.includes(raw)); assert.equal(nodes.answer.querySelectorAll("button").length, 0);
  // Legacy quote/inference answers retain their existing heading and renderer.
  run('renderAnswer({status:"answered",result:{outcome:{answer:{claims:[{type:"inference",text:"LEGACY",citations:[]}]}}}})');
  assert.equal(nodes.answer.querySelectorAll("h3")[0].textContent, "模型解讀");
  context.fetch = originalFetch;
}
function checkTouchSelection(context, nodes) {
  const run = source => vm.runInContext(source, context), extend = nodes["extend-selection"];
  const gutter = (line, shiftKey = false) => nodes.code.querySelector(`[data-line="${line}"]`).children[0].listeners.click({shiftKey});
  run("state.focus = []; state.anchor = state.end = null; state.page = 0; renderCode(); controls()");
  assert.equal(extend.disabled, true); extend.listeners.click(); assert.equal(run("state.extending"), false);
  gutter(195); extend.listeners.click();
  assert.equal(extend.attributes["aria-pressed"], "true"); assert(nodes["selection-hint"].textContent.includes("從 L195 延伸"));
  nodes["page-next"].listeners.click(); assert.equal(run("state.extending"), true, "paging must retain the armed anchor");
  gutter(208); assert.equal(run("state.anchor"), 195); assert.equal(run("state.end"), 208);
  assert.equal(extend.attributes["aria-pressed"], "false"); assert.equal(run("state.focus.length"), 0);
  nodes["add-selection"].listeners.click(); assert.equal(run("state.focus[0].end"), 208);
  gutter(210); assert.equal(run("state.anchor"), 210, "next ordinary tap starts a new selection");
  extend.listeners.click(); extend.listeners.click(); gutter(220);
  assert.equal(run("state.anchor"), 220, "second button tap cancels extension");
  extend.listeners.click(); gutter(300);
  assert.equal(run("state.anchor"), 220); assert.equal(run("state.end"), 300);
  assert.equal(nodes["add-selection"].disabled, false); assert(nodes["range-label"].textContent.includes("81 行"));
  assert.equal(nodes["add-selection"].textContent, "完整加入 2 段");
  gutter(301, true); assert.equal(run("state.anchor"), 220, "Shift extension stays available");
  for (const setup of ['state.job = {status:"running"}', 'state.refreshing = true', 'state.source.version = "older"']) {
    extend.listeners.click(); assert.equal(run("state.extending"), true);
    run(`${setup}; controls()`); assert.equal(extend.disabled, true); assert.equal(run("state.extending"), false);
    extend.listeners.click(); assert.equal(run("state.extending"), false);
    run('state.job = null; state.refreshing = false; state.source.version = "v1"; controls()');
  }
  extend.listeners.click(); nodes["outline-list"].querySelectorAll("button")[0].listeners.click();
  assert.equal(run("state.extending"), false, "definition picks reset the one-shot gesture");
}
async function checkOutline(context, nodes) {
  const run = source => vm.runInContext(source, context);
  const definition = (name, start, line, end, stub = false) => ({name, kind: "function", start_line: start, definition_line: line, end_line: end, stub});
  const items = [definition("Context.invoke", 2, 3, 4, true), definition("Context.invoke", 195, 198, 208), definition("Command.invoke", 310, 310, 320), {...definition("Context", 20, 20, 110), kind: "class"}];
  const source = {path: "a.py", version: "v1", lines: Array.from({length: 450}, (_, i) => `line ${i + 1}`), outline: {status: "available", items}};
  let requests = 0;
  context.fetch = async route => { requests++; assert(route.startsWith("/api/source?")); return {ok: true, json: async () => source}; };
  run('showProject({name:"test",version:"v1",files:[{id:"0",path:"a.py",lines:450},{id:"1",path:"b.txt",lines:1}],excluded:[]})');
  await run('openFile(state.project.files[0], "v1")');
  const buttons = () => nodes["outline-list"].querySelectorAll("button").filter(button => button.dataset.definitionLine !== undefined);
  assert.equal(nodes.outline.hidden, false); assert.equal(nodes.outline.open, false);
  assert.equal(buttons().length, 4); assert(nodes["outline-note"].textContent.includes("不代表實際呼叫關係"));
  nodes["outline-filter"].value = "context.INVOKE"; nodes["outline-filter"].listeners.input();
  assert.equal(buttons().length, 2, "qualified names and repeated stubs remain distinguishable");
  assert(buttons()[0].textContent.includes("省略內容")); assert(!buttons()[1].textContent.includes("省略內容"));
  assert.equal(buttons()[1].dataset.definitionLine, 198); assert.equal(buttons()[1].dataset.startLine, 195);
  assert(buttons()[1].textContent.includes("L195–L208（14 行）"));
  nodes.outline.open = true; buttons()[1].listeners.click();
  assert.equal(nodes.outline.open, false); assert.equal(run("state.anchor"), 195); assert.equal(run("state.end"), 208);
  assert.equal(run("state.page"), 0, "navigation includes decorators, even across a source page boundary");
  assert.equal(run("state.focus.length"), 0, "definition navigation must not add a selection");
  nodes["add-selection"].listeners.click(); assert.equal(run("state.focus.length"), 1);
  assert.equal(run("state.focus[0].start"), 195); assert.equal(run("state.focus[0].end"), 208);
  nodes["outline-filter"].value = ""; nodes["outline-filter"].listeners.input();
  const staleButton = buttons()[2]; buttons()[3].listeners.click();
  assert.equal(run("state.anchor"), 20); assert.equal(run("state.end"), 110); assert.equal(nodes["add-selection"].disabled, false);
  assert(nodes["range-label"].textContent.includes("L20–L110 · 91 行"));
  assert(nodes["range-label"].textContent.includes("完整加入 2 段"), "packable definitions select all lines, never a clipped prefix");
  for (const [line, shiftKey] of [[20, false], [30, true]]) nodes.code.querySelector(`[data-line="${line}"]`).children[0].listeners.click({shiftKey});
  assert.equal(run("state.rangeHint"), ""); nodes["add-selection"].listeners.click(); assert.equal(run("state.focus[1].end"), 30);
  buttons()[0].listeners.click(); nodes["add-selection"].listeners.click(); assert.equal(run("state.focus.length"), 3);
  buttons()[2].listeners.click(); assert.equal(nodes["add-selection"].disabled, true, "three-range limit still applies");
  assert.equal(requests, 1, "outline filtering/navigation uses only already loaded source, never jobs or another HTTP request");
  checkTouchSelection(context, nodes);
  nodes["outline-filter"].value = "no match"; nodes["outline-filter"].listeners.input();
  assert.equal(buttons().length, 0); assert(nodes["outline-list"].textContent.includes("沒有符合"));
  nodes["outline-filter"].value = "";
  for (const status of ["available", "unavailable", "limited", "unsupported"]) {
    context.outline = {status, items: [], reason: "syntax_or_version"}; run("state.source.outline = outline; renderOutline()");
    assert.equal(nodes.outline.hidden, status === "unsupported"); assert.equal(buttons().length, 0);
    if (status !== "unsupported") {
      assert.equal(nodes["outline-filter"].hidden, true); assert.equal(nodes["outline-filter-label"].hidden, true);
      assert(nodes["outline-note"].textContent.includes("文字搜尋"), "unavailable outlines preserve ordinary source/search fallback");
    }
  }
  source.outline = {status: "available", items}; await run('openFile(state.project.files[0], "v1")');
  run("state.anchor = state.end = 1; controls()"); nodes["extend-selection"].listeners.click();
  context.fetch = async () => { throw Error("source unavailable"); };
  const failed = run('openFile(state.project.files[1], "v1")');
  assert.equal(nodes.outline.hidden, true); assert.equal(buttons().length, 0, "clear stale metadata while the next source is loading");
  assert.equal(run("state.extending"), false); assert.equal(nodes["extend-selection"].disabled, true);
  await failed; staleButton.listeners.click();
  assert.equal(nodes.outline.hidden, true); assert.equal(run("state.anchor"), null); assert.equal(run("state.source"), null);
  context.fetch = async () => ({ok: true, json: async () => source}); await run('openFile(state.project.files[0], "v1")');
  assert.equal(nodes.outline.hidden, false);
  run("state.anchor = state.end = 1; controls()"); nodes["extend-selection"].listeners.click();
  run('showProject({...state.project, version:"v2"})');
  assert.equal(run("state.extending"), false); assert.equal(nodes["extend-selection"].disabled, true);
  assert.equal(nodes.outline.hidden, true); assert.equal(buttons().length, 0); assert.equal(nodes["outline-filter"].value, "");
  for (const phase of ["local model final synthesis; 3 citable source span(s) retained", "grounded answer accepted; preparing safe shutdown", "stopping the local model and proving GPU/server release", "GPU/server release proven; sealing the evidence bundle"]) {
    context.phase = phase; run('renderJob({id:"phase",status:"running",phase})');
    assert.notEqual(nodes.phase.textContent, phase, "actual synthesis and shutdown phases should be readable Chinese");
  }
}
async function checkDefinitionSearch(context, nodes) {
  const run = code => vm.runInContext(code, context), query = nodes["search-query"], mode = nodes["search-mode"];
  const files = [{id: "0", path: "a.py", lines: 450}, {id: "1", path: "b.py", lines: 450}];
  const item = (file, name, start, end, stub = false) => ({file, path: files[Number(file)].path, name, kind: "function", start_line: start, definition_line: start, end_line: end, stub});
  const matches = [item("0", "Context.invoke", 2, 4, true), item("0", "Context.invoke", 195, 208), item("1", "Command.invoke", 20, 110)];
  const response = {version: "v1", matches, truncated: true, unavailable_files: 2}, requests = [];
  const source = file => ({path: files[Number(file)].path, version: "v1", lines: Array.from({length: 450}, (_, i) => `line ${i + 1}`), outline: {status: "available", items: []}});
  const fetch = async route => {
    requests.push(route);
    if (route.startsWith("/api/definitions?")) return {ok: true, json: async () => response};
    if (route.startsWith("/api/source?")) return {ok: true, json: async () => source(new URLSearchParams(route.split("?")[1]).get("file"))};
    assert.equal(route, "/api/search?q=invoke"); return {ok: true, json: async () => ({matches: [{file: "0", path: "a.py", line: 9, text: "invoke literal"}], truncated: false})};
  };
  context.files = files; context.fetch = fetch;
  run('showProject({name:"definitions",version:"v1",files,excluded:[]})');
  const search = async (name = "invoke", kind = "definitions") => { query.value = name; mode.value = kind; mode.listeners.change(); await nodes["search-form"].listeners.submit({preventDefault() {}}); };
  const buttons = () => nodes["search-results"].querySelectorAll("button");
  await search(); assert.equal(buttons().length, 3);
  assert.equal(nodes["search-summary"].scrollCalls, undefined, "manual search does not unexpectedly scroll");
  assert(requests[0].includes("q=invoke&version=v1")); assert(nodes["search-summary"].textContent.includes("不代表實際呼叫"));
  assert(nodes["search-summary"].textContent.includes("2 個 Python")); assert(nodes["search-summary"].textContent.includes("已達上限"));
  assert(buttons()[0].textContent.includes("省略內容")); assert(buttons()[0].textContent.includes("Context.invoke · a.py")); assert(buttons()[0].textContent.includes("函式"));
  const staleButton = buttons()[0]; await buttons()[1].listeners.click();
  assert.equal(run("state.anchor"), 195); assert.equal(run("state.end"), 208); assert.equal(run("state.focus.length"), 0);
  const priorRequests = requests.length; await buttons()[0].listeners.click();
  assert.equal(requests.length, priorRequests, "same-file definition selection reuses loaded source"); assert.equal(run("state.anchor"), 2);
  await buttons()[2].listeners.click(); assert.equal(run("state.source.path"), "b.py"); assert.equal(run("state.anchor"), 20); assert.equal(run("state.end"), 110);
  assert(nodes["range-label"].textContent.includes("91 行")); assert.equal(nodes["add-selection"].disabled, false);
  assert.equal(run("state.focus.length"), 0, "neither valid nor oversized definitions auto-add evidence");
  run('state.focus = [{file:"0",path:"a.py",start:1,end:2}]; state.job = {id:"in-flight",status:"running",question:"CURRENT_QUESTION"}; renderSelections()');
  const captured = run("JSON.stringify({job:state.job,focus:state.focus})");
  assert.equal(buttons()[0].disabled, false); assert.equal(nodes["find-selection"].disabled, false);
  await buttons()[1].listeners.click();
  assert.equal(run("state.source.path"), "a.py"); assert.equal(run("state.anchor"), 195); assert.equal(run("state.end"), 208);
  assert.equal(nodes["add-selection"].disabled, true, "navigation while answering cannot add evidence to the current question");
  nodes["add-selection"].listeners.click();
  assert.equal(nodes.selections.querySelectorAll("button")[0].disabled, true, "captured focus cannot be removed while answering");
  for (const flag of ["pending", "refreshing"]) {
    run(`state.${flag} = true; controls()`);
    assert.equal(buttons()[0].disabled, true); assert.equal(nodes["find-selection"].disabled, true);
    await buttons()[0].listeners.click(); assert.equal(run("state.anchor"), 195);
    run(`state.${flag} = false; controls()`);
  }
  const code = nodes.code.querySelectorAll("code")[0], codeText = context.document.createTextNode("Context.invoke"); code.append(codeText);
  const selection = (text, start = code, end = start) => { context.document.getSelection = () => ({rangeCount: 1, toString: () => text, getRangeAt: () => ({startContainer: start, endContainer: end})}); };
  let prevented = false; nodes["find-selection"].listeners.pointerdown({preventDefault() {prevented = true;}}); assert(prevented);
  selection("Context.invoke", codeText); await nodes["find-selection"].listeners.click();
  assert.equal(nodes["search-summary"].scrollCalls, 1, "successful source shortcut reveals its results, including on mobile");
  assert.equal(query.value, "Context.invoke"); assert.equal(mode.value, "definitions");
  assert(requests.at(-1).includes("q=Context.invoke&version=v1"));
  selection("讀取.名稱"); await nodes["find-selection"].listeners.click(); assert.equal(query.value, "讀取.名稱");
  for (const [name, start, end] of [["invoke", nodes.answer, nodes.answer], ["invoke", code, nodes.answer], ["invoke()", code, code], ["x".repeat(129), code, code], ["", code, code]]) {
    const before = requests.length; selection(name, start, end); await nodes["find-selection"].listeners.click();
    assert.equal(requests.length, before); assert(nodes.error.textContent.includes("目前原碼內反白"));
  }
  assert.equal(run("JSON.stringify({job:state.job,focus:state.focus})"), captured, "definition navigation and source-name searches preserve the in-flight question and evidence");
  assert.equal(nodes["add-selection"].disabled, true); assert.equal(nodes.selections.querySelectorAll("button")[0].disabled, true);
  run("state.job = null; state.focus = []; renderSelections()");
  await search("invoke", "text"); assert.equal(buttons().length, 1); assert(buttons()[0].textContent.includes("invoke literal"));
  await buttons()[0].listeners.click(); assert.equal(run("state.anchor"), 9, "literal search retains line navigation");
  query.value = ""; query.listeners.input(); assert.equal(buttons().length, 0);
  const beforeEmpty = requests.length; await nodes["search-form"].listeners.submit({preventDefault() {}}); assert.equal(requests.length, beforeEmpty);
  await search(); context.fetch = async () => { throw Error("definition service unavailable"); };
  const beforeFailure = nodes["search-summary"].scrollCalls;
  await run("searchSource(true)"); assert.equal(buttons().length, 0); assert(nodes.error.textContent.includes("unavailable"));
  assert.equal(nodes["search-summary"].scrollCalls, beforeFailure, "failed shortcut search must not move the viewport");
  context.fetch = async () => ({ok: true, json: async () => ({...response, matches: [], truncated: false})});
  await search(); assert.equal(buttons().length, 0); assert(nodes["search-summary"].textContent.includes("0 個同名定義"));
  context.fetch = async () => ({ok: true, json: async () => ({...response, version: "wrong"})});
  await search(); assert.equal(buttons().length, 0); assert(nodes.error.textContent.includes("版本不一致"));
  let finish; context.fetch = () => new Promise(resolve => {finish = resolve;});
  const scrolled = nodes["search-summary"].scrollCalls;
  const pending = run("searchSource(true)"); query.value = ""; query.listeners.input(); finish({ok: true, json: async () => response}); await pending;
  assert.equal(buttons().length, 0, "late response cannot restore cleared-query results");
  assert.equal(nodes["search-summary"].scrollCalls, scrolled, "stale search must not move the viewport");
  const refreshing = search(); run('showProject({name:"new",version:"v2",files,excluded:[]})');
  finish({ok: true, json: async () => response}); await refreshing; await staleButton.listeners.click();
  assert.equal(buttons().length, 0); assert.equal(run("state.source"), null); assert.equal(run("state.anchor"), null);
  assert.equal(nodes["find-selection"].disabled, true, "refresh invalidates source selection and outstanding definition candidates");
  assert(requests.every(route => !route.startsWith("/api/jobs")), "definition navigation never starts inference");
}
async function checkRejectedPreview(context, nodes) {
  const run = code => vm.runInContext(code, context), originalFetch = context.fetch;
  const excerpt = "REJECTED_TEXT\n<img onerror=alert(1)> **not bold** [E1-L3] [E1:L1]\n```python\n\tcall()\n```";
  const failure = {id: "diagnostic", status: "incomplete", gpu: "released", question: "FIRST_QUESTION", rejected_preview: excerpt,
    result: {status: "stalled", ok: false, outcome: {status: "stalled", ok: false, answer: null, failure_reason: "invalid citations"}}};
  const details = () => nodes.answer.querySelectorAll("details").find(node => node.className === "rejected-preview");
  const emit = job => { context.diagnosticJob = job; run('state.rendered = ""; renderJob(diagnosticJob)'); };
  const draftQuestion = nodes.question.value, focus = run("JSON.stringify(state.focus)"), requests = [];
  context.fetch = async route => { requests.push(route); throw Error("unexpected diagnostic request"); };
  emit(failure);
  assert.equal(Boolean(details().open), false); assert.equal(details().children[0].textContent, "查看未採用的輸出節錄");
  assert.equal(details().children[2].textContent, excerpt);
  assert(details().textContent.includes("12,000")); assert(details().textContent.includes("可能不完整或不正確"));
  for (const tag of ["button", "img", "code", "strong", "a"]) assert.equal(details().querySelectorAll(tag).length, 0, "unaccepted excerpts never invoke HTML/Markdown/citation rendering");
  assert(nodes.answer.textContent.includes("invalid citations")); assert.equal(nodes["status-label"].textContent, "本題未完成");
  assert.equal(nodes["question-editor"].open, true); assert.equal(nodes.question.value, draftQuestion); assert.equal(run("JSON.stringify(state.focus)"), focus);
  const expanded = details(); expanded.open = true; run("renderJob(diagnosticJob)");
  assert.equal(details(), expanded); assert.equal(details().open, true, "unchanged status must not re-collapse an inspected excerpt");
  run('renderJob({...diagnosticJob, status: "unknown"})');
  assert.equal(details(), undefined); assert.equal(nodes["status-label"].textContent, "等待確認本機狀態");
  run("renderJob(diagnosticJob)");
  assert(details(), "reconnecting to the same incomplete job must restore its diagnostic without resetting render state in the test");
  assert.notEqual(details(), expanded); assert.equal(Boolean(details().open), false, "restored diagnostics remain collapsed");
  assert.equal(details().children[2].textContent, excerpt); assert.equal(nodes["status-label"].textContent, "本題未完成");
  assert(nodes.answer.textContent.includes("invalid citations")); assert.equal(nodes.question.value, draftQuestion);
  assert.equal(run("JSON.stringify(state.focus)"), focus);
  for (const status of ["running", "cancelling", "cancelled", "unknown", "answered", "idle"]) {
    emit(failure); emit({...failure, status}); assert.equal(details(), undefined, `${status} cannot retain a previous diagnostic`);
  }
  for (const rejected_preview of [undefined, null, 42, "", " \n\t", "x".repeat(12001)]) {
    emit({...failure, rejected_preview}); assert.equal(details(), undefined);
  }
  emit({...failure, rejected_preview: "😀".repeat(12000)});
  assert.equal(details().children[2].textContent, "😀".repeat(12000), "the Python preview cap counts Unicode codepoints, not UTF-16 units");
  for (const change of [{gpu: "unknown"}, {gpu: "not_acquired"}, {error: "worker failed"}, {result: null},
      {result: {...failure.result, ok: true}}, {result: {...failure.result, status: "backend_error"}},
      ...[{status: "source_drift"}, {ok: true}, {answer: {claims: []}}].map(change => ({result: {...failure.result, outcome: {...failure.result.outcome, ...change}}}))]) {
    emit({...failure, ...change}); assert.equal(details(), undefined, "inconsistent failure metadata cannot expose a diagnostic");
  }
  assert.deepEqual(requests, [], "rendering and expanding diagnostics never request source, trace, or another inference");
  emit(failure); nodes.question.value = "SECOND_QUESTION"; nodes.question.listeners.input();
  context.fetch = async route => {
    requests.push(route);
    if (route === "/api/jobs") return {ok: false, status: 409, json: async () => ({error: "stale source; refresh before asking"})};
    assert.equal(route, "/api/jobs/current"); return {ok: true, json: async () => failure};
  };
  await nodes["question-form"].listeners.submit({preventDefault() {}}); await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(requests, ["/api/jobs", "/api/jobs/current"], "only explicit Q2 submission adds one POST");
  assert.equal(details(), undefined); assert(!nodes.answer.textContent.includes("REJECTED_TEXT"), "rejected Q2 cannot redisplay Q1's diagnostic");
  assert.equal(nodes.question.value, "SECOND_QUESTION"); assert.equal(nodes["question-editor"].open, true);
  emit(failure); emit({id: "new-running-job", status: "running"}); assert.equal(details(), undefined);
  emit(failure); run('showProject({...state.project, version:"refreshed"})'); assert.equal(details(), undefined);
  assert(!nodes.answer.textContent.includes("REJECTED_TEXT")); context.fetch = originalFetch;
}
async function checkObservedPaths(context, nodes) {
  const run = code => vm.runInContext(code, context), originalFetch = context.fetch, requests = [];
  const call = {id: 1, parent: null, file: "0", path: "a.py", function: "same", first_line: 1, visits: [2, 3, 2, 201], returned: true, exceptions: 0};
  context.observedProject = {name: "paths", version: "observed", files: [{id: "0", path: "a.py", lines: 250}], excluded: [],
    observation: {test: "test_a.C.test_one", python: "3.10.11", test_passed: false, counts: {failed: 1}, calls: [call,
      {...call, id: 2, parent: 1, parent_line: 3, visits: [2, 3, 2, 202], exceptions: 1},
      {...call, id: 3, visits: [2, 3, 2, 201]},
      {...call, id: 4, first_line: 100}, {...call, id: 5, path: "other.py"},
      {...call, id: 6, function: "<script>literal</script>"}, {...call, id: 7, visits: [2, 3]}]}};
  context.fetch = async route => {
    requests.push(route); assert.equal(route, "/api/source?file=0&version=observed");
    return {ok: true, json: async () => ({path: "a.py", version: "observed", lines: Array.from({length: 250}, (_, i) => `line ${i + 1}`)})};
  };
  run('showProject(observedProject); state.focus = [{file:"0",path:"a.py",start:1,end:2}]; renderSelections()');
  nodes.question.value = "KEEP_QUESTION";
  assert.equal(nodes.observation.hidden, false); assert.equal(nodes.observation.open, true);
  assert(nodes["observation-status"].textContent.includes("捕捉完整 · 測試未通過／跳過"));
  assert(nodes["observation-status"].textContent.includes("失敗 1"));
  assert.equal(nodes["observed-call"].children.length, 7);
  assert.equal(nodes["observed-call"].querySelectorAll("script").length, 0);
  assert.equal(nodes["observed-call"].children[5].textContent, "#6 · <script>literal</script>");
  assert.deepEqual(nodes["observed-visits"].children.map(node => node.dataset.observedLine), [2, 3, 2, 201], "ordered repeated visits cannot become a line set");
  assert.deepEqual(nodes["observed-compare"].children.map(node => node.value), ["", "2", "3", "7"], "same co_name in a different class/definition or file cannot become a peer");
  const compare = value => { nodes["observed-compare"].value = value; nodes["observed-compare"].listeners.change(); };
  compare("2");
  assert(nodes["observed-difference"].textContent.includes("前 3 次行號相同"));
  assert.deepEqual(nodes["observed-difference"].querySelectorAll("button").map(node => node.dataset.observedLine), [2, 201, 202]);
  assert.deepEqual(requests, [], "selecting calls/comparisons must not fetch source or start a model");
  const jump = nodes["observed-difference"].querySelectorAll("button")[2]; await jump.listeners.click();
  assert.equal(run("state.source.path"), "a.py"); assert.equal(run("state.page"), 1); assert.equal(run("state.anchor"), 202);
  assert.equal(run("state.focus.length"), 1); assert.equal(run("state.focus[0].end"), 2); assert.equal(nodes.question.value, "KEEP_QUESTION");
  assert.equal(nodes.code.children.length, 50, "path jumps reuse the source page cap");
  assert.equal(nodes["observed-compare"].value, "2", "source navigation must retain the compared calls");
  compare("3"); assert(nodes["observed-difference"].textContent.includes("行號序列相同"));
  assert(nodes["observed-difference"].textContent.includes("不代表輸入、結果或副作用相同"));
  compare("7"); assert(nodes["observed-difference"].textContent.includes("行號序列結束"));
  assert.deepEqual(nodes["observed-difference"].querySelectorAll("button").map(node => node.dataset.observedLine), [3, 2]);
  compare("4"); assert.equal(nodes["observed-difference"].children.length, 0, "do not compare forged/stale selector values");
  nodes["observed-call"].value = "2"; nodes["observed-call"].listeners.change();
  assert(nodes["observed-parent"].textContent.includes("選定父層的最近位置"));
  assert.equal(nodes["observed-parent"].querySelectorAll("button")[0].dataset.observedLine, 3);
  assert.equal(nodes["observed-parent"].querySelectorAll("button")[0].dataset.callId, 1);
  nodes["observed-call"].value = "6"; nodes["observed-call"].listeners.change();
  assert.equal(nodes["observed-parent"].children.length, 0, "parent links must not survive a different call");
  assert.equal(nodes["observed-compare"].disabled, true); assert.equal(nodes["observed-difference"].children.length, 0);
  assert(nodes["observed-result"].textContent.includes("不代表成功"));
  assert.deepEqual(JSON.parse(run("JSON.stringify(orderedPathDifference([], [2]))")), {shared: 0, last: null, left: null, right: 2});
  const stale = nodes["observed-visits"].children[0];
  run('state.refreshing = true'); await stale.listeners.click(); run('state.refreshing = false');
  run('showProject({...observedProject, version:"changed", observation:null, observation_error:"Source changed; observe again"})');
  assert.equal(nodes.observation.hidden, false); assert.equal(nodes["observation-body"].hidden, true);
  assert.equal(nodes["observation-status"].textContent, "Source changed; observe again");
  assert.equal(nodes["observed-visits"].children.length, 0); assert.equal(nodes["observed-compare"].children.length, 0);
  assert.equal(nodes["observed-parent"].children.length, 0);
  await stale.listeners.click(); assert.equal(run("state.source"), null);
  assert.equal(requests.length, 1, "stale source buttons/refreshing must never rebind an old trace");
  run('showProject({...observedProject, observation:null, observation_error:null})'); assert.equal(nodes.observation.hidden, true);
  context.fetch = originalFetch;
}
async function checkReadingTrail(context, nodes) {
  assert(nodes["source-back"], "the source pane needs a separate return-to-reading-place button, not its previous-page arrow");
  const run = code => vm.runInContext(code, context), back = nodes["source-back"], requests = [];
  const files = [{id: "0", path: "a.py", lines: 700}, {id: "1", path: "b.py", lines: 500}];
  const definition = (file, start, end) => ({file, path: files[Number(file)].path, name: "helper", kind: "function", start_line: start, definition_line: start, end_line: end});
  context.trailProject = {name: "return path", version: "v1", files, excluded: []};
  const response = data => ({ok: true, json: async () => data});
  const sourceReply = route => {
    const query = new URLSearchParams(route.split("?")[1]), file = files.find(item => item.id === query.get("file"));
    assert(file); assert(["v1", "v2"].includes(query.get("version")), "source GET must retain an explicit snapshot version");
    return response({path: file.path, version: query.get("version"), lines: Array.from({length: file.lines}, (_, i) => `SOURCE_BYTES_NOT_HISTORY ${file.path} ${i + 1}`), outline: {status: "available", items: [definition(file.id, file.id === "0" ? 20 : 280, file.id === "0" ? 27 : 295)]}});
  };
  let serve = sourceReply;
  context.fetch = async (route, options) => {
    requests.push({route, method: options.method});
    assert(!route.startsWith("/api/jobs"), "reading return must never launch or poll a model");
    if (route.startsWith("/api/definitions?")) return response({version: "v1", matches: [definition("1", 280, 295)], truncated: false, unavailable_files: 0});
    if (route === "/api/refresh") return response({...context.trailProject, version: "v2"});
    assert(route.startsWith("/api/source?")); assert.equal(options.method, "GET");
    return serve(route);
  };
  const place = () => JSON.parse(run("JSON.stringify({path:state.source?.path,version:state.source?.version,page:state.page,anchor:state.anchor,end:state.end,rangeHint:state.rangeHint})"));
  const viewport = () => ({...place(), top: nodes.code.scrollTop, left: nodes.code.scrollLeft});
  const trail = () => run("JSON.stringify(state.readingTrail)");
  const count = () => run("state.readingTrail.length");
  const gutter = (line, shiftKey = false) => nodes.code.querySelector(`[data-line="${line}"]`).children[0].listeners.click({shiftKey});
  const open = (id, start = null, end = start) => run(`openFile(state.project.files[${id}], state.project.version, ${start}, ${end})`);
  const deferred = () => { let resolve; const promise = new Promise(done => {resolve = done;}); return {promise, resolve}; };
  run("showProject(trailProject)"); assert.equal(back.disabled, true); assert.equal(count(), 0);
  await nodes.files.querySelectorAll("button")[0].listeners.click();
  nodes["page-next"].listeners.click(); nodes["page-next"].listeners.click(); gutter(450); gutter(460, true);
  nodes.code.scrollTop = 123; nodes.code.scrollLeft = 31;
  run('state.focus = [{file:"0",path:"a.py",start:1,end:2}]; renderSelections()'); nodes.question.value = "KEEP READING QUESTION";
  const original = viewport(), focus = run("JSON.stringify(state.focus)"); assert.equal(original.page, 2);
  await nodes["outline-list"].querySelectorAll("button")[0].listeners.click();
  assert.equal(count(), 1); assert.equal(run("state.anchor"), 20); assert.equal(run("state.end"), 27);
  nodes.code.scrollTop = 59; nodes.code.scrollLeft = 9; const helper = viewport();
  nodes["search-mode"].value = "definitions"; nodes["search-mode"].listeners.change(); nodes["search-query"].value = "helper";
  await nodes["search-form"].listeners.submit({preventDefault() {}});
  await nodes["search-results"].querySelectorAll("button")[0].listeners.click();
  assert.equal(run("state.source.path"), "b.py"); assert.equal(run("state.anchor"), 280); assert.equal(run("state.end"), 295);
  assert.equal(count(), 2, "cross-file definition must not add an intermediate B page-zero destination");
  for (const flag of ["pending", "refreshing"]) {
    run(`state.${flag} = true; controls()`); const before = trail(), sent = requests.length;
    assert.equal(back.disabled, true); await back.listeners.click();
    assert.equal(trail(), before); assert.equal(requests.length, sent);
    run(`state.${flag} = false; controls()`);
  }
  run('state.job = {id:"answering",status:"running"}; controls()'); assert.equal(back.disabled, false);
  const beforeReturn = requests.length;
  await back.listeners.click(); assert.deepEqual(viewport(), helper); assert.equal(count(), 1);
  await back.listeners.click(); assert.deepEqual(viewport(), original); assert.equal(count(), 0); assert.equal(back.disabled, true);
  assert.deepEqual(requests.slice(beforeReturn).map(item => item.route), ["/api/source?file=0&version=v1", "/api/source?file=0&version=v1"], "both returns re-fetch the exact cached snapshot, not remembered source bytes or a live path");
  assert.equal(nodes.code.children.length, 200); assert.equal(nodes.question.value, "KEEP READING QUESTION");
  assert.equal(run("JSON.stringify(state.focus)"), focus); run("state.job = null; controls()");

  // Failed outbound lookup and failed return both retain a usable return point.
  serve = async () => {throw Error("source lookup failed");}; await open(1, 280, 295);
  assert.equal(count(), 1); assert.equal(run("state.source"), null); assert.equal(back.disabled, false);
  serve = sourceReply; await back.listeners.click(); assert.deepEqual(viewport(), original); assert.equal(count(), 0);
  await open(1, 280, 295); const retained = trail();
  serve = async () => {throw Error("return lookup failed");}; await back.listeners.click();
  assert.equal(trail(), retained); assert.equal(back.disabled, false, "a failed return must remain retryable");
  serve = sourceReply; await back.listeners.click(); assert.deepEqual(viewport(), original); assert.equal(count(), 0);

  // A late navigation cannot restore a superseded file or rewrite the trail.
  let delayed = deferred(); serve = () => delayed.promise;
  const lateOpen = open(1, 280, 295); serve = sourceReply; await open(0, 270, 279);
  const newer = viewport(), beforeLate = trail(); delayed.resolve(sourceReply("/api/source?file=1&version=v1")); await lateOpen;
  assert.deepEqual(viewport(), newer); assert.equal(trail(), beforeLate);
  delayed = deferred(); serve = () => delayed.promise; const lateBack = back.listeners.click();
  serve = sourceReply; await open(1, 280, 295); const newerTarget = viewport(), unconsumed = trail();
  delayed.resolve(sourceReply("/api/source?file=0&version=v1")); await lateBack;
  assert.deepEqual(viewport(), newerTarget); assert.equal(trail(), unconsumed, "a stale return must not pop or overwrite the current trail");
  await back.listeners.click(); assert.deepEqual(viewport(), original); assert.equal(count(), 0);

  await open(1, 280, 295); delayed = deferred(); serve = () => delayed.promise;
  const refreshRace = back.listeners.click(); await nodes.refresh.listeners.click();
  assert.equal(run("state.project.version"), "v2"); assert.equal(count(), 0); assert.equal(back.disabled, true);
  delayed.resolve(sourceReply("/api/source?file=0&version=v1")); await refreshRace;
  assert.equal(run("state.source"), null); assert.equal(count(), 0); assert.equal(back.disabled, true, "refresh cannot resurrect an old source or return point");
  serve = sourceReply; run("showProject(trailProject)"); await open(0);
  for (let i = 0; i < 45; i++) await open(i % 2, 30 + i, 31 + i);
  assert.equal(count(), 40, "reading history has a fixed coordinate-record cap");
  const entries = JSON.parse(trail()); assert(entries.every(item => JSON.stringify(item).length < 1000));
  assert(!trail().includes("SOURCE_BYTES_NOT_HISTORY"), "history must not retain cached source content");
  for (let i = 0; i < 40; i++) await back.listeners.click();
  assert.equal(count(), 0); assert.equal(back.disabled, true);
  assert.deepEqual(place(), {path: "a.py", version: "v1", page: 0, anchor: 34, end: 35, rangeHint: ""}, "the cap discards the oldest positions, not newer return destinations");
  // The DOM mock does not perform browser layout/recentering. Explicitly move
  // the viewport after each repeated pick to model that jump, then check return.
  run("showProject(trailProject)"); await open(0);
  nodes.code.scrollTop = 500; nodes.code.scrollLeft = 17; const unselectedViewport = viewport();
  await nodes.files.querySelectorAll("button")[0].listeners.click();
  assert.equal(count(), 1, "reopening the same file/page with no selection must remember a scrolled-away viewport");
  nodes.code.scrollTop = nodes.code.scrollLeft = 0;
  await back.listeners.click(); assert.deepEqual(viewport(), unselectedViewport); assert.equal(count(), 0);
  await nodes["outline-list"].querySelectorAll("button")[0].listeners.click();
  nodes.code.scrollTop = 777; nodes.code.scrollLeft = 29; const selectedViewport = viewport(), priorEntries = count();
  await nodes["outline-list"].querySelectorAll("button")[0].listeners.click();
  assert.equal(count(), priorEntries + 1, "reselecting identical definition coordinates must retain the different viewport");
  nodes.code.scrollTop = nodes.code.scrollLeft = 0;
  await back.listeners.click(); assert.deepEqual(viewport(), selectedViewport);
  assert.equal(nodes.code.children.length, 200); assert.equal(requests.filter(item => item.method === "POST").length, 1, "only the explicitly clicked snapshot refresh is a POST");
}
async function checkDiscovery(context, nodes) {
  const run = code => vm.runInContext(code, context), sent = [];
  context.discoveryProject = {name:"new project", reader:"qwen35", version:"loc-v1", excluded:[], files:[{id:"0",path:"entry.py",lines:250},{id:"1",path:"helper.py",lines:250}]};
  const candidates = [{path:"entry.py",name:"entry",kind:"function",start_line:3,definition_line:3,end_line:8,stub:false},
    {path:"helper.py",name:"Helper.long",kind:"function",start_line:5,definition_line:5,end_line:110,stub:false}];
  const located = id => ({id,kind:"locate",status:"located",version:"loc-v1",question:"submitted question",gpu:"released",
    result:{kind:"forge8.locate",ok:true,outcome:{ok:true,status:"located",snapshot_sha256:"loc-v1",source_unchanged:true,snapshot_unchanged:true,ingress_unchanged:true,acceptance:{ok:true},candidates}}});
  let job = {id:null,status:"idle"}, fail = false, loseReply = false, cancelFlow = false, cancellations = 0;
  context.fetch = async (route, options) => {
    if (route === "/api/locate") {
      sent.push(JSON.parse(options.body));
      if (fail) return {ok: false, status: 409, json: async () => ({error: "source changed before locating"})};
      job = cancelFlow ? {id:"locating-cancel",kind:"locate",status:"running",version:"loc-v1",question:sent.at(-1).question} : {...located(loseReply ? "located-after-lost-reply" : "located-one"),question:sent.at(-1).question};
      if (loseReply) throw Error("accepted locate response was lost");
      return {ok:true,json:async () => ({id:job.id})};
    }
    if (route === "/api/jobs/locating-cancel/cancel") {
      assert.equal(options.method,"POST"); assert.deepEqual(JSON.parse(options.body),{}); cancellations++;
      job={...job,status:"cancelled",gpu:"released",result:{kind:"forge8.locate",ok:false,status:"interrupted",outcome:null}};
      return {ok:true,json:async () => ({status:"cancelling"})};
    }
    if (route === "/api/jobs/current") return {ok:true,json:async () => job};
    assert(route.startsWith("/api/source?"), "only explicit locate starts a model; opening candidates must only read snapshots");
    assert.equal(options.method,"GET");
    const params = new URLSearchParams(route.split("?")[1]); assert.equal(params.get("version"),"loc-v1");
    return {ok:true,json:async () => ({path:params.get("file") === "0" ? "entry.py" : "helper.py",version:"loc-v1",lines:Array.from({length:250},(_,i)=>`line ${i+1}`),outline:{status:"available",items:[]}})};
  };
  nodes.question.value=""; run("showProject(discoveryProject)"); assert.equal(nodes.locate.disabled,true);
  const question="  請幫我找相關實作。\n\t還不知道函式名稱。  "; nodes.question.value=question; nodes.question.listeners.input();
  assert.equal(nodes.ask.disabled,true); assert.equal(nodes.locate.disabled,false,"discovery must work before any source selection");
  await nodes.locate.listeners.click(); await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(sent,[{question,version:"loc-v1"}],"discovery sends no guessed focus or extra filesystem paths");
  assert.equal(run("state.job.status"),"located"); assert.equal(nodes["question-editor"].open,false);
  nodes["question-editor"].open=true; run("renderJob(state.job)"); assert.equal(nodes["question-editor"].open,true,"polling must not fold a manually reopened editor");
  assert(nodes.answer.textContent.includes("尚未解釋程式")); assert(nodes.answer.textContent.includes("沒有讀取函式實作"));
  assert.equal(nodes.question.value,question); assert.equal(run("state.focus.length"),0);
  let buttons=nodes.answer.querySelectorAll("button"); assert.equal(buttons.length,2);
  for (const flag of ["pending","refreshing"]) {
    run(`state.${flag}=true; controls()`); assert.equal(nodes.locate.disabled,true); assert.equal(buttons[0].disabled,true);
    await buttons[0].listeners.click(); assert.equal(run("state.source"),null);
    run(`state.${flag}=false; controls()`);
  }
  await buttons[0].listeners.click(); assert.equal(run("state.source.path"),"entry.py");
  assert.equal(run("state.anchor"),3); assert.equal(run("state.end"),8); assert.equal(run("state.focus.length"),0);
  nodes["add-selection"].listeners.click(); assert.equal(run("state.focus.length"),1); assert.equal(nodes.ask.disabled,false);
  const focus=run("JSON.stringify(state.focus)");
  await buttons[1].listeners.click(); assert.equal(run("state.source.path"),"helper.py"); assert.equal(run("state.anchor"),5); assert.equal(run("state.end"),110);
  assert(nodes["range-label"].textContent.includes("完整加入 2 段")); assert.equal(run("JSON.stringify(state.focus)"),focus); assert.equal(sent.length,1);
  const scope={mode:"files_then_definitions",files:["entry.py","helper.py"],total_files:2,total_functions:400,selected_functions:25};
  context.discoveryJob=located("scoped"); context.discoveryJob.result.outcome.scope=scope;
  run("renderJob(discoveryJob)"); assert(nodes.answer.textContent.includes("25 / 400"));
  assert(nodes.answer.textContent.includes("未選中的檔案仍可能有相關實作"));
  assert.equal(nodes.answer.querySelectorAll("li").length,2); assert.equal(nodes.answer.querySelectorAll("button").length,2);
  assert.equal(nodes.question.value,question); assert.equal(run("JSON.stringify(state.focus)"),focus); assert.equal(sent.length,1);
  for (const [index,change] of [{files:["entry.py"]},{files:["entry.py","entry.py"]},{files:["<img src=x>"]},
      {total_files:3},{selected_functions:401},{selected_functions:-1},{mode:"everything"}].entries()) {
    context.discoveryJob=located(`invalid-scope-${index}`); context.discoveryJob.result.outcome.scope={...scope,...change};
    run("renderJob(discoveryJob)"); assert.equal(nodes.answer.querySelectorAll("button").length,0);
    assert.equal(nodes.answer.querySelectorAll("img").length,0);
  }
  for (const [index,change] of [{gpu:"unknown"},{version:"old"},{status:"incomplete",error:"cancelled safely"}].entries()) {
    context.discoveryJob={...located(`bad-${index}`),...change}; run("renderJob(discoveryJob)"); assert.equal(nodes.answer.querySelectorAll("button").length,0);
  }
  context.discoveryJob=located("bad-source"); context.discoveryJob.result.outcome.source_unchanged=false;
  run("renderJob(discoveryJob)"); assert.equal(nodes.answer.querySelectorAll("button").length,0);
  context.discoveryJob=located("bad-ingress"); context.discoveryJob.result.outcome.ingress_unchanged=false;
  run("renderJob(discoveryJob)"); assert.equal(nodes.answer.querySelectorAll("button").length,0);
  context.discoveryJob=located("bad-coordinate"); context.discoveryJob.result.outcome.candidates=[{...candidates[0],end_line:9999}];
  run("renderJob(discoveryJob)"); assert.equal(nodes.answer.querySelectorAll("button").length,0);
  context.discoveryJob=located("empty"); context.discoveryJob.result.outcome.candidates=[];
  run("renderJob(discoveryJob)"); assert(nodes.answer.textContent.includes("沒有找到")); assert.equal(nodes.answer.querySelectorAll("button").length,0);
  context.discoveryJob={...located("stream"),status:"running",preview:"DO NOT SHOW RAW CANDIDATE DRAFT"};
  run("renderJob(discoveryJob)"); assert(!nodes.answer.textContent.includes("DO NOT SHOW")); assert.equal(nodes.locate.disabled,true);
  context.discoveryJob=located("restored"); run("renderJob(discoveryJob)"); buttons=nodes.answer.querySelectorAll("button");
  run('showProject({...discoveryProject,version:"loc-v2"})'); await buttons[0].listeners.click(); assert.equal(run("state.source"),null);
  assert.equal(nodes.answer.querySelectorAll("button").length,0,"refresh invalidates suggestions");
  run("showProject(discoveryProject)"); context.discoveryJob=job; run("renderJob(discoveryJob)"); fail=true;
  await nodes.locate.listeners.click(); await new Promise(resolve=>setImmediate(resolve));
  assert.equal(run("state.job.status"),"incomplete"); assert.equal(run("state.job.kind"),"locate");
  assert.equal(nodes.answer.querySelectorAll("button").length,0,"failed new submission must not resurrect the previous successful candidates");
  assert(nodes.answer.textContent.includes("source changed before locating")); assert.equal(nodes.question.value,question);

  fail=false; loseReply=true; const beforeRecovery=sent.length;
  await nodes.locate.listeners.click(); await new Promise(resolve=>setImmediate(resolve));
  assert.equal(sent.length,beforeRecovery+1,"a lost accepted response must not retry the model request");
  assert.deepEqual(sent.at(-1),{question,version:"loc-v1"});
  assert.equal(run("state.job.id"),"located-after-lost-reply"); assert.equal(run("state.job.kind"),"locate");
  assert.equal(run("state.job.status"),"located"); assert.equal(nodes.error.hidden,true,"the recovered new job clears the transport error");
  assert.equal(nodes.answer.querySelectorAll("button").length,2); assert(nodes.answer.textContent.includes(question));
  assert.equal(nodes.question.value,question); assert.equal(run("state.focus.length"),0);
  await nodes.answer.querySelectorAll("button")[0].listeners.click(); nodes["add-selection"].listeners.click();
  const beforeCancelFocus=run("JSON.stringify(state.focus)"); assert.equal(run("state.focus.length"),1);
  loseReply=false; cancelFlow=true; const beforeCancel=sent.length;
  await nodes.locate.listeners.click(); await new Promise(resolve=>setImmediate(resolve));
  assert.equal(run("state.job.status"),"running"); assert.equal(nodes.cancel.hidden,false); assert.equal(nodes.cancel.disabled,false);
  assert.equal(nodes.answer.querySelectorAll("button").length,0,"starting another locate clears the previous suggestions");
  await nodes.cancel.listeners.click(); await new Promise(resolve=>setImmediate(resolve));
  assert.equal(cancellations,1); assert.equal(sent.length,beforeCancel+1,"cancel must not submit another locate or explanation");
  assert.equal(run("state.job.status"),"cancelled"); assert.equal(run("state.job.kind"),"locate"); assert.equal(nodes.cancel.hidden,true);
  assert.equal(nodes.answer.querySelectorAll("button").length,0); assert(!nodes.answer.textContent.includes("AI 建議閱讀位置"));
  assert.equal(nodes.question.value,question); assert.equal(run("JSON.stringify(state.focus)"),beforeCancelFocus);
  assert.equal(nodes.locate.disabled,false); assert.equal(nodes["question-editor"].open,true);
}
async function checkProjectQuestion(context, nodes) {
  const run = code => vm.runInContext(code, context), sent = [], requests = [];
  const files = [{id:"0",path:"entry.py",lines:250},{id:"1",path:"helper.py",lines:250}];
  const candidates = [{path:"entry.py",name:"entry <img src=x>",kind:"function",start_line:3,end_line:8},
    {path:"helper.py",name:"Helper.long",kind:"function",start_line:10,end_line:99}];
  const focus = [{path:"entry.py",start_line:3,end_line:8},{path:"helper.py",start_line:10,end_line:89},{path:"helper.py",start_line:90,end_line:99}];
  const complete = id => ({id,kind:"project",status:"answered",version:"project-v1",files,question:"submitted project question",gpu:"released",
    result:{kind:"forge8.explain",ok:true,status:"answered",project_reading:{answer_attempted:true,focus,
      discovery:{ok:true,status:"located",snapshot_sha256:"project-v1",source_unchanged:true,snapshot_unchanged:true,ingress_unchanged:true,acceptance:{ok:true},candidates,
        scope:{mode:"files_then_definitions",files:["entry.py","helper.py"],total_files:2,total_functions:6,selected_functions:2}}},
      outcome:{ok:true,status:"answered",source_unchanged:true,snapshot_unchanged:true,acceptance:{ok:true},coverage:{observed:{ranges:[
        {path:"entry.py",ranges:[{start_line:3,end_line:8}]},{path:"helper.py",ranges:[{start_line:10,end_line:99}]}]}},
        answer:{claims:[{text:"PROJECT_ANSWER [E1:L3-L4]",citations:[{evidence_id:"E1",path:"entry.py",start_line:3,end_line:4}]}]}}}});
  let job = {id:null,status:"idle"}, lost = false, cancelled = 0;
  context.fetch = async (route, options) => {
    requests.push(route);
    if (route === "/api/project-question") {
      sent.push(JSON.parse(options.body));
      job = {id:`project-${sent.length}`,kind:"project",status:"running",version:"project-v1",question:sent.at(-1).question,
        phase:"Project question: locating source before answering."};
      if (lost) throw Error("accepted response lost");
      return {ok:true,json:async()=>({id:job.id})};
    }
    if (route.endsWith("/cancel")) {
      assert.equal(route,`/api/jobs/${job.id}/cancel`); assert.deepEqual(JSON.parse(options.body),{}); cancelled++;
      job={...job,status:"cancelled",gpu:"released",result:{kind:"forge8.explain",ok:false,status:"interrupted"}};
      return {ok:true,json:async()=>({status:"cancelling"})};
    }
    if (route === "/api/jobs/current") return {ok:true,json:async()=>job};
    assert(route.startsWith("/api/source?"), "browser never chains locate or explain POSTs");
    const query=new URLSearchParams(route.split("?")[1]); assert.equal(query.get("version"),"project-v1");
    return {ok:true,json:async()=>({path:files.find(file=>file.id===query.get("file")).path,version:"project-v1",lines:Array.from({length:250},(_,i)=>`line ${i+1}`)})};
  };
  context.projectFixture={name:"unknown project",version:"project-v1",reader:"qwen35",files,excluded:[]};
  nodes.question.value=""; run("showProject(projectFixture)"); assert(nodes["ask-project"].disabled);
  const question="  陌生專案問題\n\t不提供函式名稱。  "; nodes.question.value=question; nodes.question.listeners.input();
  assert.equal(nodes["ask-project"].disabled,false); assert(nodes.ask.disabled);
  run('state.focus=[{file:"0",path:"entry.py",start:1,end:2}]; renderSelections()');
  const manual=run("JSON.stringify(state.focus)");
  await nodes["ask-project"].listeners.click(); await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(sent,[{question,version:"project-v1"}]); assert.equal(run("state.job.kind"),"project");
  assert(nodes["ask-project"].disabled && nodes.ask.disabled && nodes.locate.disabled && nodes.refresh.disabled);
  assert.equal(nodes.question.value,question); assert.equal(run("JSON.stringify(state.focus)"),manual);
  job={...job,phase:"Project question: preparing complete candidate source."}; await run("poll()");
  assert(nodes.phase.textContent.includes("定位模型已釋放")); assert(nodes["ask-project"].disabled); assert.equal(sent.length,1);
  job={...job,phase:"Project question: answering from the automatically selected source.",preview:"ordinary project draft"}; await run("poll()");
  assert(nodes.answer.textContent.includes("ordinary project draft")); assert.equal(sent.length,1);
  job={...complete(job.id),question}; await run("poll()");
  assert(nodes.answer.textContent.includes("模型選材")); assert(nodes.answer.textContent.includes("PROJECT_ANSWER"));
  assert.equal(nodes.answer.querySelectorAll("img").length,0); assert.equal(nodes["question-editor"].open,false);
  const scopeBox=nodes.answer.querySelectorAll("details").find(node=>node.className==="project-reading-scope");
  assert.equal(scopeBox.open,false,"completed answer stays prominent above collapsed selection details");
  assert.equal(scopeBox.children[0].textContent,"模型選材 · 3 段回答原碼 · 非整個專案");
  assert.deepEqual(scopeBox.querySelectorAll("li").slice(0,2).map(node=>node.textContent),["entry.py","helper.py"],"selected files are named, not only counted");
  scopeBox.open=true; await run("poll()"); assert.equal(scopeBox.open,true);
  assert.equal(nodes.answer.querySelectorAll("details").find(node=>node.className==="project-reading-scope"),scopeBox,"unchanged polling preserves manual scope expansion");
  let links=nodes.answer.querySelectorAll("button").filter(button=>button.dataset.retained);
  assert.deepEqual(links.map(button=>button.textContent),focus.map(span=>`${span.path} · L${span.start_line}–L${span.end_line}`));
  await links[1].listeners.click(); assert.equal(run("state.anchor"),10); assert.equal(run("state.end"),89);
  assert.equal(run("JSON.stringify(state.focus)"),manual); assert.equal(nodes.question.value,question);
  const held=links[0]; context.updated={...job,status:"unknown"}; run("renderJob(updated)");
  assert(!nodes.answer.textContent.includes("PROJECT_ANSWER")); const count=requests.length; await held.listeners.click(); assert.equal(requests.length,count);
  context.updated=job; run("renderJob(updated)"); assert(nodes.answer.textContent.includes("PROJECT_ANSWER"),"reconnection restores the same final job and source scope");
  nodes["question-editor"].open=true; run("renderJob(updated)"); assert.equal(nodes["question-editor"].open,true);
  const fallback=complete("fallback"); fallback.status="incomplete"; fallback.result.ok=false; fallback.result.status="selection_required";
  fallback.result.error="Complete candidate source does not fit"; fallback.result.outcome=null;
  fallback.result.project_reading={...fallback.result.project_reading,answer_attempted:false,focus:[]};
  context.updated=fallback; run("renderJob(updated)"); assert(nodes.answer.textContent.includes("尚未進行回答"));
  assert.equal(nodes["question-editor"].open,false,"valid fallback candidates are visible without scrolling past the editor");
  assert.equal(nodes.question.value,question); assert.equal(run("JSON.stringify(state.focus)"),manual);
  nodes["question-editor"].open=true; run("renderJob(updated)"); assert.equal(nodes["question-editor"].open,true,"manual editor reopening survives unchanged fallback polling");
  const fallbackScope=nodes.answer.querySelectorAll("details").find(node=>node.className==="project-reading-scope");
  assert.equal(fallbackScope.open,true,"capacity fallback keeps candidate actions expanded");
  assert(fallbackScope.children[0].textContent.includes("0 段回答原碼"));
  const reason=nodes.answer.children.find(node=>node.textContent===fallback.result.error);
  assert(reason); assert(nodes.answer.children.indexOf(reason)<nodes.answer.children.indexOf(fallbackScope),"capacity reason appears before candidate cards");
  assert.equal(nodes.answer.textContent.split(fallback.result.error).length-1,1,"general error rendering cannot repeat the fallback reason");
  assert.equal(fallbackScope.textContent.split(candidates[0].name).length-1,1,"each candidate is displayed only as its actionable card");
  assert.equal(fallbackScope.textContent.split(candidates[1].name).length-1,1);
  links=nodes.answer.querySelectorAll("button"); assert.equal(links.length,2); await links[1].listeners.click();
  assert.equal(run("state.anchor"),candidates[1].start_line,"packable whole candidate is selected without silently adding evidence");
  assert.equal(run("state.end"),candidates[1].end_line); assert.equal(run("JSON.stringify(state.focus)"),manual);
  context.updated={...fallback,status:"unknown"}; run("renderJob(updated)"); const beforeHeld=requests.length;
  await links[0].listeners.click(); assert.equal(requests.length,beforeHeld,"a held fallback candidate cannot reopen after its job becomes unknown");
  const incomplete=complete("failed-project-answer"); incomplete.status="incomplete"; incomplete.result.ok=false; incomplete.result.status="stalled";
  Object.assign(incomplete.result.outcome,{ok:false,status:"stalled",answer:null,failure_reason:"Generation stopped at output limit"});
  context.updated=incomplete; run("renderJob(updated)");
  assert.equal(nodes["question-editor"].open,false,"a gated failed project answer must expose its failure and source scope before the editor");
  assert(nodes.answer.textContent.includes(incomplete.result.outcome.failure_reason));
  assert(!nodes.answer.textContent.includes("PROJECT_ANSWER"));
  assert.equal(nodes.answer.querySelectorAll("button").length,3);
  const failedScope=nodes.answer.querySelectorAll("details").find(node=>node.className==="project-reading-scope");
  assert.equal(failedScope.open,false); nodes["question-editor"].open=true; run("renderJob(updated)");
  assert.equal(nodes["question-editor"].open,true,"reopening the editor survives the same failed-job poll");
  for (const [index,change] of [{status:"cancelled"},{gpu:"unknown"},{version:"stale"}].entries()) {
    context.updated={...incomplete,...change,id:`unsafe-failed-project-${index}`}; run("renderJob(updated)");
    assert.equal(nodes["question-editor"].open,true); assert.equal(nodes.answer.querySelectorAll("button").length,0);
  }
  assert.equal(nodes.question.value,question); assert.equal(run("JSON.stringify(state.focus)"),manual); assert.equal(sent.length,1);
  const failed=complete("unverified-project"); failed.status="incomplete"; failed.result.ok=false; failed.result.status="stalled";
  Object.assign(failed.result.outcome,{ok:false,status:"stalled",answer:null,unverified_prose:"Complete but unverified"});
  context.updated=failed; run("renderJob(updated)"); assert(nodes.answer.textContent.includes("待核對的模型解讀")); assert.equal(nodes.answer.querySelectorAll("button").length,3);
  const empty=JSON.parse(JSON.stringify(fallback)); empty.id="empty-project-candidates";
  empty.result.project_reading.discovery.candidates=[];
  context.updated=empty; run("renderJob(updated)"); assert.equal(nodes["question-editor"].open,true,"empty candidate fallback keeps editing available");
  assert.equal(nodes.answer.querySelectorAll("button").length,0);
  for (const count of [4,6]) {
    const expanded=JSON.parse(JSON.stringify(complete(`windows-${count}`)));
    const cuts=count===4 ? [[10,39],[40,69],[70,99]] : [[10,27],[28,45],[46,63],[64,81],[82,99]];
    expanded.result.project_reading.focus=[focus[0],...cuts.map(([start_line,end_line])=>({path:"helper.py",start_line,end_line}))];
    expanded.result.outcome.coverage.observed.ranges[1].ranges=cuts.map(([start_line,end_line])=>({start_line,end_line}));
    context.updated=expanded; run("renderJob(updated)");
    assert(nodes.answer.textContent.includes("PROJECT_ANSWER"));
    assert.equal(nodes.answer.querySelectorAll("button").filter(button=>button.dataset.retained).length,count);
    assert(nodes.answer.textContent.includes(`${count} 段回答原碼`));
    assert.equal(run("JSON.stringify(state.focus)"),manual,"automatic windows do not replace the manual draft");
  }
  for (const [bounds,accepted] of [
      [Array.from({length:7},(_,i)=>[i+1,i+1]),false],
      [[[1,80],[81,160],[161,239],[240,240]],true],
      [[[1,80],[81,160],[161,240],[241,241]],false]]) {
    const bounded=JSON.parse(JSON.stringify(complete(`bound-${bounds.length}-${bounds.at(-1)[1]}`)));
    const spans=bounds.map(([start_line,end_line])=>({path:"entry.py",start_line,end_line}));
    bounded.result.project_reading.focus=spans;
    bounded.result.project_reading.discovery.candidates=[{...candidates[0],start_line:1,end_line:Math.min(bounds.at(-1)[1],240)}];
    bounded.result.outcome.coverage.observed.ranges=[{path:"entry.py",ranges:spans}];
    context.updated=bounded; run("renderJob(updated)");
    assert.equal(nodes.answer.textContent.includes("PROJECT_ANSWER"),accepted,"seven windows or 241 actual lines cannot become a project answer");
    assert.equal(nodes.answer.querySelectorAll("button").filter(button=>button.dataset.retained).length,accepted ? bounds.length : 0);
  }
  // Optional one-hop declaration provenance reuses the existing verified scope,
  // never adds a model request, and does not replace the user's manual input.
  const extraFixture = id => {
    const value=JSON.parse(JSON.stringify(complete(id))), meta=value.result.project_reading;
    meta.focus[0].start_line=1; meta.focus[2].end_line=102;
    value.result.outcome.coverage.observed.ranges[0].ranges[0].start_line=1;
    value.result.outcome.coverage.observed.ranges[1].ranges[0].end_line=102;
    meta.context={added:[{path:"entry.py",start_line:1,end_line:2},{path:"helper.py",start_line:100,end_line:102}],
      skipped:{ambiguous:1,conditional:2,limit:3,unavailable:4,capacity:384}};
    return value;
  };
  const emitContext = value => {context.updated=value; run('state.rendered=""; renderJob(updated)');};
  const projectScope = () => nodes.answer.querySelectorAll("details").find(node=>node.className==="project-reading-scope");
  const extraLinks = () => nodes.answer.querySelectorAll("button").filter(button=>button.dataset.projectContext);
  const extra=extraFixture("with-extra-context"), beforeExtra=requests.length, submittedBeforeExtra=sent.length;
  emitContext(extra); assert(nodes.answer.textContent.includes("PROJECT_ANSWER"));
  assert(projectScope().children[0].textContent.includes("自動補充 2 項同檔宣告")); assert.equal(projectScope().open,false);
  assert(projectScope().textContent.includes("名稱相符的一層宣告")); assert(projectScope().textContent.includes("不是已解析的名稱綁定、必要依賴或完整上下文"));
  for (const text of ["多個同名宣告 1","條件式宣告 2","補充數量／行數上限 3","無可用靜態宣告清單 4","原碼預算不足 384"]) assert(projectScope().textContent.includes(text));
  assert.deepEqual(extraLinks().map(button=>button.textContent),["entry.py · L1–L2","helper.py · L100–L102"]);
  assert.equal(requests.length,beforeExtra,"rendering supplemental provenance must not fetch or infer");
  const heldExtra=extraLinks()[0];
  for (const [index,span] of extra.result.project_reading.context.added.entries()) {
    await extraLinks()[index].listeners.click(); assert.equal(run("state.source.path"),span.path);
    assert.equal(run("state.anchor"),span.start_line); assert.equal(run("state.end"),span.end_line);
  }
  assert.deepEqual(requests.slice(beforeExtra),["/api/source?file=0&version=project-v1","/api/source?file=1&version=project-v1"]);
  assert.equal(sent.length,submittedBeforeExtra); assert.equal(run("JSON.stringify(state.focus)"),manual); assert.equal(nodes.question.value,question);
  const none=JSON.parse(JSON.stringify(complete("context-empty"))); none.result.project_reading.context={added:[],skipped:{}};
  emitContext(none); assert.equal(projectScope().textContent,scopeBox.textContent,"empty optional metadata leaves the old scope unchanged");
  assert.equal(extraLinks().length,0);
  const skipped=extraFixture("context-skipped-only"); skipped.result.project_reading.context.added=[]; emitContext(skipped);
  assert(!projectScope().children[0].textContent.includes("自動補充")); assert.equal(extraLinks().length,0); assert(projectScope().textContent.includes("未補入原因計數"));
  const boundary=extraFixture("context-boundary"); boundary.result.project_reading.context.added=[
    {path:"entry.py",start_line:1,end_line:2},{path:"helper.py",start_line:10,end_line:19},
    {path:"helper.py",start_line:20,end_line:29},{path:"helper.py",start_line:30,end_line:47}];
  emitContext(boundary); assert.equal(extraLinks().length,4,"four whole declarations totaling exactly forty lines fit");
  const across=extraFixture("context-across-windows"); across.result.project_reading.context.added=[{path:"helper.py",start_line:85,end_line:95}];
  emitContext(across); assert.equal(extraLinks().length,1,"a whole declaration may cross adjacent planned windows within their verified union");
  const badContexts=[null,[],{}, {added:[],skipped:{},extra:true},{added:"wrong",skipped:{}},{added:[],skipped:[]},
    ...[0,-1,1.5,385,true].map(count=>({added:[],skipped:{capacity:count}})),{added:[],skipped:{unknown:1}},
    {added:Array.from({length:5},(_,i)=>({path:"entry.py",start_line:i+1,end_line:i+1})),skipped:{}},
    {added:[{path:"helper.py",start_line:10,end_line:30},{path:"helper.py",start_line:30,end_line:49}],skipped:{}},
    ...[{path:"unknown.py",start_line:1,end_line:1},{path:"entry.py",start_line:9,end_line:9},
      {path:"helper.py",start_line:10,end_line:50},{path:"helper.py",start_line:100,end_line:103},
      {path:"entry.py",start_line:0,end_line:1},{path:"entry.py",start_line:2,end_line:1},
      {path:"entry.py",start_line:1.5,end_line:2},{path:"entry.py",start_line:1,end_line:2,extra:true}].map(span=>({added:[span],skipped:{}})),
    {added:[{path:"entry.py",start_line:1,end_line:2},{path:"entry.py",start_line:1,end_line:2}],skipped:{}}];
  for (const [index,bad] of badContexts.entries()) {
    const invalid=extraFixture(`invalid-extra-${index}`); invalid.result.project_reading.context=bad; emitContext(invalid);
    assert.equal(projectScope(),undefined); assert.equal(nodes.answer.querySelectorAll("button").length,0);
    assert(!nodes.answer.textContent.includes("PROJECT_ANSWER"),"malformed supplemental metadata cannot render an otherwise usable answer");
  }
  const staleExtraRequests=requests.length; await heldExtra.listeners.click(); assert.equal(requests.length,staleExtraRequests);
  const unverifiedExtra=extraFixture("unverified-extra-context"); unverifiedExtra.status="incomplete"; unverifiedExtra.result.ok=false; unverifiedExtra.result.status="stalled";
  Object.assign(unverifiedExtra.result.outcome,{ok:false,status:"stalled",answer:null,unverified_prose:"SUPPLEMENTAL_UNVERIFIED"});
  emitContext(unverifiedExtra); assert(nodes.answer.textContent.includes("SUPPLEMENTAL_UNVERIFIED")); assert.equal(extraLinks().length,2);
  unverifiedExtra.result.project_reading.context.added[0].end_line=9; emitContext(unverifiedExtra);
  assert(!nodes.answer.textContent.includes("SUPPLEMENTAL_UNVERIFIED")); assert.equal(projectScope(),undefined);
  const fallbackExtra=JSON.parse(JSON.stringify(fallback)); fallbackExtra.result.project_reading.context={added:[],skipped:{capacity:1}};
  emitContext(fallbackExtra); assert(projectScope()); assert.equal(extraLinks().length,0);
  fallbackExtra.result.project_reading.context.added=[{path:"entry.py",start_line:1,end_line:1}]; emitContext(fallbackExtra);
  assert.equal(projectScope(),undefined,"unattempted answers cannot claim supplemental lines were read");
  for (const [index,mutate] of [value=>{value.version="stale";},value=>{value.gpu="unknown";},value=>{delete value.result.project_reading;},
      value=>{value.result.project_reading.focus=[{path:"helper.py",start_line:10,end_line:99}];},
      value=>{value.result.outcome.coverage.observed.ranges[1].ranges[0].end_line=98;},
      value=>{value.result.project_reading.discovery.candidates.push({...candidates[0],start_line:100,end_line:101});}].entries()) {
    const invalid=JSON.parse(JSON.stringify(complete(`invalid-${index}`))); mutate(invalid); context.updated=invalid; run("renderJob(updated)");
    assert.equal(nodes.answer.querySelectorAll("button").length,0); assert(!nodes.answer.textContent.includes("PROJECT_ANSWER"));
  }
  context.updated={id:null,status:"idle"}; run("renderJob(updated)"); lost=true; nodes.question.listeners.input();
  await nodes["ask-project"].listeners.click(); await new Promise(resolve=>setImmediate(resolve));
  assert.equal(sent.length,2); assert.equal(run("state.job.id"),"project-2"); assert.equal(nodes.error.hidden,true);
  await nodes.cancel.listeners.click(); await new Promise(resolve=>setImmediate(resolve));
  assert.equal(cancelled,1); assert.equal(sent.length,2); assert.equal(run("state.job.status"),"cancelled");
  assert.equal(nodes.question.value,question); assert.equal(run("JSON.stringify(state.focus)"),manual);
  assert.equal(nodes.answer.querySelectorAll("button").length,0);
  const beforeRefresh=requests.length; run("showProject({...projectFixture,version:'new'})"); await held.listeners.click();
  assert.equal(requests.length,beforeRefresh); assert.equal(nodes.question.value,question);
}
async function checkUnverifiedProse(context, nodes) {
  const run = code => vm.runInContext(code, context), requests = [];
  const files = [{id:"0",path:"entry.py",lines:250},{id:"1",path:"helper.py",lines:250}];
  const text = "GENERATED_TEXT **still unverified** [E9:L1-L2]\n\n<img src=x onerror=alert(1)>\n\n```python\n\treturn value\n```";
  const outcome = {ok:false,status:"stalled",answer:null,source_unchanged:true,snapshot_unchanged:true,
    acceptance:{ok:true},unverified_prose:text,failure_reason:"REFERENCE_FAILURE_DETAIL",
    coverage:{observed:{ranges:[{path:"entry.py",ranges:[{start_line:195,end_line:208}]},{path:"helper.py",ranges:[{start_line:1,end_line:160}]}]}}};
  const original = {id:"review-one",status:"incomplete",version:"review-v1",reader:"qwen35",files,gpu:"released",question:"ORIGINAL_QUESTION",
    result:{kind:"forge8.explain",ok:false,status:"stalled",outcome}};
  const output = () => nodes.answer.querySelectorAll("div").find(node => node.className === "unverified-output");
  const emit = job => { context.reviewJob=job; run('state.rendered=""; renderJob(reviewJob)'); };
  context.reviewFiles=files; run('showProject({name:"review",version:"review-v1",files:reviewFiles,excluded:[]})');
  nodes.question.value="MY NEW DRAFT"; run('state.focus=[{file:"0",path:"entry.py",start:1,end:2}]; renderSelections()');
  const focus=run("JSON.stringify(state.focus)");
  context.fetch=async (route, options) => {
    requests.push(route); assert.equal(options.method,"GET"); assert(route.startsWith("/api/source?"));
    const query=new URLSearchParams(route.split("?")[1]); assert.equal(query.get("version"),"review-v1");
    return {ok:true,json:async()=>({path:files[Number(query.get("file"))].path,version:"review-v1",lines:Array.from({length:250},(_,i)=>`line ${i+1}`),outline:{status:"available",items:[]}})};
  };
  emit(original);
  assert(output(),"a complete but uncited response needs a readable, explicitly unverified view");
  assert(nodes["status-label"].textContent.includes("引用未通過")); assert(!nodes["status-label"].textContent.includes("已完成"));
  assert.equal(nodes["question-editor"].open,false,"make room for the generated text without clearing the editor");
  assert.equal(nodes.question.value,"MY NEW DRAFT"); assert.equal(run("JSON.stringify(state.focus)"),focus);
  assert(output().textContent.includes("GENERATED_TEXT")); assert(output().textContent.includes("不是回答引用"));
  assert.equal(output().querySelectorAll("img").length,0); assert.equal(output().querySelectorAll("a").length,0);
  const prose=output().querySelectorAll("div").find(node => node.className === "answer-prose");
  assert(prose.textContent.includes("[E9:L1-L2]")); assert.equal(prose.querySelectorAll("button").length,0,"never promote model references in unverified prose");
  assert.equal(prose.querySelectorAll("code").at(-1).textContent,"\treturn value");
  const detail=output().querySelectorAll("details")[0]; assert.equal(Boolean(detail.open),false); assert(detail.textContent.includes("REFERENCE_FAILURE_DETAIL"));
  const buttons=output().querySelectorAll("button"); assert.equal(buttons.length,2);
  for (const flag of ["pending","refreshing"]) {
    run(`state.${flag}=true; controls()`); assert.equal(buttons[0].disabled,true);
    await buttons[0].listeners.click(); assert.equal(requests.length,0); run(`state.${flag}=false; controls()`);
  }
  await buttons[0].listeners.click(); assert.equal(run("state.source.path"),"entry.py");
  assert.equal(run("state.anchor"),195); assert.equal(run("state.end"),208);
  await buttons[1].listeners.click(); assert.equal(run("state.source.path"),"helper.py");
  assert.equal(run("state.anchor"),1); assert.equal(run("state.end"),160,"merged observed ranges must not be clipped into invented original selections");
  assert.equal(nodes["add-selection"].disabled,false); assert(nodes["range-label"].textContent.includes("完整加入 2 段"));
  assert.equal(nodes.question.value,"MY NEW DRAFT"); assert.equal(run("JSON.stringify(state.focus)"),focus); assert.equal(requests.length,2);
  nodes["question-editor"].open=true; run("renderJob(reviewJob)"); assert.equal(nodes["question-editor"].open,true,"polling preserves manual editor expansion");
  run('renderJob({...reviewJob,status:"unknown"})'); assert.equal(output(),undefined,"lost status clears full unverified output too");
  const beforeStale=requests.length; await buttons[0].listeners.click(); assert.equal(requests.length,beforeStale);
  run("renderJob(reviewJob)"); assert(output(),"the same known terminal result may recover after a transient status loss");
  for (const change of [{status:"running"},{status:"cancelled"},{gpu:"unknown"},{version:"old"},{error:"worker failed"},{kind:"locate"}]) {
    emit({...original,...change}); assert.equal(output(),undefined);
  }
  for (const change of [{source_unchanged:false},{snapshot_unchanged:false},{acceptance:{ok:false}},{unverified_prose:""},{unverified_prose:"x".repeat(12001)},{answer:{claims:[]}},{ok:true}]) {
    emit({...original,result:{...original.result,outcome:{...outcome,...change}}}); assert.equal(output(),undefined);
  }
  emit({...original,result:{...original.result,outcome:{...outcome,coverage:{observed:{ranges:[{path:"entry.py",ranges:[{start_line:1,end_line:9999}]}]}}}}});
  assert.equal(output().querySelectorAll("button").length,0,"invalid host coordinates never open source");
  emit(original); const stale=output().querySelectorAll("button")[0];
  run('showProject({name:"new",version:"review-v2",files:reviewFiles,excluded:[]})');
  await stale.listeners.click(); assert.equal(requests.length,beforeStale); assert.equal(output(),undefined);
}
async function checkDefinitionExpansion(context, nodes) {
  const run = code => vm.runInContext(code, context), button = nodes["expand-definition"], note = nodes["definition-target"], requests=[];
  const item = (name,start,end,definition=start,kind="function") => ({name,kind,start_line:start,definition_line:definition,end_line:end,stub:false});
  const outer = item("Owner",90,280,90,"class"), parent=item("Owner.run",180,235), inner=item("Owner.run.nested",193,210,197), sibling=item("Owner.run.next",212,225);
  context.expandProject={name:"expand",version:"expand-v1",files:[{id:"0",path:"nested.py",lines:400}],excluded:[]};
  context.expandOutline={status:"available",items:[outer,parent,inner,sibling]};
  context.fetch=async(route,options)=>{
    requests.push(route); assert.equal(options.method,"GET"); assert.equal(route,"/api/source?file=0&version=expand-v1");
    return {ok:true,json:async()=>({path:"nested.py",version:"expand-v1",lines:Array.from({length:400},(_,i)=>`line ${i+1}`),outline:context.expandOutline})};
  };
  run("showProject(expandProject)"); nodes.question.value="KEEP DRAFT";
  await run('openFile(state.project.files[0],state.project.version)');
  run('state.focus=[{file:"0",path:"nested.py",start:1,end:2}]; renderSelections()');
  const focus=run("JSON.stringify(state.focus)"), job=run("JSON.stringify(state.job)");
  const place=()=>run("JSON.stringify({anchor:state.anchor,end:state.end,page:state.page})");
  const select=(start,end=start)=>run(`state.anchor=${start}; state.end=${end}; state.page=Math.floor((${end}-1)/200); renderCode(); controls()`);
  const unchanged=()=>{assert.equal(nodes.question.value,"KEEP DRAFT");assert.equal(run("JSON.stringify(state.focus)"),focus);assert.equal(run("JSON.stringify(state.job)"),job);};
  assert.equal(button.disabled,true); assert(note.textContent.includes("先選取原碼行"));
  select(204,199); const original=place(), calls=requests.length;
  assert.equal(button.disabled,false); assert(note.textContent.includes("函式 Owner.run.nested · L193–L210（18 行）"));
  assert(note.textContent.includes("不代表完整依賴"));
  await button.listeners.click(); assert.equal(requests.length,calls,"expansion uses cached coordinates without HTTP or model calls");
  assert.equal(run("state.anchor"),193); assert.equal(run("state.end"),210); assert.equal(run("state.page"),0); unchanged();
  assert(note.textContent.includes("Owner.run · L180–L235"),"an exact selected definition offers the next strictly larger parent");
  await button.listeners.click(); assert.equal(run("state.anchor"),180); assert.equal(run("state.end"),235); unchanged();
  assert.equal(button.disabled,true); assert(note.textContent.includes("類別 Owner · L90–L280（191 行）"));
  assert(note.textContent.includes("放不進剩餘 2 段")); const beforeOversize=place(); await button.listeners.click(); assert.equal(place(),beforeOversize);
  await nodes["source-back"].listeners.click(); assert.equal(run("state.anchor"),193); assert.equal(run("state.end"),210);
  await nodes["source-back"].listeners.click(); assert.equal(place(),original,"return restores reversed original selection and page"); unchanged();
  select(207,217); assert(note.textContent.includes("Owner.run · L180–L235"),"the whole range must fit, not only its first endpoint");
  await button.listeners.click(); assert.equal(run("state.anchor"),180); assert.equal(run("state.end"),235); unchanged();
  context.onlyInner={status:"available",items:[inner]}; run("state.source.outline=onlyInner");
  select(193,210); assert.equal(button.disabled,true); assert(note.textContent.includes("已選取完整定義"));
  select(10,20); assert.equal(button.disabled,true); assert(note.textContent.includes("沒有包含整段"));
  for (const range of [[198,198],[193,210]]) {
    context.ambiguous={status:"available",items:[outer,parent,inner,{...inner,name:"Other.same_span"}]}; run("state.source.outline=ambiguous"); select(...range);
    const before=place(); assert.equal(button.disabled,true); await button.listeners.click(); assert.equal(place(),before); assert(note.textContent.includes("無法唯一選取"));
  }
  for (const bad of [{...inner,start_line:0},{...inner,end_line:401},{...inner,definition_line:190},{...inner,start_line:true},{...inner,kind:"__proto__"},null]) {
    context.badOutline={status:"available",items:[bad]}; run("state.source.outline=badOutline"); select(198);
    assert.equal(button.disabled,true); assert(note.textContent.includes("定義座標無效"));
  }
  for (const status of ["unsupported","unavailable","limited"]) {
    context.badOutline={status,items:[inner]}; run("state.source.outline=badOutline"); select(198); assert.equal(button.disabled,true);
  }
  for (const size of [80,81]) {
    context.boundaryOutline={status:"available",items:[item("boundary",190,189+size)]}; run("state.source.outline=boundaryOutline"); select(198);
    assert.equal(button.disabled,false); assert(note.textContent.includes(`（${size} 行）`));
  }
  context.literalOutline={status:"available",items:[{...inner,name:"<img src=x onerror=alert(1)>",kind:"async function",stub:true}]};
  run("state.source.outline=literalOutline"); select(198); assert(note.textContent.includes("非同步函式 <img")); assert(note.textContent.includes("省略內容")); assert.equal(note.querySelectorAll("img").length,0);
  run("state.source.outline=expandOutline"); select(198); context.originalExpandSource=run("state.source");
  for (const change of ['state.anchor=199', 'state.source={...state.source}', 'state.project={...state.project,version:"new"}', 'state.source={...state.source,version:"old"}']) {
    run("state.project=expandProject; state.source=originalExpandSource"); select(198); run(change);
    const before=place(), trail=run("state.readingTrail.length"); await button.listeners.click();
    assert.equal(place(),before); assert.equal(run("state.readingTrail.length"),trail,"a displayed target cannot act after its source, version or range changes");
  }
  run("state.project=expandProject; state.source=originalExpandSource");
  for (const flag of ["pending","refreshing"]) {
    select(198); run(`state.${flag}=true; controls()`); const before=place();
    assert.equal(button.disabled,true); await button.listeners.click(); assert.equal(place(),before); run(`state.${flag}=false`);
  }
  for (const status of ["running","cancelling","unknown"]) {
    select(198); run(`state.job={id:"busy",status:"${status}"}; controls()`); const before=place();
    assert.equal(button.disabled,true); await button.listeners.click(); assert.equal(place(),before);
  }
  run('state.job={id:null,status:"idle"}; state.source=originalExpandSource; controls()'); unchanged();
  assert.equal(requests.length,3,"only initial open and two explicit returns fetch source; expansion never fetches or launches anything");
  select(198); run('showProject({...expandProject,version:"refreshed"})'); const afterRefresh=place(); await button.listeners.click();
  assert.equal(place(),afterRefresh); assert.equal(button.disabled,true); assert.equal(requests.length,3);
}
function checkPackedSelections(context, nodes) {
  const run = code => vm.runInContext(code, context), add = nodes["add-selection"], originalFetch = context.fetch;
  let requests = 0;
  context.fetch = async () => { requests++; throw Error("selection packing cannot request or execute anything"); };
  const focus = () => run("JSON.stringify(state.focus)");
  const setup = (lines, prior = [{file:"1",path:"prior.py",start:1,end:2}]) => {
    context.packedLines = lines; context.packedPrior = prior;
    run('showProject({name:"packing",version:"packing-v1",files:[{id:"0",path:"long.py",lines:packedLines.length},{id:"1",path:"prior.py",lines:2}],excluded:[]}); state.source={file:state.project.files[0],path:"long.py",version:state.project.version,lines:packedLines,outline:{status:"available",items:[]}}; state.focus=packedPrior; state.anchor=state.end=1; renderCode(); renderSelections()');
    nodes.question.value = "KEEP QUESTION"; nodes["experiment-input"].value = '{"args":["KEEP INPUT"]}';
    run('state.history=[{id:"retained",question:"KEEP HISTORY"}]; draft={marker:"KEEP DRAFT"}; controls()');
  };
  const select = (start, end) => run(`state.anchor=${start}; state.end=${end}; controls()`);
  const unchangedDrafts = () => {
    assert.equal(nodes.question.value, "KEEP QUESTION"); assert.equal(nodes["experiment-input"].value, '{"args":["KEEP INPUT"]}');
    assert.equal(run("state.history[0].question"), "KEEP HISTORY"); assert.equal(run("draft.marker"), "KEEP DRAFT");
  };
  try {
    setup(Array.from({length:300}, (_, index) => `line ${index + 1}`));
    run('state.source.outline.items=[{name:"long",kind:"function",start_line:159,definition_line:159,end_line:280,stub:false}]; state.anchor=state.end=200; controls()');
    assert.equal(nodes["expand-definition"].disabled, false);
    const prior = run("state.focus[0]"); nodes["expand-definition"].listeners.click();
    assert.equal(run("state.anchor"), 159); assert.equal(run("state.end"), 280);
    assert.equal(run("state.readingTrail.at(-1).anchor"), 200, "expansion retains the prior reading position");
    assert.equal(run("state.focus.length"), 1, "expansion does not add evidence or submit a question");
    assert.equal(add.textContent, "完整加入 2 段"); assert.equal(add.disabled, false);
    add.listeners.click();
    assert.equal(run("state.focus[0]"), prior, "existing manual selection objects remain untouched");
    assert.deepEqual(JSON.parse(focus()), [{file:"1",path:"prior.py",start:1,end:2},
      {file:"0",path:"long.py",start:159,end:238},{file:"0",path:"long.py",start:239,end:280}]);
    unchangedDrafts();

    setup(["😀".repeat(2000), "x".repeat(1985), "tail"], []); select(1, 3);
    assert.equal(add.textContent, "完整加入 2 段"); add.listeners.click();
    assert.deepEqual(JSON.parse(focus()).map(({start,end}) => ({start,end})), [{start:1,end:2},{start:3,end:3}], "count Unicode characters, line-number gutters and the intervening newline, not UTF-16 code units");
    assert.equal(run("selectedCharacters(state.source,1,2) + 2 * 7"), 4000); unchangedDrafts();

    setup([...Array(79).fill("x".repeat(48)), "x".repeat(29)], []); select(1,80);
    assert.equal(run("selectedCharacters(state.source,1,80)"),3900);
    assert.equal(add.textContent,"完整加入 2 段"); add.listeners.click();
    assert.deepEqual(JSON.parse(focus()).map(({start,end})=>({start,end})),[{start:1,end:71},{start:72,end:80}],"3,900 raw characters plus 80 gutters cannot become one 4,460-character backend read");
    assert.deepEqual(JSON.parse(run('JSON.stringify(state.focus.map(item => [...state.source.lines.slice(item.start-1,item.end).map((line,index)=>`${String(item.start+index).padStart(6)}|${line}`).join("\\n")].length))')), [3975,484]);
    setup(["x".repeat(3993)], []); select(1,1); assert.equal(add.disabled,false);
    add.listeners.click(); assert.equal(run("state.focus.length"),1,"one line fits exactly 4,000 characters including its gutter");

    setup(Array(240).fill("x"), []); select(1,240); assert.equal(add.textContent,"完整加入 3 段"); add.listeners.click();
    assert.deepEqual(JSON.parse(focus()).map(({start,end})=>({start,end})),[{start:1,end:80},{start:81,end:160},{start:161,end:240}]);
    for (const [lines, prior, start, end, note] of [
      [Array(122).fill("x"), [{file:"1",path:"prior.py",start:1,end:1},{file:"1",path:"prior.py",start:2,end:2}], 1,122,"剩餘 1 段"],
      [["x".repeat(3000),"y".repeat(3000),"z".repeat(3000)], undefined,1,3,"剩餘 2 段"],
      [["ok","x".repeat(4001)], undefined,1,2,"L2 單行含行號超過"],
      [["x".repeat(3994)], [],1,1,"L1 單行含行號超過"],
      [Array(241).fill("x"), [],1,241,"剩餘 3 段"],
    ]) {
      setup(lines,prior); select(start,end); const before=focus();
      assert.equal(add.disabled,true); assert(nodes["range-label"].textContent.includes(note));
      add.listeners.click(); assert.equal(focus(),before,"capacity failure must not add even an initial valid chunk");
      assert.equal(run(`packSourceRange(state.source.lines,${start},${end},3-state.focus.length).chunks.length`),0); unchangedDrafts();
    }

    setup(Array(100).fill("x"), [{file:"0",path:"long.py",start:1,end:80}]); select(1,100);
    const duplicate = focus(); assert.equal(add.disabled,false); add.listeners.click();
    assert.equal(focus(),duplicate,"one duplicate chunk rejects the whole operation, not just that chunk");
    assert(nodes.error.textContent.includes("原有選段未變動"));

    for (const mutation of ['state.anchor=2','state.end=99','state.source={...state.source}',
      'state.source.version="stale"','state.project={...state.project,version:"stale"}',
      'state.focus=[...state.focus,{file:"1",path:"prior.py",start:2,end:2}]']) {
      setup(Array(100).fill("x")); select(1,100); assert.equal(add.disabled,false);
      run(mutation); const before=focus(); add.listeners.click(); assert.equal(focus(),before,"a stale Add preview cannot apply changed source, endpoints, version or manual selections");
      unchangedDrafts();
    }

    setup(Array(100).fill("x"), []);
    run('state.project.comparison={head:"a".repeat(40)}; state.source.path="before/long.py"'); nodes["change-slot"].value="before";
    select(1,81); assert.equal(add.disabled,true); add.listeners.click(); assert.equal(focus(),"[]");
    select(1,80); assert.equal(add.disabled,false); add.listeners.click();
    assert.deepEqual(JSON.parse(focus()),[{file:"0",path:"before/long.py",start:1,end:80,role:"before"}],"comparison roles remain single bounded ranges");
    setup(Array(100).fill("x"), []); run("state.project.browse_only=true"); select(1,81);
    assert.equal(add.disabled,true,"browse-only name navigation retains its existing one-range limit");
    assert.equal(requests,0);
  } finally { context.fetch=originalFetch; run("draft=null"); }
}
async function checkTraceback(context, nodes) {
  const run = code => vm.runInContext(code, context), button = nodes["traceback-locate"], results = nodes["traceback-results"], requests = [];
  const files = [{id:"0",path:"a.py",lines:420},{id:"1",path:"other/a.py",lines:40}];
  context.traceProject = {name:"trace",version:"trace-v1",files,excluded:[]};
  const text = '為什麼失敗？\r\nTraceback (most recent call last):\r\n  File "a.py", line 201, in run\r\nValueError: <img src=x onerror=alert(1)>\n';
  const frame = {reported_path:"a.py",line:201,function:"<img src=x onerror=alert(1)>",file:"0",path:"a.py",reason:null};
  const outcome = {version:"trace-v1",scope:"unverified_user_traceback",matched:2,frames:[frame,{reported_path:"/private/a.py",line:4,function:"",file:null,path:null,reason:"outside_project"},{...frame},{reported_path:"missing.py",line:3,function:"gone",file:null,path:null,reason:"unknown_source"}]};
  const reply = data => ({ok:true,json:async()=>data}); let serve = async () => reply(outcome);
  context.fetch = async (route, options) => {
    requests.push({route,method:options.method,body:options.body});
    if (route === "/api/traceback") { assert.equal(options.method,"POST"); return serve(); }
    if (route === "/api/refresh") return reply({...context.traceProject,version:"trace-v2"});
    assert.equal(route,"/api/source?file=0&version=trace-v1", "trace text cannot create arbitrary paths, model requests or observations");
    assert.equal(options.method,"GET");
    return reply({path:"a.py",version:"trace-v1",lines:Array.from({length:420},(_,i)=>`source ${i+1}`),outline:{status:"unsupported",items:[]}});
  };
  nodes.question.value=""; run("showProject(traceProject)"); assert.equal(button.disabled,true);
  nodes.question.value = text; nodes.question.listeners.input();
  run('state.focus=[{file:"1",path:"other/a.py",start:1,end:2}]; renderSelections()');
  const focus = run("JSON.stringify(state.focus)"); await run('openFile(state.project.files[0],state.project.version,10,12)');
  const before = requests.length, job = run("JSON.stringify(state.job)"); await button.listeners.click();
  assert.equal(requests.length,before+1); assert.deepEqual(JSON.parse(requests.at(-1).body),{text,version:"trace-v1"});
  assert.equal(run("JSON.stringify(state.job)"),job); assert.equal(nodes.question.value,text); assert.equal(run("JSON.stringify(state.focus)"),focus);
  assert(nodes["traceback-summary"].textContent.includes("貼上紀錄未驗證，只對應目前快照：2 / 4"));
  assert.deepEqual(results.children.map(row=>row.dataset.tracebackFrame),[1,2,3,4]);
  assert.equal(results.querySelectorAll("button").length,2,"keep repeated frames, unmatched rows are not links");
  assert(results.children[1].textContent.includes("不在目前專案內")); assert(results.children[3].textContent.includes("未納入目前快照"));
  assert(results.textContent.includes(frame.function)); assert.equal(results.querySelectorAll("img").length,0);
  const held = results.querySelectorAll("button")[0]; await held.listeners.click();
  assert.equal(run("state.anchor"),201); assert.equal(run("state.end"),201); assert.equal(run("state.page"),1);
  await nodes["source-back"].listeners.click(); assert.equal(run("state.anchor"),10); assert.equal(run("state.end"),12);
  assert.equal(run("JSON.stringify(state.focus)"),focus); assert.equal(nodes.question.value,text);
  for (const status of ["running","cancelling","unknown"]) {
    run(`state.job={id:"model",status:"${status}"}; controls()`); const count=requests.length;
    assert.equal(button.disabled,true); await button.listeners.click(); assert.equal(requests.length,count);
  }
  run('state.job={id:"model",status:"running"}; controls()'); await held.listeners.click();
  assert.equal(run("state.job.status"),"running"); assert.equal(nodes["add-selection"].disabled,true,"reading during generation cannot change captured focus");
  run("state.job=null; controls()");
  const count=requests.length; nodes.question.value=text+"changed"; nodes.question.listeners.input();
  assert.equal(results.children.length,0); await held.listeners.click(); assert.equal(requests.length,count);

  const deferred = () => {let resolve; const promise=new Promise(done=>{resolve=done;}); return {promise,resolve};};
  const first=deferred(),second=deferred(); let next=first; serve=()=>next.promise;
  const oldRequest=button.listeners.click(); assert.equal(button.disabled,true);
  nodes.question.value=text; nodes.question.listeners.input(); next=second;
  const newerRequest=button.listeners.click(); first.resolve(reply(outcome)); await oldRequest;
  assert.equal(results.children.length,0); assert.equal(button.disabled,true,"old replies must not clear the newer pending request");
  second.resolve(reply(outcome)); await newerRequest; assert.equal(results.children.length,4);
  const delayed=deferred(); serve=()=>delayed.promise; const refreshRace=button.listeners.click();
  await nodes.refresh.listeners.click(); delayed.resolve(reply(outcome)); await refreshRace;
  assert.equal(run("state.project.version"),"trace-v2"); assert.equal(results.children.length,0);
  const afterRefresh=requests.length; await held.listeners.click(); assert.equal(requests.length,afterRefresh);
  run("showProject(traceProject)"); nodes.question.value=text; nodes.question.listeners.input();
  const unmatched=["outside_project","unknown_source","unsupported_path","line_out_of_range","line_separators"].map(reason=>({...frame,line:0,file:null,path:null,reason}));
  serve=async()=>reply({...outcome,matched:0,frames:unmatched}); await button.listeners.click();
  assert.equal(results.children.length,5); assert.equal(results.querySelectorAll("button").length,0); assert(nodes["traceback-summary"].textContent.includes("0 / 5"));
  assert(results.textContent.includes("行號超出目前檔案")); assert(results.textContent.includes("換行格式無法精確對應行號"));
  for (const invalid of [{...outcome,version:"wrong"},{...outcome,scope:"verified"},{...outcome,matched:1},{...outcome,frames:[]},{...outcome,frames:Array(33).fill(frame)},{...outcome,matched:1,frames:[{...frame,path:"other/a.py"}]},{...outcome,matched:1,frames:[{...frame,line:0}]},{...outcome,matched:1,frames:[{...frame,line:421}]}]) {
    serve=async()=>reply(invalid); await button.listeners.click(); assert.equal(results.children.length,0); assert(nodes.error.textContent);
  }
  serve=async()=>{throw Error("HTTP unavailable")}; await button.listeners.click();
  assert(nodes.error.textContent.includes("HTTP unavailable")); assert.equal(button.disabled,false); assert.equal(results.children.length,0);
  serve=async()=>reply(outcome); await button.listeners.click(); assert.equal(results.children.length,4,"failed lookup remains retryable");
  const beforeOversize=requests.length; nodes.question.value="x".repeat(2001); nodes.question.listeners.input(); await button.listeners.click();
  assert.equal(requests.length,beforeOversize); assert.equal(nodes.question.value.length,2001); assert(nodes.error.textContent.includes("未送出或裁切"));
  nodes.question.value=text; nodes.question.selectionStart=nodes.question.selectionEnd=text.length; let prevented=0;
  nodes.question.listeners.paste({clipboardData:{getData:()=>"x".repeat(2001)},preventDefault(){prevented++;}});
  assert.equal(prevented,1); assert.equal(nodes.question.value,text); assert(nodes.error.textContent.includes("已保留原稿"));
  nodes.question.selectionStart=0; nodes.question.selectionEnd=text.length;
  nodes.question.listeners.paste({clipboardData:{getData:()=>text},preventDefault(){prevented++;}}); assert.equal(prevented,1,"normal replacement paste keeps native editing");
  assert.equal(requests.filter(item=>item.method==="POST"&&!(["/api/traceback","/api/refresh"].includes(item.route))).length,0);
}
async function checkReadingHistory(context, nodes) {
  const run = code => vm.runInContext(code, context), reply = value => ({ok: true, json: async () => value});
  const requests = [], files = [{id: "0", path: "a.py", lines: 4}];
  const project = {name: "session", version: "history-v1", files, excluded: []};
  const reference = {evidence_id: "E1", path: "a.py", start_line: 1, end_line: 2};
  const first = {id: "history-a", version: project.version, files, question: "A_ORIGINAL_QUESTION", status: "answered", gpu: "released", elapsed_seconds: 12.5,
    result: {kind: "forge8.explain", ok: true, status: "answered", outcome: {ok: true, status: "answered", source_unchanged: true, snapshot_unchanged: true,
      acceptance: {ok: true}, answer: {claims: [{text: "A_ORIGINAL_ANSWER [E1:L1-L2]", citations: [reference]}]}}}};
  let current = first, archived = [first], evicted = 0, currentProject = project, acceptSubmission;
  const submissionGate = new Promise(done => { acceptSubmission = done; });
  let historyReply = async () => reply({version: currentProject.version, entries: archived, evicted});
  context.fetch = async (route, options) => {
    requests.push({route, method: options.method, body: options.body});
    if (route === "/api/project") return reply(currentProject);
    if (route === "/api/history") return historyReply();
    if (route === "/api/jobs/current") return reply(current);
    if (route === "/api/jobs") {
      assert.deepEqual(JSON.parse(options.body), {question: "B_DRAFT_QUESTION", version: project.version, focus: [{file: "0", start: 1, end: 2}]}, "history must never become model question context");
      current = {id: "history-b", version: project.version, files, question: "B_DRAFT_QUESTION", status: "running", elapsed_seconds: 3, preview: "B_LIVE_DRAFT"};
      await submissionGate;
      return reply({id: current.id});
    }
    if (route === "/api/jobs/history-b/cancel") {
      current = {...current, status: "cancelled", gpu: "released", elapsed_seconds: 7}; delete current.preview;
      archived = [first, current]; return reply({status: "cancelled"});
    }
    if (route === "/api/refresh") {
      currentProject = {...project, version: "history-v2"}; archived = []; evicted = 0; current = {id: null, status: "idle"};
      return reply(currentProject);
    }
    assert.equal(route, "/api/source?file=0&version=history-v1", "history navigation may only read its exact-version source");
    return reply({path: "a.py", version: project.version, lines: ["first source", "second source", "third", "fourth"], outline: {status: "unsupported", items: []}});
  };
  context.historyProject = project; run("showProject(historyProject)"); await run("poll()");
  assert.equal(run("state.history.length"), 1); assert(nodes["history-select"].textContent.includes("A_ORIGINAL_QUESTION"));
  nodes.question.value = "B_DRAFT_QUESTION"; run('state.focus = [{file:"0", path:"a.py", start:1, end:2}]; renderSelections()');
  const focus = run("JSON.stringify(state.focus)");
  const choose = id => {nodes["history-select"].value = id; nodes["history-select"].listeners.change();};
  const submission = nodes["question-form"].listeners.submit({preventDefault() {}});
  choose(first.id); assert(nodes.answer.textContent.includes("A_ORIGINAL_ANSWER"));
  acceptSubmission(); await submission; await new Promise(setImmediate);
  assert.equal(run("state.job.id"), "history-b"); assert(nodes.answer.textContent.includes("A_ORIGINAL_ANSWER"), "accepting B must not overwrite A selected while B was being submitted");
  choose(""); assert(nodes.answer.textContent.includes("B_LIVE_DRAFT"));
  const count = requests.length, editorOpen = nodes["question-editor"].open;
  choose(first.id);
  assert.equal(requests.length, count, "selecting history makes no HTTP or model request");
  assert(nodes.answer.textContent.includes("A_ORIGINAL_QUESTION")); assert(nodes.answer.textContent.includes("A_ORIGINAL_ANSWER"));
  assert(!nodes.answer.textContent.includes("B_LIVE_DRAFT")); assert.equal(run("draft"), null);
  assert.equal(run("state.job.id"), "history-b"); assert.equal(nodes["status-label"].textContent, "本機模型處理中");
  assert.equal(nodes.cancel.hidden, false); assert(nodes["history-note"].textContent.includes("仍屬於目前工作"));
  assert.equal(nodes.question.value, "B_DRAFT_QUESTION"); assert.equal(run("JSON.stringify(state.focus)"), focus); assert.equal(nodes["question-editor"].open, editorOpen);
  const citation = nodes.answer.querySelectorAll("button").find(button => button.className === "citation");
  await citation.listeners.click();
  assert(nodes.answer.textContent.includes("first source")); assert.equal(nodes.question.value, "B_DRAFT_QUESTION"); assert.equal(run("JSON.stringify(state.focus)"), focus);
  const historyOptions = [...nodes["history-select"].children];
  current = {...current, elapsed_seconds: 5, preview: "B_NEW_LIVE_DRAFT"}; await run("poll()");
  assert.deepEqual(nodes["history-select"].children, historyOptions, "ordinary status polls must preserve the open native selector and its option nodes");
  assert(nodes.answer.textContent.includes("A_ORIGINAL_ANSWER")); assert(!nodes.answer.textContent.includes("B_NEW_LIVE_DRAFT")); assert.equal(nodes.elapsed.textContent, "0:05");
  choose(""); assert(nodes.answer.textContent.includes("B_NEW_LIVE_DRAFT"));
  choose(first.id); await nodes.cancel.listeners.click(); await new Promise(setImmediate);
  assert.equal(requests.filter(item => item.route.endsWith("/cancel")).length, 1); assert.equal(requests.find(item => item.route.endsWith("/cancel")).route, "/api/jobs/history-b/cancel");
  assert.equal(run("state.job.status"), "cancelled"); assert(nodes.answer.textContent.includes("A_ORIGINAL_ANSWER")); assert.equal(run("state.history.length"), 2);
  assert.equal(nodes.question.value, "B_DRAFT_QUESTION"); assert.equal(run("JSON.stringify(state.focus)"), focus);
  assert.equal(requests.filter(item => item.method === "POST" && item.route === "/api/jobs").length, 1);

  // Run the actual bootstrap after clearing client state: the server is the only
  // history source, including when its current job is already terminal.
  run("state.history = []; state.selectedHistory = null");
  await run(app.slice(app.lastIndexOf("(async () => {")));
  assert.equal(run("state.history.length"), 2); assert.equal(run("state.selectedHistory"), null);
  assert.equal(nodes["status-label"].textContent, "本題已取消"); choose(first.id); assert(nodes.answer.textContent.includes("A_ORIGINAL_ANSWER"));
  const beforeReads = requests.length; choose(current.id); assert(nodes.answer.textContent.includes("本題已取消")); assert(!nodes.answer.textContent.includes("B_NEW_LIVE_DRAFT")); choose(first.id);
  assert.equal(requests.length, beforeReads);

  const unverified = {...first, id: "history-unverified", status: "incomplete", question: "UNVERIFIED_ORIGINAL_QUESTION",
    result: {kind: "forge8.explain", ok: false, status: "stalled", outcome: {ok: false, status: "stalled", answer: null, source_unchanged: true, snapshot_unchanged: true,
      acceptance: {ok: true}, unverified_prose: "HISTORICAL_UNVERIFIED [E9:L1-L2]", failure_reason: "bad reference",
      coverage: {observed: {ranges: [{path: "a.py", ranges: [{start_line: 1, end_line: 2}]}]}}}}};
  archived = [first, unverified]; await run("loadHistory()"); choose(unverified.id);
  assert(nodes.answer.textContent.includes("待核對的模型解讀")); assert(nodes.answer.textContent.includes("HISTORICAL_UNVERIFIED"));
  const retained = nodes.answer.querySelectorAll("button").find(button => button.dataset.retained);
  const heldOutput = nodes.answer.querySelectorAll("div").find(node => node.className === "unverified-output");
  current = {...current, id: "history-c", status: "running", preview: "CURRENT_C_DRAFT"}; await run("poll()");
  assert.equal(nodes.answer.querySelectorAll("div").find(node => node.className === "unverified-output"), heldOutput, "running current work must not delete an archived unverified answer");
  await retained.listeners.click(); assert.equal(run("state.source.path"), "a.py"); assert.equal(run("state.job.id"), "history-c");
  current = {...current, status: "unknown"}; await run("poll()"); assert(nodes.answer.textContent.includes("HISTORICAL_UNVERIFIED"));
  current = {...current, status: "cancelled"}; delete current.preview; await run("poll()");
  assert(nodes.answer.textContent.includes("HISTORICAL_UNVERIFIED"));

  const deferred = () => {let resolve; const promise = new Promise(done => {resolve = done;}); return {promise, resolve};};
  const older = deferred(), newer = deferred(); historyReply = () => older.promise;
  const oldRequest = run("loadHistory()"); historyReply = () => newer.promise; const newRequest = run("loadHistory()");
  newer.resolve(reply({version: project.version, entries: [current], evicted: 1})); await newRequest;
  assert.equal(run("state.selectedHistory"), null); assert(nodes["history-note"].textContent.includes("移除 1 題")); assert(nodes["history-note"].textContent.includes("已返回目前工作"));
  older.resolve(reply({version: project.version, entries: [first], evicted: 0})); await oldRequest;
  assert.equal(run("state.history[0].id"), current.id, "older history response cannot replace newer data or resurrect evicted answers");

  historyReply = async () => reply({version: project.version, entries: [first], evicted: 0}); await run("loadHistory()"); choose(first.id);
  const sameVersion = deferred(); historyReply = () => sameVersion.promise; const sameVersionRequest = run("loadHistory()");
  run("showProject({...historyProject}, true)"); sameVersion.resolve(reply({version: project.version, entries: [first], evicted: 0})); await sameVersionRequest;
  assert.equal(run("state.history.length"), 0, "a successful same-byte refresh also rejects old history replies by project identity/generation");
  const staleCitationRequests = requests.length; await citation.listeners.click(); assert.equal(requests.length, staleCitationRequests, "old citation controls cannot navigate after same-version refresh");
  historyReply = async () => reply({version: project.version, entries: [first], evicted: 0}); await run("loadHistory()"); choose(first.id);
  const stale = deferred(); historyReply = () => stale.promise; const staleRequest = run("loadHistory()");
  await nodes.refresh.listeners.click(); stale.resolve(reply({version: project.version, entries: [first], evicted: 0})); await staleRequest;
  assert.equal(run("state.history.length"), 0); assert.equal(run("state.selectedHistory"), null); assert.equal(run("state.project.version"), "history-v2");
  assert(!nodes.answer.textContent.includes("A_ORIGINAL_ANSWER")); assert(nodes["history-note"].textContent.includes("先前問答紀錄已清除"));
  for (const bad of [
    {version: project.version, entries: [first], evicted: 0},
    {version: currentProject.version, entries: [{...first, version: currentProject.version, status: "running"}], evicted: 0},
    {version: currentProject.version, entries: [{...first, version: currentProject.version, preview: "DO_NOT_REVIVE"}], evicted: 0},
    {version: currentProject.version, entries: [{...first, version: currentProject.version, rejected_preview: "DO_NOT_REVIVE"}], evicted: 0},
    {version: currentProject.version, entries: [first, first], evicted: 0},
    {version: currentProject.version, entries: [], evicted: -1}
  ]) {
    historyReply = async () => reply(bad); await run("loadHistory()");
    assert.equal(run("state.history.length"), 0); assert.equal(nodes["history-retry"].hidden, false); assert(!nodes.answer.textContent.includes("DO_NOT_REVIVE"));
  }
  historyReply = async () => {throw Error("history offline")}; await run("loadHistory()"); assert(nodes["history-note"].textContent.includes("目前工作不受影響"));
  historyReply = async () => reply({version: currentProject.version, entries: [], evicted: 0}); await nodes["history-retry"].listeners.click();
  assert.equal(nodes["history-retry"].hidden, true); assert.equal(run("state.job.status"), "idle");
}
async function checkArchivedSourceNavigation(context, nodes) {
  const run = code => vm.runInContext(code, context), requests = [];
  const files = [{id: "0", path: "entry.py", lines: 30}, {id: "1", path: "helper.py", lines: 30}];
  const project = {name: "archived navigation", version: "navigation-v1", files, excluded: []};
  const candidate = {path: "helper.py", name: "helper", kind: "function", start_line: 10, definition_line: 10, end_line: 14, stub: false};
  const discovery = {ok: true, status: "located", snapshot_sha256: project.version, source_unchanged: true,
    snapshot_unchanged: true, ingress_unchanged: true, acceptance: {ok: true}, candidates: [candidate]};
  const common = {version: project.version, files, gpu: "released", elapsed_seconds: 20};
  const located = {...common, id: "archive-locate", kind: "locate", status: "located", question: "ORIGINAL_LOCATE_QUESTION",
    result: {kind: "forge8.locate", ok: true, outcome: discovery}};
  const completed = {...common, id: "archive-project", kind: "project", status: "answered", question: "ORIGINAL_PROJECT_QUESTION",
    result: {kind: "forge8.explain", ok: true, status: "answered", project_reading: {discovery, answer_attempted: true,
      focus: [{path: "helper.py", start_line: 10, end_line: 14}]}, outcome: {ok: true, status: "answered", source_unchanged: true,
      snapshot_unchanged: true, acceptance: {ok: true}, coverage: {observed: {ranges: [{path: "helper.py", ranges: [{start_line: 10, end_line: 14}]}]}},
      answer: {claims: [{text: "ARCHIVED_PROJECT_ANSWER", citations: []}]}}}};
  const fallback = {...common, id: "archive-project-fallback", kind: "project", status: "incomplete", question: "ORIGINAL_FALLBACK_QUESTION",
    result: {kind: "forge8.explain", ok: false, status: "selection_required", error: "Complete candidates do not fit",
      outcome: null, project_reading: {discovery, answer_attempted: false, focus: []}}};
  context.navigationProject = project;
  context.activeNavigationJob = {id: "different-current-job", version: project.version, status: "running", question: "CURRENT_JOB_QUESTION", elapsed_seconds: 8, preview: "CURRENT_JOB_PREVIEW"};
  context.fetch = async (route, options) => {
    requests.push(route); assert.equal(options.method, "GET", "archived source navigation cannot make implicit inference or cancellation requests");
    if (route === "/api/history") return {ok: true, json: async () => ({version: project.version, entries: [located, completed, fallback], evicted: 0})};
    assert.equal(route, "/api/source?file=1&version=navigation-v1");
    return {ok: true, json: async () => ({path: "helper.py", version: project.version, lines: Array.from({length: 30}, (_, i) => `helper line ${i + 1}`), outline: {status: "available", items: [candidate]}})};
  };
  run("showProject(navigationProject); renderJob(activeNavigationJob)");
  nodes.question.value = "UNSUBMITTED_NEXT_QUESTION";
  run('state.focus = [{file:"0", path:"entry.py", start:1, end:2}]; renderSelections()');
  await run("loadHistory()");
  const focus = run("JSON.stringify(state.focus)"), current = run("JSON.stringify(state.job)");
  const choose = id => {nodes["history-select"].value = id; nodes["history-select"].listeners.change();};
  let priorButton;
  for (const [archived, attribute, editorOpen] of [[located, "discovery", true], [completed, "retained", false], [fallback, "discovery", true]]) {
    nodes["question-editor"].open = editorOpen; const beforeSelection = requests.length;
    choose(archived.id);
    assert.equal(requests.length, beforeSelection, "selecting an archived locate/project answer does not fetch or rerun it");
    assert(nodes.answer.textContent.includes(archived.question)); assert(!nodes.answer.textContent.includes("CURRENT_JOB_PREVIEW"));
    assert.equal(nodes["question-editor"].open, editorOpen, "archived renderers preserve the user's editor-open choice");
    if (priorButton) {await priorButton.listeners.click(); assert.equal(requests.length, beforeSelection, "a held source action from a different viewed answer cannot navigate");}
    const button = nodes.answer.querySelectorAll("button").find(button => button.dataset[attribute]);
    assert(button, `archived ${archived.id} must retain its gated ${attribute} action`);
    await button.listeners.click();
    assert.equal(run("state.source.path"), "helper.py"); assert.equal(run("state.anchor"), 10); assert.equal(run("state.end"), 14);
    assert.equal(run("JSON.stringify(state.job)"), current, "historical source actions cannot replace current job or cancellation target");
    assert.equal(run("JSON.stringify(state.focus)"), focus); assert.equal(nodes.question.value, "UNSUBMITTED_NEXT_QUESTION");
    assert.equal(nodes["question-editor"].open, editorOpen); assert.equal(nodes.cancel.hidden, false); assert.equal(nodes.cancel.disabled, false);
    assert.equal(nodes["add-selection"].disabled, true, "browsing historical source while current work runs cannot mutate its captured focus");
    run("renderJob({...activeNavigationJob, elapsed_seconds: 9})");
    assert(nodes.answer.textContent.includes(archived.question)); assert.equal(nodes["question-editor"].open, editorOpen);
    run("renderJob(activeNavigationJob)"); priorButton = button;
  }
  const beforeCurrent = requests.length; choose(""); await priorButton.listeners.click();
  assert.equal(requests.length, beforeCurrent); assert(nodes.answer.textContent.includes("CURRENT_JOB_PREVIEW"));
  assert.deepEqual(requests, ["/api/history", "/api/source?file=1&version=navigation-v1", "/api/source?file=1&version=navigation-v1"],
    "only history hydration and two explicit source reads occur; the remaining same-file candidate reuses current snapshot data");
}
async function checkGeneratorExperiments(context, nodes) {
  const run = code => vm.runInContext(code, context), originalFetch = context.fetch, requests = [];
  const reply = value => ({ok: true, json: async () => JSON.parse(JSON.stringify(value))});
  const deferred = () => {let resolve; const promise = new Promise(done => {resolve = done;}); return {promise, resolve};};
  const project = {name: "generator fixture", version: "generator-v1", experiments_enabled: true,
    files: [{id: "0", path: "probe.py", lines: 2}, {id: "1", path: "helper.py", lines: 1}], excluded: []};
  const target = {file: "0", path: "probe.py", version: project.version, entry: "probe", source_sha256: "a".repeat(64), source_bytes: 36};
  const input = '{"args":[9007199254740993],"kwargs":{}}\n';
  const resultText = '{"generator":{"values":[9007199254740993,"<img src=x>"],"stop":"limit"}}';
  const completed = (id, steps = 3) => ({...target, id, status: "completed", input_text: input, result_text: resultText,
    elapsed_seconds: 1, source_unchanged: true, runtime_unchanged: true, ...(steps ? {generator_steps: steps} : {})});
  let current = {id: null, status: "idle"}, held = null, nextId = 0;
  let runReply = body => {current = {...completed("generator-" + ++nextId, body.generator_steps || 0), status: "running"};
    delete current.result_text; return reply({id: current.id});};
  context.fetch = async (route, options) => {
    const body = options.body === undefined ? undefined : JSON.parse(options.body); requests.push({route, method: options.method, body});
    if (route === "/api/experiment/current") return held ? held.promise : reply(current);
    if (route === "/api/experiment/run") return runReply(body);
    if (route === "/api/experiment/baseline/clear") {
      assert.deepEqual(body, {id: "ordinary-a", version: project.version, revision: 1});
      current = {...current, input_comparison: {...current.input_comparison, revision: 2, baseline: null, reason: "no_baseline"}};
      return reply(current.input_comparison);
    }
    throw Error("unexpected generator request: " + route);
  };
  context.generatorProject = project; context.generatorTarget = target;
  const choose = value => {nodes["experiment-generator-steps"].value = String(value); nodes["experiment-generator-steps"].listeners.change();};
  const count = route => requests.filter(item => item.route === route).length;
  try {
    run("showProject(generatorProject,true);state.experiment.target=generatorTarget;state.experiment.baseTarget=generatorTarget;state.experiment.visible=true;renderExperiment()");
    nodes.question.value = "KEEP QUESTION"; nodes.answer.textContent = "KEEP ANSWER";
    run("state.focus=[{file:'0',path:'probe.py',start:1,end:2}];renderSelections()");
    nodes["experiment-input"].value = input; nodes["experiment-input"].listeners.input();
    const reading = () => run("JSON.stringify([state.focus,state.job,state.history,$('question').value,$('answer').textContent])"), kept = reading();
    assert.equal(nodes["experiment-generator-steps"].value, "0");
    assert.equal(nodes["experiment-generator-option"].hidden, false);
    assert.equal(nodes["experiment-generator-submitted"].hidden, true);
    assert.match(html, /next\(\).*close\(\).*finally/);
    assert.match(html, /next\(\) 嘗試次數，不代表已耗盡/);
    for (const bad of ["13", "-1", "1.5", "", "03", "true"]) {choose(bad); assert.equal(run("state.experiment.generatorDraft"), 0);}
    choose(3); assert.equal(requests.length, 0, "draft mode never fetches or executes");
    assert.equal(nodes["experiment-panel"].dataset.generator, "false", "a generator draft does not relabel an ordinary result layout");
    assert.equal(nodes["experiment-trace-option"].hidden, true); assert.equal(nodes["experiment-trace-lines"].disabled, true);
    assert.equal(nodes["experiment-modules"].hidden, true); assert.equal(nodes["experiment-module-prepare"].disabled, true);
    assert.match(nodes["experiment-run"].textContent, /3 次 next\(\).*close\(\)/);
    await nodes["experiment-run"].listeners.click(); await nodes["experiment-run"].listeners.click();
    assert.equal(count("/api/experiment/run"), 1);
    assert.deepEqual(requests.find(item => item.route === "/api/experiment/run").body,
      {file: "0", version: target.version, entry: "probe", source_sha256: target.source_sha256, input_text: input, allow_execution: true, generator_steps: 3});
    assert.equal(nodes["experiment-generator-steps"].disabled, true);
    const panel = nodes["experiment-panel"];
    panel.scrollTop = 0; panel.clientHeight = 200; panel.scrollHeight = 900;
    panel.getBoundingClientRect = () => ({top: 100, bottom: 300});
    nodes["experiment-result"].getBoundingClientRect = () => ({top: 600, bottom: 840});
    nodes["experiment-output-side"].getBoundingClientRect = () => ({top: 400});
    current = completed("generator-1"); await run("pollExperiment()");
    assert.equal(panel.scrollTop, 488, "reveal the current generator result, not the earlier output controls");
    assert.equal(nodes["experiment-result-text"].textContent, resultText, "never parse/stringify the result's huge integers");
    assert.equal(nodes["experiment-result-text"].querySelectorAll("img").length, 0);
    assert.match(nodes["experiment-generator-submitted"].textContent, /3 次 next/);
    panel.scrollTop = 17;
    const beforeDraft = requests.length; choose(6);
    assert.equal(requests.length, beforeDraft); assert.equal(nodes["experiment-input-state"].hidden, false);
    await run("pollExperiment()"); assert.equal(nodes["experiment-generator-steps"].value, "6");
    assert.equal(panel.scrollTop, 17, "draft edits and polling do not reveal the same generator result again");
    delete panel.clientHeight; delete panel.scrollHeight; panel.scrollTop = 0;
    assert.match(nodes["experiment-generator-submitted"].textContent, /3 次 next/);
    assert.equal(nodes["experiment-input"].value, input); assert.equal(reading(), kept);

    // Reload recovers a non-preset API limit; a later completed-job GET cannot erase an edited draft.
    current = completed("generator-reload", 4); run("resetExperiment()"); await new Promise(resolve => setImmediate(resolve));
    assert.equal(nodes["experiment-generator-steps"].value, "4");
    assert.equal(nodes["experiment-input"].value, input); assert.equal(count("/api/experiment/run"), 1);
    held = deferred(); const late = run("pollExperiment()"); choose(12); choose(0);
    current = completed("generator-late", 2); held.resolve(reply(current)); await late; held = null;
    assert.equal(nodes["experiment-generator-steps"].value, "0", "late recovery must preserve an ABA mode draft");
    assert.match(nodes["experiment-generator-submitted"].textContent, /2 次 next/);
    assert.equal(reading(), kept);

    // Exact integer mode and incompatible report combinations are rejected, not normalized.
    for (const value of [0, 13, -1, 1.5, true, "3", null]) {
      context.generatorBad = {...target, generator_steps: value};
      assert.equal(run("validExperimentTarget(generatorBad,generatorProject,true)"), false);
    }
    for (const value of [1, 4, 12]) {
      context.generatorBad = {...target, generator_steps: value};
      assert.equal(run("validExperimentTarget(generatorBad,generatorProject,true)"), true);
    }
    for (const extra of [{trace_lines: true}, {module_set: {}}, {mode: "head_current"}, {search: {}}]) {
      context.generatorBad = {...target, generator_steps: 3, ...extra};
      assert.equal(run("validExperimentGenerator(generatorBad)"), false);
    }
    context.generatorBad = {...target, generator_steps: 3, trace_lines: false};
    assert.equal(run("validExperimentGenerator(generatorBad)"), true);
    assert.equal(run("sameExperimentIdentity(generatorTarget,generatorBad)"), false);

    // A stays visible and explicitly clearable; generator reports cannot become A or produce a verdict.
    const baseline = {...target, id: "ordinary-a", input_text: input, result_text: '{"kind":"return","value":1}',
      input_sha256: "b".repeat(64), runtime_sha256: "c".repeat(64), report_sha256: "d".repeat(64), trace_lines: false};
    current = {...current, input_comparison: {revision: 1, baseline, current_id: current.id, can_pin: false, settling: false,
      outcome: "unavailable", reason: "current_unavailable", current_report_sha256: null}};
    await run("pollExperiment()");
    assert.equal(nodes["experiment-baseline-a"].hidden, false);
    assert.equal(nodes["experiment-baseline-a-result"].textContent, baseline.result_text);
    assert.equal(nodes["experiment-baseline-clear"].disabled, false); assert.equal(nodes["experiment-baseline-pin"].disabled, true);
    assert.match(nodes["experiment-baseline-summary"].textContent, /Generator.*不與 A 比較/);
    assert.equal(nodes["experiment-panel"].dataset.baseline, "true");
    assert.equal(nodes["experiment-panel"].dataset.generator, "true");
    assert.equal(nodes["experiment-generator-steps"].value, "0", "current generator layout survives a next-call draft switched off");
    const layoutRequests = requests.length, retainedLayout = reading();
    choose(6); choose(0); run("renderExperiment()");
    assert.equal(nodes["experiment-panel"].dataset.generator, "true");
    assert.equal(nodes["experiment-baseline-a"].hidden, false);
    assert.equal(nodes["experiment-baseline-a-result"].textContent, baseline.result_text);
    assert.equal(nodes["experiment-result-text"].textContent, resultText);
    assert.equal(requests.length, layoutRequests); assert.equal(reading(), retainedLayout);
    context.generatorCurrent = current;
    for (const extra of [{can_pin: true}, {current_report_sha256: "e".repeat(64)}, {outcome: "same", reason: null}, {reason: "same_run"}]) {
      context.generatorBad = {...current, input_comparison: {...current.input_comparison, ...extra}};
      assert.equal(run("validExperimentInputComparison(generatorBad,generatorProject)"), false);
    }
    assert.equal(run("validExperimentInputComparison(generatorCurrent,generatorProject)"), true);
    choose(3); const runsBeforeClear = count("/api/experiment/run");
    assert.equal(nodes["experiment-run"].disabled, false, "an ordinary A need not be cleared to run a generator");
    assert.equal(nodes["experiment-baseline-a"].hidden, false); await nodes["experiment-baseline-clear"].listeners.click();
    assert.equal(count("/api/experiment/run"), runsBeforeClear); assert.equal(nodes["experiment-baseline-a"].hidden, true);

    // Lost admission cannot be reconciled by an old/idle/different-mode response or repeated POST.
    runReply = () => {throw Error("lost generator reply");};
    await nodes["experiment-run"].listeners.click(); const sentCount = count("/api/experiment/run");
    assert.equal(run("state.experiment.unknown"), true); await nodes["experiment-run"].listeners.click();
    assert.equal(count("/api/experiment/run"), sentCount);
    current = {id: null, status: "idle"}; await run("pollExperiment()");
    assert.equal(run("state.experiment.unknown"), true);
    current = completed("generator-recovered", 4); await run("pollExperiment()");
    assert.equal(run("state.experiment.unknown"), true); assert.equal(run("state.experiment.job"), null);
    current = completed("generator-recovered", 3); await run("pollExperiment()");
    assert.equal(run("state.experiment.unknown"), false); assert.equal(run("state.experiment.job.generator_steps"), 3);
    current = {...current, generator_steps: 4}; await run("pollExperiment()");
    assert.equal(run("state.experiment.unknown"), true); assert.equal(run("state.experiment.job.generator_steps"), 3);
    current = {...current, generator_steps: 3}; await run("pollExperiment()");

    // Turning off does not accidentally resubmit the prior generator mode.
    choose(0); await nodes["experiment-run"].listeners.click();
    assert.equal(Object.hasOwn(requests.filter(item => item.route === "/api/experiment/run").at(-1).body, "generator_steps"), false);
    current = completed("ordinary-recovered", 3); await run("pollExperiment()");
    assert.equal(run("state.experiment.unknown"), true);
    current = completed("ordinary-recovered", 0); await run("pollExperiment()");
    assert.equal(run("state.experiment.unknown"), false); assert.equal(nodes["experiment-generator-steps"].value, "0");
    assert.match(nodes["experiment-generator-submitted"].textContent, /不推進/);
    assert.equal(nodes["experiment-panel"].dataset.generator, "false", "an ordinary accepted job restores the original A/B layout");

    current = {...current, input_comparison: {revision: 3, baseline: null, current_id: current.id, can_pin: true, settling: false,
      outcome: "unavailable", reason: "no_baseline", current_report_sha256: "e".repeat(64)}};
    await run("pollExperiment()"); assert.equal(nodes["experiment-baseline-pin"].disabled, false);
    choose(3); assert.equal(nodes["experiment-baseline-pin"].disabled, true, "generator draft cannot pin an earlier ordinary report");
    choose(0);

    choose(3); run("state.experiment.traceDraft=true;experimentControls()");
    assert.equal(nodes["experiment-run"].disabled, true); assert.equal(nodes["experiment-generator-steps"].disabled, false);
    choose(0); assert.equal(run("state.experiment.generatorDraft"), 0, "an incompatible recovered mode can always be turned off");
    run("state.experiment.traceDraft=false;experimentControls()");

    // Existing trace/module drafts require an explicit return to single-file/no-trace mode.
    nodes["experiment-trace-lines"].checked = true; nodes["experiment-trace-lines"].listeners.change();
    choose(3); assert.equal(run("state.experiment.generatorDraft"), 0);
    nodes["experiment-trace-lines"].checked = false; nodes["experiment-trace-lines"].listeners.change();
    run("state.experiment.moduleDraft=['1'];experimentControls()");
    assert.equal(nodes["experiment-generator-option"].hidden, true); choose(3); assert.equal(run("state.experiment.generatorDraft"), 0);
    run("state.experiment.moduleDraft=[];experimentControls()"); choose(3);
    runReply = () => ({ok: false, status: 409, json: async () => ({error: "generator source refused"})});
    await nodes["experiment-run"].listeners.click();
    assert.equal(run("state.experiment.unknown"), false); assert.equal(run("state.experiment.submission"), null);
    assert.match(nodes["experiment-status"].textContent, /generator source refused.*服務拒絕試跑/);
    assert.equal(nodes["experiment-input"].value, input); assert.equal(reading(), kept);
  } finally { context.fetch = originalFetch; }
}
async function checkInlineExperiments(context, nodes) {
  const run = code => vm.runInContext(code, context), requests = [], originalFetch = context.fetch;
  const reply = value => ({ok: true, json: async () => value});
  const deferred = () => {let resolve; const promise = new Promise(done => {resolve = done;}); return {promise, resolve};};
  const files = [{id: "0", path: "owned.py", lines: 2}, {id: "1", path: "other.py", lines: 2}];
  const item = name => ({name, kind: "function", start_line: 1, definition_line: 1, end_line: 2, stub: false});
  const sources = files.map((file, index) => ({...file, file: file.id, version: "experiment-v1",
    lines: [`def ${index ? "other" : "echo"}(value):`, "    return value"],
    outline: {status: "available", items: [item(index ? "other" : "echo")]}}));
  const target = body => ({file: body.file, path: files[Number(body.file)].path, version: body.version,
    entry: body.entry, source_sha256: (body.file === "0" ? "a" : "b").repeat(64), source_bytes: 40});
  let current = {id: null, status: "idle"}, offline = false, prepareReply = body => reply(target(body));
  let runReply = body => {current = {...target(body), id: "experiment-one", status: "running", input_text: body.input_text, elapsed_seconds: 0}; return reply({id: current.id});};
  context.fetch = async (route, options) => {
    const request = {route, method: options.method, body: options.body === undefined ? undefined : JSON.parse(options.body)};
    requests.push(request);
    if (route.startsWith("/api/source?")) return reply(sources[Number(new URLSearchParams(route.split("?")[1]).get("file"))]);
    if (route === "/api/experiment/prepare") return prepareReply(request.body);
    if (route === "/api/experiment/run") return runReply(request.body);
    if (route === "/api/experiment/current") {if (offline) throw Error("owned status unavailable"); return reply(current);}
    if (route === "/api/experiment/cancel") {assert.deepEqual(request.body, {id: current.id}); current = {...current, status: "cancelled"}; return reply({status: "cancelled"});}
    throw Error(`unexpected experiment-side request: ${route}`);
  };
  const count = route => requests.filter(request => request.route === route).length;
  const experimentButtons = () => nodes["outline-list"].querySelectorAll("button").filter(button => button.dataset.experimentEntry !== undefined);
  context.experimentProject = {name: "owned-experiment", version: "experiment-v1", files, excluded: []};

  // 1. Normal reading is default-off: neither rendering nor a stale explicit handler runs anything.
  run("showProject(experimentProject)"); await run("openFile(state.project.files[0],state.project.version)"); requests.length = 0;
  run("renderOutline(); controls()"); assert.equal(nodes["experiment-panel"].hidden, true); assert.equal(experimentButtons().length, 0);
  await run("openExperiment(state.source.outline.items[0],state.source)"); assert.equal(requests.length, 0);

  // 2. Opt-in exposes preparation only; the existing answer/draft/selected ranges remain owned by reading.
  run("showProject({...experimentProject,experiments_enabled:true})"); await run("openFile(state.project.files[0],state.project.version)");
  context.experimentPrevious = {...answered("reader-previous", "KEEP_READING_ANSWER"), version: "experiment-v1", files};
  nodes.question.value = "KEEP_MY_QUESTION";
  run('renderJob(experimentPrevious); state.focus=[{file:"0",path:"owned.py",start:1,end:2}]; state.anchor=1; state.end=2; renderSelections()');
  const readingState = () => run("JSON.stringify({job:state.job,focus:state.focus,history:state.history,selected:state.selectedHistory})");
  const readingBefore = readingState(), answerBefore = nodes.answer.textContent;
  const unchanged = () => {assert.equal(readingState(), readingBefore); assert.equal(nodes.answer.textContent, answerBefore); assert.equal(nodes.question.value, "KEEP_MY_QUESTION");};
  requests.length = 0;
  assert.equal(experimentButtons().length, 1); await experimentButtons()[0].listeners.click();
  assert.deepEqual(requests.map(request => request.route), ["/api/experiment/prepare"]);
  assert.deepEqual(requests[0].body, {file: "0", version: "experiment-v1", entry: "echo"});
  assert.equal(nodes["experiment-panel"].hidden, false); assert.equal(run("state.anchor"), 1); assert.equal(run("state.end"), 2); unchanged();

  // 3. One explicit click creates one POST even if clicked again while its reply is pending.
  const input = '{"args":[9007199254740993],"kwargs":{}}\n';
  nodes["experiment-input"].value = input; nodes["experiment-input"].listeners.input();
  const submitted = deferred(), normalReply = runReply;
  runReply = body => {normalReply(body); return submitted.promise;};
  const first = nodes["experiment-run"].listeners.click();
  await nodes["experiment-run"].listeners.click();
  submitted.resolve(reply({id: "experiment-one"})); await first;
  assert.equal(count("/api/experiment/run"), 1);
  const sent = requests.find(request => request.route === "/api/experiment/run").body;
  assert.deepEqual(sent, {file: "0", version: "experiment-v1", entry: "echo", source_sha256: "a".repeat(64), input_text: input, allow_execution: true});
  assert.equal(nodes["experiment-input"].value, input); unchanged();

  // 4. Preserve the server's JSON TEXT, including integers beyond Number.MAX_SAFE_INTEGER and inert markup.
  const resultText = '{\n  "kind": "return",\n  "value": 9007199254740993,\n  "note": "<img src=x onerror=alert(1)>"\n}';
  current = {...current, status: "completed", result_text: resultText, stdout: "literal <script>owned</script>", stderr: "",
    source_unchanged: true, runtime_unchanged: true, elapsed_seconds: 1.25};
  const panel = nodes["experiment-panel"];
  panel.clientHeight = 200; panel.scrollHeight = 800;
  panel.getBoundingClientRect = () => ({top: 100, bottom: 300});
  nodes["experiment-result"].getBoundingClientRect = () => ({top: 500, bottom: 740});
  nodes["experiment-output-side"].getBoundingClientRect = () => ({top: 450});
  await run("pollExperiment()");
  assert.equal(nodes["experiment-result-text"].textContent, resultText);
  assert.equal(nodes["experiment-result-text"].querySelectorAll("img").length, 0);
  assert.equal(nodes["experiment-stdout"].textContent, current.stdout); assert.equal(count("/api/experiment/run"), 1); unchanged();
  assert.equal(panel.scrollTop, 338, "new result reveals the outcome inside the bounded panel");
  panel.scrollTop = 17; run("renderExperiment()");
  assert.equal(panel.scrollTop, 17, "later rendering must not override the reader's scroll position");
  delete panel.clientHeight; delete panel.scrollHeight;
  assert.equal(nodes["experiment-input-state"].hidden, true);
  const beforeEditing = requests.length;
  nodes["experiment-input"].value = '{"args":[1],"kwargs":{}}'; nodes["experiment-input"].listeners.input();
  assert.equal(nodes["experiment-input-state"].hidden, false, "changed draft must not appear to own the old result");
  assert.equal(nodes["experiment-result-text"].textContent, resultText);
  assert.equal(requests.length, beforeEditing, "editing never submits or polls");
  nodes["experiment-input"].value = input; nodes["experiment-input"].listeners.input();
  assert.equal(nodes["experiment-input-state"].hidden, true); unchanged();

  // 5. A lost POST reply is unknown, not permission to submit again; GET recovers its exact new job.
  runReply = body => {current = {...target(body), id: "experiment-two", status: "running", input_text: body.input_text, elapsed_seconds: 0}; offline = true; throw Error("owned lost run reply");};
  await nodes["experiment-run"].listeners.click();
  assert.equal(count("/api/experiment/run"), 2); assert.equal(run("state.experiment.unknown"), true);
  await nodes["experiment-run"].listeners.click(); assert.equal(count("/api/experiment/run"), 2);
  assert.equal(nodes["experiment-recheck"].hidden, false); offline = false;
  await nodes["experiment-recheck"].listeners.click();
  assert.equal(run("state.experiment.job.id"), "experiment-two"); assert.equal(run("state.experiment.unknown"), false);
  assert.equal(count("/api/experiment/run"), 2); unchanged();

  // 6. Polling, browsing another source, and attempted refresh never cancel or retarget the active job.
  assert.equal(count("/api/experiment/cancel"), 0); const beforeRefresh = requests.length;
  await nodes.refresh.listeners.click(); assert.equal(requests.length, beforeRefresh);
  await run("openFile(state.project.files[1],state.project.version)"); await run("pollExperiment()");
  assert.equal(run("state.experiment.job.id"), "experiment-two"); assert.equal(run("state.experiment.target.file"), "0");
  assert.equal(count("/api/experiment/cancel"), 0);
  await nodes["experiment-cancel"].listeners.click(); await run("pollExperiment()");
  assert.equal(count("/api/experiment/cancel"), 1); assert.equal(run("state.experiment.job.status"), "cancelled");
  assert.equal(count("/api/experiment/run"), 2); unchanged();

  // 7. A same-version refresh and a newer prepare invalidate a late older target response.
  const delayed = deferred(); prepareReply = () => delayed.promise;
  await run("openFile(state.project.files[0],state.project.version)");
  const stale = run("openExperiment(state.source.outline.items[0],state.source)");
  run("showProject({...experimentProject,experiments_enabled:true},true)");
  await run("openFile(state.project.files[1],state.project.version)"); prepareReply = body => reply(target(body));
  await run("openExperiment(state.source.outline.items[0],state.source)");
  const newIdentity = nodes["experiment-identity"].textContent;
  delayed.resolve(reply(target({file: "0", version: "experiment-v1", entry: "echo"}))); await stale;
  assert.equal(run("state.experiment.target.file"), "1"); assert.equal(run("state.experiment.target.entry"), "other");
  assert.equal(nodes["experiment-identity"].textContent, newIdentity); assert.equal(count("/api/experiment/run"), 2);
  assert.equal(count("/api/experiment/cancel"), 1); assert.equal(nodes["experiment-run"].disabled, false);

  // Cleanup failure remains locked even though the server's Python thread ended.
  current = {...target({file: "1", version: "experiment-v1", entry: "other"}), id: "unknown-cleanup",
    status: "incomplete", cleanup_unknown: true, input_text: input, elapsed_seconds: 2,
    source_unchanged: false, runtime_unchanged: false};
  await run("pollExperiment()");
  for (const id of ["experiment-run", "experiment-close", "ask", "refresh", "change-mode"]) assert.equal(nodes[id].disabled, true);
  assert.equal(nodes["experiment-cancel"].hidden, true);
  assert(nodes["experiment-status"].textContent.includes("無法確認原生程序"));
  assert(nodes["experiment-status"].textContent.includes("保留快照或輸入"));
  assert(nodes["experiment-status"].textContent.includes("執行環境完整性"));
  await nodes["experiment-recheck"].listeners.click();
  assert.equal(count("/api/experiment/run"), 2); assert.equal(count("/api/experiment/cancel"), 1);
  context.fetch = originalFetch;
}
async function checkExperimentBaseline(context, nodes) {
  const run = code => vm.runInContext(code, context), originalFetch = context.fetch, requests = [];
  const clone = value => JSON.parse(JSON.stringify(value)), reply = value => ({ok: true, status: 200, json: async () => clone(value)});
  const deferred = () => {let resolve; const promise = new Promise(done => {resolve = done;}); return {promise, resolve};};
  const project = {name: "baseline fixture", version: "baseline-v1", experiments_enabled: true,
    files: [{id: "0", path: "probe.py", lines: 2}, {id: "1", path: "helper.py", lines: 1}], excluded: []};
  const target = {file: "0", path: "probe.py", version: project.version, entry: "probe", source_sha256: "a".repeat(64), source_bytes: 36};
  const inputA = '{"args":[9007199254740993],"kwargs":{}}\n', inputB = '{"args":[9007199254740995],"kwargs":{}}';
  const resultA = '{"kind":"return","value":9007199254740993,"label":"<img src=x>原文"}', resultB = '{"kind":"return","value":9007199254740995}';
  const complete = (id, input, result) => ({...target, id, status: "completed", input_text: input, result_text: result, elapsed_seconds: 1,
    source_unchanged: true, runtime_unchanged: true, process_status: "passed", host_status: "exited", stdout: "private log", stderr: ""});
  let current = complete("trial-a", inputA, resultA), baseline = null, revision = 0, settling = false, outcome = "different";
  let statusOverride = null, statusOffline = false, mutationBehavior = "normal", pendingMutation = null;
  const capture = job => ({...target, id: job.id, input_text: job.input_text, result_text: job.result_text, input_sha256: "b".repeat(64),
    runtime_sha256: "d".repeat(64), report_sha256: (job.id === "trial-a" ? "c" : "f").repeat(64), trace_lines: job.trace_lines === true});
  const projection = () => ({revision, baseline: baseline && clone(baseline), current_id: current.id,
    can_pin: current.status === "completed" && !settling, settling,
    outcome: baseline && baseline.id !== current.id && current.status === "completed" && !settling ? outcome : "unavailable",
    reason: !baseline ? "no_baseline" : baseline.id === current.id ? "same_run" : current.status !== "completed" || settling ? "current_unavailable" : null,
    current_report_sha256: current.status === "completed" && !settling ? capture(current).report_sha256 : null});
  const status = () => ({...current, input_comparison: projection()});
  context.fetch = async (route, options) => {
    const body = options.body === undefined ? undefined : JSON.parse(options.body); requests.push({route, method: options.method, body});
    if (route === "/api/experiment/current") {if (statusOffline) throw Error("baseline status unavailable"); return reply(statusOverride || status());}
    if (route === "/api/experiment/baseline" || route === "/api/experiment/baseline/clear") {
      assert.deepEqual(body, {id: route.endsWith("/clear") ? baseline.id : current.id, version: project.version, revision});
      if (mutationBehavior === "pending") return pendingMutation.promise;
      if (mutationBehavior === "reject") return {ok: false, status: 409, json: async () => ({error: "baseline revision refused"})};
      baseline = route.endsWith("/clear") ? null : capture(current); revision++;
      if (mutationBehavior === "lost") throw Error("lost baseline reply");
      return reply(projection());
    }
    if (route === "/api/experiment/run") {
      assert.equal(body.input_text, inputB); assert.equal(body.allow_execution, true);
      assert.deepEqual(Object.keys(body).sort(), ["allow_execution", "entry", "file", "input_text", "source_sha256", "version"]);
      current = {...target, id: "trial-b", status: "running", input_text: body.input_text, elapsed_seconds: 0}; return reply({id: current.id});
    }
    if (route === "/api/experiment/cancel") {assert.deepEqual(body, {id: current.id}); current = {...current, status: "cancelled"}; return reply({status: "cancelled"});}
    if (route === "/api/refresh") {assert.deepEqual(body, {}); baseline = null; revision++; return reply(project);}
    throw Error(`unexpected baseline request: ${route}`);
  };
  context.baselineProject = project;
  const reset = async () => {
    run("showProject(baselineProject,true); $('question').value='UNSENT_QUESTION'; $('answer').textContent='UNCHANGED_ANSWER'; state.focus=[{file:'0',path:'probe.py',start:1,end:2}]; renderSelections()");
    await run("pollExperiment()");
  };
  await reset();
  const reading = () => run("JSON.stringify([state.project.version,state.focus,state.job,state.history,$('question').value,$('answer').textContent])"), originalReading = reading();
  const count = route => requests.filter(item => item.route === route).length;
  assert.equal(nodes["experiment-baseline-pin"].disabled, false); assert.equal(nodes["experiment-baseline-a"].hidden, true);
  // Pin records the submitted call, not the next draft, and never runs a guest.
  nodes["experiment-input"].value = inputB; nodes["experiment-input"].listeners.input();
  await nodes["experiment-baseline-pin"].listeners.click();
  assert.equal(count("/api/experiment/run"), 0); assert.equal(nodes["experiment-baseline-a-input"].textContent, inputA);
  assert.equal(nodes["experiment-baseline-a-result"].textContent, resultA); assert.equal(nodes["experiment-baseline-a-result"].querySelectorAll("img").length, 0);
  assert.equal(nodes["experiment-input"].value, inputB); assert.equal(nodes["experiment-baseline-pin"].disabled, true);
  assert(nodes["experiment-baseline-summary"].textContent.includes("A 已固定")); assert.equal(nodes["experiment-result"].hidden, true, "A is not duplicated as a new B");
  assert.equal(nodes["experiment-logs"].hidden, false, "pinning current A must not hide its original logs"); assert.equal(reading(), originalReading);
  const pinned = run("JSON.stringify(state.experiment.baseline)");
  await nodes["experiment-run"].listeners.click();
  assert.equal(count("/api/experiment/run"), 1); assert.equal(run("JSON.stringify(state.experiment.baseline)"), pinned);
  assert.equal(nodes["experiment-result"].hidden, true); assert.equal(nodes["experiment-baseline-b-input-text"].textContent, inputB);
  assert(!nodes["experiment-baseline-summary"].textContent.includes("回報相同")); assert.equal(reading(), originalReading);
  current = complete("trial-b", inputB, resultB); await run("pollExperiment()");
  assert(nodes["experiment-baseline-summary"].textContent.includes("回報不同")); assert.equal(nodes["experiment-result-text"].textContent, resultB);
  assert.equal(nodes["experiment-baseline-a-input"].textContent, inputA); assert.equal(nodes["experiment-baseline-b-input-text"].textContent, inputB);
  nodes["experiment-input"].value = '{"args":[0],"kwargs":{}}'; nodes["experiment-input"].listeners.input(); await run("pollExperiment()");
  assert.equal(nodes["experiment-baseline-b-input-text"].textContent, inputB); assert.equal(nodes["experiment-input"].value, '{"args":[0],"kwargs":{}}');
  assert(nodes["experiment-baseline-pin"].textContent.includes("取代 A"));
  await nodes["experiment-baseline-pin"].listeners.click(); assert.equal(nodes["experiment-baseline-a-input"].textContent, inputB);
  await nodes["experiment-baseline-clear"].listeners.click(); assert.equal(nodes["experiment-baseline-a"].hidden, true);
  assert.equal(nodes["experiment-result"].hidden, false); assert.equal(count("/api/experiment/run"), 1); assert.equal(reading(), originalReading);

  // A lost mutation cannot be resolved by a single old revision, and never becomes execution uncertainty.
  statusOverride = clone(status()); mutationBehavior = "lost";
  await nodes["experiment-baseline-pin"].listeners.click();
  assert.equal(run("state.experiment.baselineUnknown"), true); assert.equal(run("state.experiment.unknown"), false);
  assert.equal(nodes["experiment-baseline-pin"].disabled, true); assert.equal(nodes["experiment-run"].disabled, true);
  assert.equal(nodes.refresh.disabled, false); assert.equal(nodes.question.disabled, false); assert.equal(nodes["experiment-recheck"].hidden, false);
  const pins = count("/api/experiment/baseline"); await nodes["experiment-baseline-pin"].listeners.click(); assert.equal(count("/api/experiment/baseline"), pins);
  statusOffline = true; await nodes["experiment-recheck"].listeners.click(); assert.equal(run("state.experiment.unknown"), false);
  assert.equal(nodes.question.disabled, false); statusOffline = false; statusOverride = null; mutationBehavior = "normal";
  await nodes["experiment-recheck"].listeners.click(); assert.equal(run("state.experiment.baselineUnknown"), false);
  assert.equal(nodes["experiment-baseline-a-input"].textContent, inputB); assert.equal(count("/api/experiment/baseline"), pins);
  // A readable 4xx is a refused mutation, not a reason to keep waiting for a revision increase.
  mutationBehavior = "reject"; await nodes["experiment-baseline-clear"].listeners.click();
  assert.equal(run("state.experiment.baselineUnknown"), false); assert(nodes["experiment-baseline-summary"].textContent.includes("revision refused"));
  assert.equal(nodes["experiment-baseline-a"].hidden, false); mutationBehavior = "normal";

  // Reload uses current GET only and recovers A; target/draft changes do not rewrite it.
  const beforeReload = requests.length; await reset();
  assert.deepEqual(requests.slice(beforeReload).map(item => [item.method, item.route]), [["GET", "/api/experiment/current"]]);
  assert.equal(nodes["experiment-baseline-a-input"].textContent, inputB);
  // Module-set jobs intentionally omit the single-file extension. They remain
  // usable, while the prior single-file baseline is retained without a verdict.
  const beforeModule = requests.length, moduleBaseline = run("JSON.stringify(state.experiment.baseline)");
  statusOverride = {...complete("trial-module", inputB, resultB), module_set: {import_root: ".", entry_path: target.path,
    entry: target.entry, entry_module: "probe", sha256: "e".repeat(64), files: [
      {path: target.path, sha256: target.source_sha256, size_bytes: target.source_bytes},
      {path: "helper.py", sha256: "f".repeat(64), size_bytes: 19}]}};
  await run("pollExperiment()");
  assert.equal(run("state.experiment.baselineUnknown"), false); assert.equal(run("state.experiment.unknown"), false);
  assert.equal(run("state.experiment.inputComparison"), null); assert.equal(run("JSON.stringify(state.experiment.baseline)"), moduleBaseline);
  assert.equal(nodes["experiment-baseline-controls"].hidden, true); assert.equal(nodes["experiment-run"].disabled, false);
  assert.equal(nodes["experiment-module-prepare"].disabled, false); assert.equal(nodes["experiment-result-text"].textContent, resultB);
  statusOverride = null; await run("pollExperiment()");
  assert.equal(nodes["experiment-baseline-a-input"].textContent, inputB); assert.equal(run("JSON.stringify(state.experiment.baseline)"), moduleBaseline);
  assert.deepEqual(requests.slice(beforeModule).map(item => [item.method, item.route]), [["GET", "/api/experiment/current"], ["GET", "/api/experiment/current"]]);
  run("state.experiment.target={...state.experiment.target,entry:'other'}; renderExperiment()");
  assert.equal(nodes["experiment-baseline-a"].hidden, true); assert(nodes["experiment-baseline-summary"].textContent.includes("probe.py"));
  assert.equal(run("state.experiment.baseline.entry"), "probe");
  run("state.experiment.target=state.experiment.job; renderExperiment()");
  assert.equal(nodes["experiment-baseline-a"].hidden, false);
  // Closing an ordinary worker has a distinct settling phase; do not stop polling early.
  baseline = null; revision++; current = complete("trial-c", inputA, resultA); settling = true; await reset();
  assert.equal(nodes["experiment-baseline-pin"].disabled, true); assert.equal(nodes["experiment-run"].disabled, true);
  assert(nodes["experiment-baseline-summary"].textContent.includes("收尾")); settling = false; await run("pollExperiment()");
  assert.equal(nodes["experiment-baseline-pin"].disabled, false);
  await nodes["experiment-baseline-pin"].listeners.click();
  current = {...target, id: "trial-d", status: "running", input_text: inputB, elapsed_seconds: 0}; await run("pollExperiment()");
  await nodes["experiment-cancel"].listeners.click();
  assert.equal(nodes["experiment-baseline-a-input"].textContent, inputA); assert(nodes["experiment-baseline-summary"].textContent.includes("尚無可比較"));
  assert.equal(nodes["experiment-baseline-pin"].disabled, true); assert.equal(count("/api/experiment/run"), 1);

  // Validate all metadata before displaying a verdict; inputs/results stay text, never JSON-number conversion.
  current = complete("trial-e", inputB, resultB); await reset();
  const valid = status(); context.baselineValid = clone(valid); assert.equal(run("validExperimentInputComparison(baselineValid)"), true);
  // Host-valid near-limit views need room for the separately bounded envelope.
  const nearLimit = clone(valid), publicBytes = value => Buffer.byteLength(JSON.stringify(value), "utf8");
  nearLimit.input_comparison.baseline.result_text = '{"return":""}';
  nearLimit.input_comparison.baseline.result_text = JSON.stringify({return: "x".repeat(131072 - 64 - publicBytes(nearLimit.input_comparison.baseline))});
  // Python's default JSON separators add one space per colon/comma in this flat view.
  assert(publicBytes(nearLimit.input_comparison.baseline) + 2 * Object.keys(nearLimit.input_comparison.baseline).length - 1 <= 131072);
  assert(publicBytes(nearLimit.input_comparison) > 131072); assert(publicBytes(nearLimit.input_comparison) <= 131072 + 2048);
  context.baselineNearLimit = nearLimit; assert.equal(run("validExperimentInputComparison(baselineNearLimit)"), true);
  const oversizedView = clone(nearLimit); oversizedView.input_comparison.baseline.result_text += "x".repeat(65);
  context.baselineOversizedView = oversizedView; assert.equal(run("validExperimentInputComparison(baselineOversizedView)"), false, "the view itself still has the original 128 KiB limit");
  const invalid = [job => job.input_comparison.current_id = "other", job => job.input_comparison.revision = -1,
    job => job.input_comparison.baseline.input_sha256 += "\n", job => job.input_comparison.baseline.path = "not-admitted.py",
    job => job.input_comparison.baseline.input_text = "x".repeat(16385), job => job.input_comparison.baseline.result_text = "x".repeat(131073),
    job => job.input_comparison.baseline.trace_lines = "false", job => job.input_comparison.baseline.source_bytes = 65537,
    job => job.input_comparison.current_report_sha256 = null, job => job.input_comparison.settling = true,
    job => job.input_comparison.baseline.id = job.id, job => job.module_set = {}];
  for (const mutate of invalid) {const job = clone(valid); mutate(job); context.baselineInvalid = job; assert.equal(run("validExperimentInputComparison(baselineInvalid)"), false);}
  const preservedA = run("JSON.stringify(state.experiment.baseline)"); statusOverride = clone(valid); statusOverride.input_comparison.baseline.result_text = "changed without revision";
  await run("pollExperiment()"); assert.equal(run("JSON.stringify(state.experiment.baseline)"), preservedA);
  assert.equal(run("state.experiment.baselineUnknown"), true); assert.equal(run("state.experiment.unknown"), false);
  assert(!nodes["experiment-baseline-summary"].textContent.includes("回報不同")); statusOverride = null;
  await run("pollExperiment()");
  // A same-version refresh invalidates an in-flight mutation, without a late GET repopulating A.
  mutationBehavior = "pending"; pendingMutation = deferred();
  const late = nodes["experiment-baseline-pin"].listeners.click(); const beforeRefresh = requests.length;
  await nodes.refresh.listeners.click(); const refreshedRequests = requests.length;
  assert.equal(requests[beforeRefresh].route, "/api/refresh"); assert.equal(nodes["experiment-baseline-a"].hidden, true);
  pendingMutation.resolve(reply({})); await late;
  assert.equal(requests.length, refreshedRequests); assert.equal(run("state.experiment.baseline"), undefined);
  assert.equal(nodes.question.value, "UNSENT_QUESTION"); assert.equal(count("/api/experiment/run"), 1);
  // A's line inspector remains available after pinning; it is not labelled as B.
  mutationBehavior = "normal"; baseline = null; revision++;
  current = {...complete("trial-trace", inputA, resultA), trace_lines: true,
    reported_trace: {line_events: [1, 2, 2], truncated: false, hook_intact: true}};
  await reset(); await nodes["experiment-baseline-pin"].listeners.click();
  assert.equal(nodes["experiment-baseline-b-title"].textContent, "目前回報就是 A；尚未執行 B");
  assert.equal(nodes["experiment-result"].hidden, true); assert.equal(nodes["experiment-logs"].hidden, false);
  assert.equal(nodes["experiment-trace"].hidden, false); assert.equal(nodes["experiment-trace-view"].disabled, false);
  assert.equal(nodes["experiment-trace-input-text"].textContent, inputA); assert(nodes["experiment-trace-summary"].textContent.includes("3 次行事件"));
  assert.equal(count("/api/experiment/run"), 1); assert.equal(reading(), originalReading);
  context.fetch = originalFetch;
}
async function checkPairedExperiments(context, nodes) {
  assert.match(html, /相同不代表程式等價；不同不證明由修改造成，也不驗證 AI 的解釋/);
  assert.match(html, /<details id="experiment-submitted-input" hidden><summary>本次已送出的輸入（唯讀）<\/summary>/);
  assert.match(html, /id="experiment-submitted-input-text" tabindex="0" aria-label="本次已送出的 JSON 輸入"/);
  const css = fs.readFileSync(path.join(root, "src/forge8/web/app.css"), "utf8");
  assert.match(css, /\.experiment-panel\s*\{[^}]*padding:\s*0 15px 12px;/, "top padding belongs to the opaque sticky heading, not an exposed scroll strip");
  assert.match(css, /\.experiment-heading\s*\{[^}]*position:\s*sticky;[^}]*top:\s*0;[^}]*padding:\s*12px 0 6px;/);
  const run = code => vm.runInContext(code, context), originalFetch = context.fetch, requests = [];
  const reply = value => ({ok: true, json: async () => value});
  const deferred = () => {let resolve; const promise = new Promise(done => {resolve = done;}); return {promise, resolve};};
  const files = [{id: "0", path: "before/owned.py", lines: 2}, {id: "1", path: "after/owned.py", lines: 2}];
  const comparison = {head: "d".repeat(40), original_snapshot_sha256: "e".repeat(64),
    catalogue: {schema_version: 1, kind: "source_change_catalogue", semantics_verified: false, files: []}};
  const target = {file: "1", path: files[1].path, version: "paired-v1", entry: "echo", source_sha256: "a".repeat(64), source_bytes: 40,
    mode: "head_current", head: comparison.head, current_snapshot_sha256: comparison.original_snapshot_sha256,
    before: {file: "0", path: files[0].path, source_sha256: "b".repeat(64), source_bytes: 42}};
  const input = ' {"args":[9007199254740993],"kwargs":{"text":"你好"}}\n';
  const beforeText = '{"kind":"return","value":9007199254740993}', afterText = '{"kind":"return","value":"<img src=x onerror=alert(1)>"}';
  const observation = (result_text, report = "f") => ({result_text, stdout: "literal <script>not executed</script>", stderr: "",
    process_status: "passed", host_status: "exited", source_unchanged: true, runtime_unchanged: true, complete: true, report_sha256: report.repeat(64)});
  const pending = id => ({...target, id, status: "running", phase: "checking", input_text: input, elapsed_seconds: 0,
    observations: {}, comparison_result: "unavailable", comparison_rule: "canonical-json-v1", comparison_unchanged: false});
  let current = {id: null, status: "idle"}, offline = false, prepareReply = () => reply(target);
  let runReply = () => {current = pending("pair-one"); return reply({id: current.id});};
  context.fetch = async (route, options) => {
    const body = options.body === undefined ? undefined : JSON.parse(options.body);
    requests.push({route, method: options.method, body});
    if (route.startsWith("/api/source?")) {
      const file = files[Number(new URLSearchParams(route.split("?")[1]).get("file"))];
      return reply({file: file.id, path: file.path, version: "paired-v1", lines: ["def echo(value):", "    return value"],
        outline: {status: "available", items: [{name: "echo", kind: "function", start_line: 1, definition_line: 1, end_line: 2, stub: false}]}});
    }
    if (route === "/api/experiment/prepare") return prepareReply(body);
    if (route === "/api/experiment/run") return runReply(body);
    if (route === "/api/experiment/current") {if (offline) throw Error("paired status unavailable"); return reply(current);}
    if (route === "/api/experiment/cancel") {
      assert.deepEqual(body, {id: current.id});
      current = {...current, status: "cancelled", comparison_result: "unavailable", comparison_unchanged: false};
      return reply({status: "cancelled"});
    }
    throw Error(`unexpected paired-side request: ${route}`);
  };
  const count = route => requests.filter(request => request.route === route).length;
  const buttons = () => nodes["outline-list"].querySelectorAll("button").filter(button => button.dataset.experimentEntry !== undefined);
  context.pairedProject = {name: "paired", version: target.version, files, excluded: [], comparison};
  run("showProject(pairedProject,true)"); await run("openFile(state.project.files[1],state.project.version)");
  assert.equal(buttons().length, 0, "comparison must not bypass explicit experiment opt-in");
  run("showProject({...pairedProject,experiments_enabled:true},true)");
  await run("openFile(state.project.files[0],state.project.version)");
  assert.equal(buttons().length, 0, "HEAD cannot launch an ambiguous ordinary trial");
  await run("openExperiment(state.source.outline.items[0],state.source)"); assert.equal(count("/api/experiment/prepare"), 0);
  await run("openFile(state.project.files[1],state.project.version)");
  context.pairedReading = {...answered("paired-reading", "KEEP_PAIRED_READING"), version: target.version, files, comparison};
  nodes.question.value = "KEEP_PAIRED_QUESTION";
  run('renderJob(pairedReading); state.focus=[{file:"0",path:"before/owned.py",start:1,end:2,role:"before"}]; renderSelections()');
  const reading = () => run("JSON.stringify({job:state.job,focus:state.focus,history:state.history,selected:state.selectedHistory})");
  const beforeReading = reading(), beforeAnswer = nodes.answer.textContent;
  const unchanged = () => {assert.equal(reading(), beforeReading); assert.equal(nodes.answer.textContent, beforeAnswer); assert.equal(nodes.question.value, "KEEP_PAIRED_QUESTION");};
  assert.equal(buttons().length, 1); assert.match(buttons()[0].textContent, /兩版/);
  await buttons()[0].listeners.click();
  assert.deepEqual(requests.find(request => request.route === "/api/experiment/prepare").body,
    {file: "1", version: target.version, entry: "echo", mode: "head_current"});
  assert.equal(count("/api/experiment/run"), 0); unchanged();
  assert.equal(nodes["experiment-modules"].hidden, true); assert.equal(nodes["experiment-call-input"].hidden, true);
  assert.equal(nodes["experiment-from-call"].disabled, true); assert.equal(nodes["experiment-module-prepare"].disabled, true);
  assert(nodes["experiment-identity"].textContent.includes(target.before.source_sha256));
  assert(nodes["experiment-identity"].textContent.includes(target.source_sha256));
  assert.match(nodes["experiment-run"].textContent, /兩份完整模組，各呼叫一次 echo/);
  assert.match(nodes["experiment-notice"].textContent, /兩個獨立 CPython\/WASI/);
  assert.equal(nodes["experiment-submitted-input"].hidden, true, "preparation is not a submitted input record");

  nodes["experiment-input"].value = input; nodes["experiment-input"].listeners.input();
  const submitted = deferred(); runReply = () => {current = pending("pair-one"); return submitted.promise;};
  const first = nodes["experiment-run"].listeners.click(); await nodes["experiment-run"].listeners.click();
  submitted.resolve(reply({id: "pair-one"})); await first;
  assert.equal(count("/api/experiment/run"), 1, "one consent authorizes one server-owned pair, never two browser runs");
  assert.deepEqual(requests.find(request => request.route === "/api/experiment/run").body,
    {file: "1", version: target.version, entry: "echo", source_sha256: target.source_sha256, input_text: input,
      allow_execution: true, mode: "head_current", head: comparison.head, before_sha256: target.before.source_sha256});
  assert.equal(nodes["experiment-input"].value, input); assert.match(nodes["experiment-status"].textContent, /正在核對兩側原碼、輸入與回報完整性/); unchanged();
  assert.equal(nodes["experiment-submitted-input"].hidden, false);
  assert.equal(nodes["experiment-submitted-input"].open, false);
  assert.equal(nodes["experiment-submitted-input-text"].textContent, input, "record keeps exact whitespace, Unicode and the large integer without parsing JSON");

  current = {...current, phase: "after", observations: {before: observation(beforeText)}, elapsed_seconds: 1};
  await run("pollExperiment()");
  assert.equal(nodes["experiment-before-result"].textContent, beforeText);
  assert.equal(nodes["experiment-after-result"].hidden, true);
  assert.match(nodes["experiment-pair-summary"].textContent, /尚無/); assert.match(nodes["experiment-status"].textContent, /正在處理目前版本/);
  assert.equal(nodes["experiment-input"].readOnly, true); assert.equal(nodes["change-mode"].disabled, true);
  assert.equal(count("/api/experiment/run"), 1, "displaying the first observation must not start the second guest");
  current = {...current, phase: "checking", observations: {...current.observations, after: observation(afterText, "c")}};
  await run("pollExperiment()");
  assert.match(nodes["experiment-status"].textContent, /正在核對兩側原碼、輸入與回報完整性/);
  assert.doesNotMatch(nodes["experiment-status"].textContent, /尚未進入試跑/);
  assert.match(nodes["experiment-pair-summary"].textContent, /尚無/, "two guest reports alone do not authorize a comparison verdict");
  current = {...current, status: "completed",
    comparison_result: "different", comparison_unchanged: true, elapsed_seconds: 2};
  await run("pollExperiment()");
  const completed = current;
  assert.match(nodes["experiment-pair-summary"].textContent, /回報不同/);
  assert.equal(nodes["experiment-before-result"].textContent, beforeText); assert.equal(nodes["experiment-after-result"].textContent, afterText);
  assert.equal(nodes["experiment-after-result"].querySelectorAll("img").length, 0);
  assert.equal(nodes["experiment-before-stdout"].textContent, current.observations.before.stdout);
  assert.equal(nodes["experiment-before-stdout"].querySelectorAll("script").length, 0);
  assert.equal(nodes["experiment-result"].hidden, true); assert.equal(nodes["experiment-logs"].hidden, true); unchanged();
  assert.match(nodes["experiment-before-status"].textContent, /工作程序正常結束/);
  assert.match(nodes["experiment-before-status"].textContent, /僅為執行環境狀態，不代表函式正確/);
  assert.doesNotMatch(nodes["experiment-before-status"].textContent, /passed|測試通過/);
  assert.match(run('experimentRuntimeStatus({process_status:"passed",host_status:"trap"})'), /WASI 已中止/);
  assert.match(run('experimentRuntimeStatus({process_status:"launch_error"})'), /啟動或清理錯誤/);
  const beforeDisclosure = requests.length;
  nodes["experiment-submitted-input"].open = true; run("renderExperiment()");
  assert.equal(nodes["experiment-submitted-input"].open, true);
  assert.equal(requests.length, beforeDisclosure, "opening the native disclosure and rendering send no request");
  nodes["experiment-before-logs"].open = true;
  const inputDraft = '{"args":[2],"kwargs":{}}';
  nodes["experiment-input"].value = inputDraft; nodes["experiment-input"].listeners.input();
  await run("pollExperiment()");
  assert.equal(nodes["experiment-input"].value, inputDraft, "status queries cannot replace a newer paired-input draft");
  assert.equal(nodes["experiment-input-state"].hidden, false); assert.equal(nodes["experiment-before-logs"].open, true);
  assert.equal(nodes["experiment-before-result"].textContent, beforeText);
  assert.equal(nodes["experiment-submitted-input-text"].textContent, input, "a newer draft must never relabel the submitted pair");
  assert.equal(nodes["experiment-submitted-input"].open, true, "status polling preserves the reader's disclosure state");
  nodes["experiment-input"].value = input; nodes["experiment-input"].listeners.input();

  current = {...completed, comparison_result: "same"}; await run("pollExperiment()");
  assert.match(nodes["experiment-pair-summary"].textContent, /回報相同/, "the UI reports the typed backend comparison, never parses/compares potentially rounded JSON values");
  current = {...completed, comparison_result: "unavailable", observations: {before: observation(beforeText), after: observation(undefined, "c")}};
  await run("pollExperiment()");
  assert.match(nodes["experiment-pair-summary"].textContent, /尚無/); assert.equal(nodes["experiment-after-result"].hidden, true);
  assert.match(nodes["experiment-after-status"].textContent, /沒有可比較的 JSON 回報/);

  // Same job, immutable pair: neither side, mode, HEAD nor current snapshot may silently change.
  current = completed; await run("pollExperiment()");
  const invalid = [{...completed, before: {...target.before, source_sha256: "f".repeat(64)}},
    {...completed, source_sha256: "f".repeat(64)}, {...completed, head: "f".repeat(40)},
    {...completed, current_snapshot_sha256: "f".repeat(64)}, {...completed, mode: undefined},
    {...completed, before: {...target.before, path: "before/other.py"}}, {...completed, mode: "unknown"},
    {...completed, before: {...target.before, source_bytes: 65537}}, {...completed, module_set: {}},
    {...completed, input_text: "changed input"}];
  for (const candidate of invalid) {
    current = candidate; await run("pollExperiment()");
    assert.equal(run("state.experiment.unknown"), true); assert.equal(nodes["experiment-run"].disabled, true);
    assert.equal(run("state.experiment.job.before.source_sha256"), target.before.source_sha256);
    assert.equal(nodes["experiment-before-result"].textContent, beforeText);
    assert.equal(nodes["experiment-submitted-input-text"].textContent, input, "invalid same-job input or identity cannot replace its record");
    current = completed; await run("pollExperiment()"); assert.equal(run("state.experiment.unknown"), false);
  }
  const badReports = [{...completed, comparison_result: "different", comparison_unchanged: false},
    {...completed, comparison_rule: "untrusted-rule"}, {...completed, status: "running"}, {...completed, observations: null},
    {...completed, observations: {before: observation(beforeText)}},
    {...completed, observations: {...completed.observations, after: {...observation(afterText), report_sha256: "bad"}}},
    {...completed, observations: {...completed.observations, after: {...observation(afterText), complete: false}}},
    {...completed, observations: {...completed.observations, after: {...observation(afterText), stdout: "x".repeat(196609)}}},
    {...completed, observations: {...completed.observations, after: observation("x".repeat(393217))}}];
  for (const candidate of badReports) {
    current = candidate; await run("pollExperiment()"); assert.equal(run("state.experiment.unknown"), true);
    assert.equal(nodes["experiment-after-result"].textContent, afterText, "malformed reports never replace the last valid pair");
    current = completed; await run("pollExperiment()");
  }

  // Display-only input boundary: never create markup, parse numbers or silently truncate an oversized record.
  const markedInput = ' {"args":[9007199254740993,"<img src=x onerror=alert(1)>"],"kwargs":{}}\n';
  const exceptionText = '{"exception":"ValueError","message":"owned exception","phase":"call"}';
  context.inputRecord = {...completed, id: "display-record", input_text: markedInput,
    observations: {...completed.observations, before: observation(exceptionText)}};
  const beforeInputDisplay = requests.length;
  run("renderPairedExperiment(inputRecord,true)");
  assert.equal(nodes["experiment-submitted-input-text"].textContent, markedInput);
  assert.equal(nodes["experiment-submitted-input-text"].querySelectorAll("img").length, 0);
  assert.equal(nodes["experiment-before-result"].textContent, exceptionText, "runtime completion never changes a guest-reported exception into success");
  assert.match(nodes["experiment-before-status"].textContent, /不代表函式正確/);
  assert.equal(nodes["experiment-submitted-input"].open, false, "a different job starts with its own collapsed input record");
  context.inputRecord.input_text = '{"args":["' + "x".repeat(16384 - '{"args":[""],"kwargs":{}}'.length) + '"],"kwargs":{}}';
  run("renderPairedExperiment(inputRecord,true)");
  assert.equal(nodes["experiment-submitted-input-text"].textContent.length, 16384);
  context.inputRecord.input_text = '{"args":["' + "字".repeat(5500) + '"],"kwargs":{}}';
  run("renderPairedExperiment(inputRecord,true)");
  assert.equal(nodes["experiment-submitted-input"].hidden, true); assert.equal(nodes["experiment-submitted-input-text"].textContent, "");
  assert.equal(requests.length, beforeInputDisplay);
  run("renderExperiment()");

  // Lost POST reply: recover only by GET and bind the returned pair to the submitted target/input.
  runReply = () => {current = pending("pair-two"); offline = true; throw Error("lost paired run reply");};
  await nodes["experiment-run"].listeners.click(); assert.equal(count("/api/experiment/run"), 2);
  await nodes["experiment-run"].listeners.click(); assert.equal(count("/api/experiment/run"), 2);
  const actual = current; offline = false;
  current = {...actual, before: {...target.before, source_sha256: "f".repeat(64)}};
  await nodes["experiment-recheck"].listeners.click(); assert.equal(run("state.experiment.unknown"), true);
  current = actual; await nodes["experiment-recheck"].listeners.click();
  assert.equal(run("state.experiment.job.id"), "pair-two"); assert.equal(run("state.experiment.unknown"), false);
  assert.equal(nodes["experiment-submitted-input-text"].textContent, input); assert.equal(nodes["experiment-submitted-input"].open, false);
  current = {...current, phase: "after", observations: {before: observation(beforeText)}};
  await run("pollExperiment()"); await nodes["experiment-cancel"].listeners.click();
  assert.equal(count("/api/experiment/cancel"), 1); assert.equal(count("/api/experiment/run"), 2);
  assert.equal(run("state.experiment.job.status"), "cancelled"); assert.match(nodes["experiment-pair-summary"].textContent, /尚無/);
  assert.equal(nodes["experiment-after-result"].hidden, true); unchanged();

  // Reload restores a completed pair with GET only; it does not add a trial to model history.
  current = completed; const beforeReload = requests.length;
  run("resetExperiment()"); await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(requests.slice(beforeReload).map(request => [request.route, request.method]), [["/api/experiment/current", "GET"]]);
  assert.equal(nodes["experiment-input"].value, input); assert.equal(nodes["experiment-after-result"].textContent, afterText);
  assert.equal(nodes["experiment-submitted-input-text"].textContent, input); assert.equal(nodes["experiment-submitted-input"].open, false);
  assert.equal(nodes["experiment-modules"].hidden, true); assert.equal(nodes["experiment-call-input"].hidden, true); unchanged();
  current = {...completed, status: "incomplete", cleanup_unknown: true, comparison_result: "unavailable", comparison_unchanged: false};
  await run("pollExperiment()");
  for (const id of ["experiment-run", "experiment-close", "ask", "refresh", "change-mode"]) assert.equal(nodes[id].disabled, true);
  await nodes["experiment-recheck"].listeners.click(); assert.equal(count("/api/experiment/run"), 2); assert.equal(count("/api/experiment/cancel"), 1);
  current = completed; await run("pollExperiment()");

  // Rejected and stale preparations never create a runnable pair.
  prepareReply = () => reply({...target, before: {...target.before, source_sha256: "bad"}});
  await run("openExperiment(state.source.outline.items[0],state.source)");
  assert.equal(run("state.experiment.target"), null); assert.equal(nodes["experiment-run"].disabled, true);
  assert.equal(nodes["experiment-submitted-input"].hidden, true); assert.equal(nodes["experiment-submitted-input-text"].textContent, "");
  const delayed = deferred(); prepareReply = () => delayed.promise;
  const stale = run("openExperiment(state.source.outline.items[0],state.source)");
  run("showProject({...pairedProject,experiments_enabled:true},true)");
  delayed.resolve(reply(target)); await stale;
  assert.equal(run("state.experiment.target"), null); assert.equal(count("/api/experiment/run"), 2);
  context.fetch = originalFetch;
}
async function checkPairedInputSearch(context, nodes) {
  const run = code => vm.runInContext(code, context), originalFetch = context.fetch, requests = [];
  const reply = value => ({ok: true, json: async () => value});
  const deferred = () => {let resolve; const promise = new Promise(done => {resolve = done;}); return {promise, resolve};};
  const files = [{id: "0", path: "before/search.py", lines: 2}, {id: "1", path: "after/search.py", lines: 2}];
  const comparison = {head: "d".repeat(40), original_snapshot_sha256: "e".repeat(64),
    catalogue: {schema_version: 1, kind: "source_change_catalogue", semantics_verified: false, files: []}};
  const target = {file: "1", path: files[1].path, version: "search-v1", entry: "echo", source_sha256: "a".repeat(64), source_bytes: 40,
    mode: "head_current", head: comparison.head, current_snapshot_sha256: comparison.original_snapshot_sha256,
    before: {file: "0", path: files[0].path, source_sha256: "b".repeat(64), source_bytes: 42}};
  const input = ' {"args":[9007199254740993],"kwargs":{"text":"<script>你好</script>"}}\n';
  const inputs = [input, input.replace("9007199254740993", "9007199254740992"), input.replace("9007199254740993", "9007199254740994")];
  const plan = {strategy: "source-v1", seed_input_text: input, inputs: inputs.map((input_text, index) => ({input_text, location: index ? "/args/0" : "seed",
    ...(index ? {hint: {side: index === 1 ? "before" : "after", line: 2}} : {})})),
    sources: {before: target.before.source_sha256, after: target.source_sha256, entry: target.entry},
    max_initializations: 6, max_seconds: 120, limited: true, sha256: "f".repeat(64)};
  const observation = text => ({result_text: text, stdout: "", stderr: "", process_status: "passed", host_status: "exited",
    source_unchanged: true, runtime_unchanged: true, complete: true, report_sha256: "c".repeat(64)});
  const pending = id => ({...target, id, status: "running", phase: "checking", input_text: input, elapsed_seconds: 0,
    observations: {}, comparison_result: "unavailable", comparison_rule: "canonical-json-v1", comparison_unchanged: false,
    search: {strategy: "source-v1", plan_sha256: plan.sha256, total: 3, completed: 0, case_index: 1, input_text: input, stop_reason: null}});
  let current = {id: null, status: "idle"}, prepareReply = () => reply({...target, search_plan: plan});
  let runReply = () => {current = pending("search-one"); return reply({id: current.id});};
  context.fetch = async (route, options) => {
    const body = options.body === undefined ? undefined : JSON.parse(options.body);
    requests.push({route, method: options.method, body});
    if (route.startsWith("/api/source?")) return reply({file: "1", path: target.path, version: target.version,
      lines: ["def echo(value):", "    return value"], outline: {status: "available", items: [{name: "echo", kind: "function", start_line: 1, definition_line: 1, end_line: 2, stub: false}]}});
    if (route === "/api/experiment/prepare") return body.search ? prepareReply(body) : reply(target);
    if (route === "/api/experiment/run") return runReply(body);
    if (route === "/api/experiment/current") return reply(current);
    if (route === "/api/experiment/cancel") {
      assert.deepEqual(body, {id: current.id});
      current = {...current, status: "cancelled", comparison_result: "unavailable", comparison_unchanged: false,
        search: {...current.search, stop_reason: "cancelled"}};
      return reply({status: "cancelled"});
    }
    throw Error(`unexpected input-search request: ${route}`);
  };
  const count = route => requests.filter(request => request.route === route).length;
  const edit = value => {nodes["experiment-input"].value = value; nodes["experiment-input"].listeners.input();};
  const toggle = value => {nodes["experiment-search-enabled"].checked = value; nodes["experiment-search-enabled"].listeners.change();};
  context.searchProject = {name: "search", version: target.version, files, excluded: [], comparison, experiments_enabled: true};
  context.searchPlan = plan; context.searchTarget = target;
  run("showProject(searchProject,true)"); await run("openFile(state.project.files[1],state.project.version)");
  await run("openExperiment(state.source.outline.items[0],state.source)");
  nodes.question.value = "PRESERVE SEARCH QUESTION";
  run('state.focus=[{file:"1",path:"after/search.py",start:1,end:2,role:"after"}];renderSelections()');
  const reading = () => run("JSON.stringify({source:state.source,focus:state.focus,job:state.job,history:state.history,selected:state.selectedHistory})");
  const preserved = reading(), answer = nodes.answer.textContent;
  const unchanged = () => {assert.equal(reading(), preserved); assert.equal(nodes.question.value, "PRESERVE SEARCH QUESTION"); assert.equal(nodes.answer.textContent, answer);};
  assert.equal(nodes["experiment-search-option"].hidden, false); assert.equal(nodes["experiment-search-enabled"].checked, false);
  assert.equal(nodes["experiment-search-plan"].hidden, true); assert.equal(nodes["experiment-trace-option"].hidden, true);
  edit(input); const initialPosts = requests.filter(request => request.method === "POST").length;
  toggle(true); assert.equal(requests.filter(request => request.method === "POST").length, initialPosts);
  assert.match(nodes["experiment-run"].textContent, /預覽.*不執行/);
  await nodes["experiment-run"].listeners.click();
  assert.equal(count("/api/experiment/run"), 0, "preview cannot execute either side");
  assert.deepEqual(requests.at(-1).body, {mode: "head_current", file: "1", version: target.version, entry: "echo", search: "source-v1", input_text: input});
  assert.equal(nodes["experiment-search-plan"].hidden, false);
  assert.equal(nodes["experiment-search-plan"].open, true, "new previews show every input before consent");
  assert.deepEqual(nodes["experiment-search-inputs"].querySelectorAll("pre").map(node => node.textContent), inputs);
  assert.equal(nodes["experiment-search-inputs"].querySelectorAll("script").length, 0);
  assert.deepEqual(nodes["experiment-search-inputs"].querySelectorAll("span").map(node => node.textContent),
    [" · 條件線索：HEAD L2", " · 條件線索：目前 L2"]);
  assert.match(nodes["experiment-run"].textContent, /3 組.*6 次完整模組.*120 秒/); unchanged();

  // A -> B -> A edits and toggle ABA both require a new explicit preview.
  edit(inputs[1]); edit(input); assert.equal(nodes["experiment-search-plan"].hidden, true);
  await nodes["experiment-run"].listeners.click(); toggle(false); toggle(true);
  assert.equal(nodes["experiment-search-plan"].hidden, true); assert.equal(count("/api/experiment/run"), 0);
  await nodes["experiment-run"].listeners.click(); const beforeConsent = count("/api/experiment/prepare");
  await nodes["experiment-run"].listeners.click();
  assert.equal(count("/api/experiment/prepare"), beforeConsent); assert.equal(count("/api/experiment/run"), 1);
  assert.deepEqual(requests.find(request => request.route === "/api/experiment/run").body,
    {file: "1", version: target.version, entry: "echo", source_sha256: target.source_sha256, input_text: input, allow_execution: true,
      mode: "head_current", head: target.head, before_sha256: target.before.source_sha256, search: "source-v1", search_plan_sha256: plan.sha256});
  assert.equal(nodes["experiment-search-enabled"].disabled, true);
  assert.equal(nodes["experiment-search-plan"].open, false, "submitted plans collapse to leave room for results");
  assert.equal(nodes["experiment-submitted-input-text"].textContent, input);
  current = {...current, phase: "after", elapsed_seconds: 2, observations: {before: observation('{"kind":"return","value":9007199254740992}')},
    search: {...current.search, case_index: 2, completed: 1, input_text: inputs[1]}};
  await run("pollExperiment()");
  assert.equal(nodes["experiment-search-current-input"].textContent, inputs[1]);
  assert.equal(nodes["experiment-after-result"].hidden, true); assert.match(nodes["experiment-pair-summary"].textContent, /第 2／3 組/);
  const found = {...current, status: "completed", comparison_result: "different", comparison_unchanged: true,
    observations: {...current.observations, after: observation('{"kind":"return","value":9007199254740993}')},
    search: {...current.search, completed: 2, stop_reason: "different"}};
  current = found; await run("pollExperiment()");
  assert.match(nodes["experiment-pair-summary"].textContent, /找到不同回報.*第 2 組/);
  assert.equal(nodes["experiment-search-plan"].open, false);
  nodes["experiment-search-plan"].open = true;
  await run("pollExperiment()");
  assert.equal(nodes["experiment-search-plan"].open, true, "polling preserves explicitly reopened input lists");
  assert.equal(nodes["experiment-search-current-input"].textContent, inputs[1]);
  edit('{"args":["NEW DRAFT"],"kwargs":{}}'); await run("pollExperiment()");
  assert.equal(nodes["experiment-input"].value, '{"args":["NEW DRAFT"],"kwargs":{}}');
  assert.equal(nodes["experiment-submitted-input-text"].textContent, input); unchanged();

  // Invalid cases cannot overwrite a known result or silently reinterpret the previewed plan.
  current = {...found, search: {...found.search, input_text: inputs[2]}}; await run("pollExperiment()");
  assert.equal(run("state.experiment.unknown"), true); assert.equal(nodes["experiment-search-current-input"].textContent, inputs[1]);
  current = {...found, search: {...found.search, plan_sha256: "1".repeat(64)}}; await run("pollExperiment()");
  assert.equal(run("state.experiment.unknown"), true);
  current = found; await run("pollExperiment()"); assert.equal(run("state.experiment.unknown"), false);
  context.searchJob = found;
  assert.equal(run("validExperimentSearch(searchJob)"), true);
  for (const change of [{total: 13}, {case_index: 0}, {completed: 0}, {stop_reason: "exhausted"}, {plan_sha256: plan.sha256 + "\n"}]) {
    context.invalidSearch = {...found, search: {...found.search, ...change}};
    assert.equal(run("validExperimentSearch(invalidSearch)"), false, JSON.stringify(change));
  }
  for (const change of [{max_seconds: 121}, {max_initializations: 5}, {sha256: plan.sha256 + "\n"}, {seed_input_text: inputs[1]},
    {inputs: [...plan.inputs, plan.inputs[0]]}, {inputs: [{input_text: input, location: "x".repeat(257)}]},
    {inputs: [{input_text: input, location: "\u202e"}]}, {sources: {...plan.sources, before: "0".repeat(64)}},
    {sources: {...plan.sources, after: "0".repeat(64)}}, {sources: {...plan.sources, entry: "other"}},
    {inputs: [plan.inputs[0], {...plan.inputs[1], hint: {side: "<script>", line: 2}}, plan.inputs[2]]},
    {inputs: [plan.inputs[0], {...plan.inputs[1], hint: {side: "before", line: 0}}, plan.inputs[2]]},
    {inputs: [plan.inputs[0], {...plan.inputs[1], hint: {side: "before", line: 65537}}, plan.inputs[2]]},
    {inputs: [{...plan.inputs[0], hint: {side: "before", line: 1}}, ...plan.inputs.slice(1)]}]) {
    context.invalidPlan = {...plan, ...change};
    assert.equal(run("validExperimentSearchPlan(invalidPlan,searchPlan.seed_input_text,searchTarget)"), false, JSON.stringify(change));
  }
  assert.equal(run("validExperimentSearchPlan(searchPlan,searchPlan.seed_input_text,searchTarget)"), true);
  context.legacyPlan = {...plan, strategy: "nearby-v1", inputs: plan.inputs.map(({hint, ...item}) => item)};
  delete context.legacyPlan.sources;
  assert.equal(run("validExperimentSearchPlan(legacyPlan,searchPlan.seed_input_text,searchTarget)"), true);
  current = {...found, comparison_result: "same", search: {...found.search, case_index: 3, completed: 3, input_text: inputs[2], stop_reason: "exhausted"}};
  await run("pollExperiment()"); assert.match(nodes["experiment-pair-summary"].textContent, /已測 3 組.*不代表兩版等價/);
  current = {...found, status: "incomplete", comparison_result: "unavailable", comparison_unchanged: false,
    search: {...found.search, completed: 1, stop_reason: "budget"}};
  await run("pollExperiment()"); assert.match(nodes["experiment-pair-summary"].textContent, /預算已到.*搜尋未完成/);
  current = found; const beforeReload = requests.length;
  run("resetExperiment()"); await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(requests.slice(beforeReload).map(request => [request.route, request.method]), [["/api/experiment/current", "GET"]]);
  assert.equal(nodes["experiment-search-enabled"].checked, true); assert.equal(nodes["experiment-search-plan"].hidden, true);
  assert.equal(nodes["experiment-search-current-input"].textContent, inputs[1]);
  assert.match(nodes["experiment-run"].textContent, /預覽/); unchanged();

  // Pending preview never publishes after a changed input, toggle, target, or refresh intent.
  for (const mutation of ["input", "toggle", "target", "refresh", "close"]) {
    run("state.experiment.target=searchTarget;state.experiment.visible=true;state.experiment.searchDraft=true;invalidateExperimentSearch(state.experiment);renderExperiment()");
    edit(input); const held = deferred(); prepareReply = () => held.promise;
    const preview = nodes["experiment-run"].listeners.click();
    assert.equal(run("state.experiment.searchPending"), true);
    if (mutation === "input") {edit(inputs[1]); edit(input);}
    if (mutation === "toggle") {toggle(false); toggle(true);}
    if (mutation === "target") run("state.experiment.target={...searchTarget}");
    if (mutation === "refresh") run("state.refreshing=true");
    if (mutation === "close") nodes["experiment-close"].listeners.click();
    held.resolve(reply({...target, search_plan: plan})); await preview;
    assert.equal(run("currentExperimentSearchPlan()"), null, mutation);
    run("state.refreshing=false;state.experiment.visible=true;experimentControls()");
  }
  assert.equal(count("/api/experiment/run"), 1); unchanged();
  prepareReply = () => reply({...target, search_plan: plan});
  run("state.experiment.target=searchTarget;renderExperiment()"); edit(input); toggle(true);
  await nodes["experiment-run"].listeners.click();
  runReply = () => ({ok: false, status: 400, json: async () => ({error: "stale plan"})});
  await nodes["experiment-run"].listeners.click();
  assert.equal(run("state.experiment.unknown"), false); assert.equal(run("state.experiment.submission"), null);
  assert.equal(nodes["experiment-search-plan"].hidden, true); assert.equal(count("/api/experiment/run"), 2);
  assert.match(nodes["experiment-search-note"].textContent, /stale plan.*服務拒絕搜尋.*未重送/,
    "restoring the previous completed job cannot hide the definite rejection");

  // A lost/5xx reply plus old terminal/idle is not proof the search was rejected.
  await nodes["experiment-run"].listeners.click();
  runReply = () => ({ok: false, status: 503, json: async () => ({error: "reply unavailable"})});
  await nodes["experiment-run"].listeners.click();
  assert.equal(run("state.experiment.unknown"), true); assert.equal(nodes["experiment-run"].disabled, true);
  await nodes["experiment-run"].listeners.click(); assert.equal(count("/api/experiment/run"), 3);
  current = {id: null, status: "idle"}; await run("pollExperiment()");
  assert.equal(run("state.experiment.unknown"), true); assert.equal(nodes["experiment-run"].disabled, true);
  // A lost POST has no accepted job to compare: the submitted strategy must
  // still match, even when the reported plan hash, source and counts all agree.
  current = pending("search-recovered"); current.search = {...current.search, strategy: "nearby-v1"};
  const beforeStrategyCheck = requests.length;
  await run("pollExperiment()");
  assert.equal(run("state.experiment.unknown"), true, "a different strategy cannot reconcile the pending source search");
  assert.equal(run("state.experiment.job"), null);
  assert.equal(run("state.experiment.submission.search.strategy"), "source-v1");
  assert.equal(nodes["experiment-run"].disabled, true);
  assert.deepEqual(requests.slice(beforeStrategyCheck).map(request => [request.route, request.method]),
    [["/api/experiment/current", "GET"]], "strategy mismatch only rechecks state; it never repeats execution");
  current = pending("search-recovered"); await run("pollExperiment()");
  assert.equal(run("state.experiment.unknown"), false); assert.equal(run("state.experiment.job.id"), "search-recovered");
  await nodes["experiment-cancel"].listeners.click();
  assert.equal(count("/api/experiment/cancel"), 1); assert.equal(count("/api/experiment/run"), 3);
  assert.match(nodes["experiment-pair-summary"].textContent, /搜尋已取消/); unchanged();
  assert(requests.every(request => request.method === "GET" || ["/api/experiment/prepare", "/api/experiment/run", "/api/experiment/cancel"].includes(request.route)));
  context.fetch = originalFetch;
}
async function checkExperimentModules(context, nodes) {
  const run = code => vm.runInContext(code, context), originalFetch = context.fetch, requests = [];
  const reply = value => ({ok: true, json: async () => value});
  const files = [{id: "0", path: "owned_pkg/__init__.py", lines: 0}, {id: "1", path: "owned_pkg/helper.py", lines: 2},
    {id: "2", path: "owned_pkg/rows.py", lines: 2}, {id: "3", path: "other.py", lines: 2}, {id: "4", path: "notes.md", lines: 1}];
  const target = body => ({file: body.file, path: files[Number(body.file)].path, version: body.version,
    entry: body.entry, source_sha256: "a".repeat(64), source_bytes: 40});
  const metadata = {import_root: ".", entry_path: "owned_pkg/rows.py", entry: "encode_row", entry_module: "owned_pkg.rows",
    files: files.slice(0, 3).map(file => ({path: file.path, sha256: (file.id === "2" ? "a" : "b").repeat(64), size_bytes: file.id === "0" ? 0 : 40})),
    sha256: "c".repeat(64)};
  const raw = ' {"args":[9007199254740993],"kwargs":{"suffix":"!"}}\n';
  const resultText = '{"return":9007199254740993}';
  let current = {id: null, status: "idle"};
  let prepareReply = body => reply({...target(body), ...(body.modules ? {module_set: metadata} : {})});
  context.fetch = async (route, options) => {
    const body = options.body === undefined ? undefined : JSON.parse(options.body);
    requests.push({route, method: options.method, body});
    if (route.startsWith("/api/source?")) {
      const file = files[Number(new URLSearchParams(route.split("?")[1]).get("file"))], name = file.id === "2" ? "encode_row" : "other";
      return reply({file: file.id, path: file.path, version: "modules-v1", lines: [`def ${name}(value):`, "    return value"],
        outline: {status: "available", items: [{name, kind: "function", start_line: 1, definition_line: 1, end_line: 2, stub: false}]}});
    }
    if (route === "/api/experiment/prepare") return prepareReply(body);
    if (route === "/api/experiment/current") return reply(current);
    if (route === "/api/experiment/run") {
      current = {...target(body), module_set: metadata, id: "module-run", status: "completed", input_text: body.input_text,
        elapsed_seconds: 1.5, result_text: resultText};
      return reply({id: current.id});
    }
    throw Error(`unexpected module-selection request: ${route}`);
  };
  const boxes = () => nodes["experiment-module-options"].querySelectorAll("input");
  const select = id => {const box = boxes().find(box => box.value === id); assert(box && !box.disabled); box.checked = true; box.listeners.change();};
  const count = route => requests.filter(request => request.route === route).length;
  context.moduleProject = {name: "owned-modules", version: "modules-v1", files, excluded: [], experiments_enabled: true};
  run("showProject(moduleProject,true)"); await run("openFile(state.project.files[2],state.project.version)");
  await run("openExperiment(state.source.outline.items[0],state.source)");
  const base = target({file: "2", version: "modules-v1", entry: "encode_row"});
  current = {...base, id: "prior-module-result", status: "completed", input_text: raw, elapsed_seconds: 1, result_text: '"prior result"'};
  await run("pollExperiment()");
  context.moduleReading = {...answered("module-reading", "KEEP_MODULE_READING"), version: "modules-v1", files};
  nodes.question.value = "KEEP_MODULE_QUESTION";
  run('renderJob(moduleReading); state.focus=[{file:"2",path:"owned_pkg/rows.py",start:1,end:2}]; renderSelections()');
  const reading = () => run("JSON.stringify({job:state.job,focus:state.focus,history:state.history,selected:state.selectedHistory})");
  const beforeReading = reading(), beforeAnswer = nodes.answer.textContent;
  const unchanged = () => {assert.equal(reading(), beforeReading); assert.equal(nodes.answer.textContent, beforeAnswer); assert.equal(nodes.question.value, "KEEP_MODULE_QUESTION");};

  // The entry is implicit; changing the extra-file draft or filtering never sends HTTP.
  assert.deepEqual(boxes().map(box => box.value), ["0", "1", "3"]);
  const beforeSelecting = requests.length;
  select("0"); select("1");
  nodes["experiment-module-filter"].value = "helper"; nodes["experiment-module-filter"].listeners.input();
  const labels = nodes["experiment-module-options"].querySelectorAll("label");
  assert.equal(labels.find(row => row.dataset.path.endsWith("__init__.py")).hidden, true);
  assert.equal(labels.find(row => row.dataset.path.endsWith("helper.py")).hidden, false);
  assert.equal(requests.length, beforeSelecting); assert.equal(nodes["experiment-run"].disabled, true);
  assert.equal(nodes["experiment-input-state"].hidden, false);
  assert.match(nodes["experiment-module-note"].textContent, /先前/);
  assert.equal(nodes["experiment-result-text"].textContent, '"prior result"'); unchanged();

  // Explicit preparation includes the hidden checked parent and preserves the raw input.
  nodes["experiment-modules"].open = true;
  await nodes["experiment-module-prepare"].listeners.click();
  assert.deepEqual(requests.at(-1), {route: "/api/experiment/prepare", method: "POST",
    body: {file: "2", version: "modules-v1", entry: "encode_row", modules: ["0", "1", "2"]}});
  assert.equal(nodes["experiment-input"].value, raw); assert.equal(nodes["experiment-modules"].open, false);
  assert.equal(nodes["experiment-run"].disabled, false); assert.equal(count("/api/experiment/run"), 0);
  assert(nodes["experiment-modules-bound"].textContent.includes("owned_pkg/__init__.py"));
  assert(nodes["experiment-identity"].textContent.includes(metadata.sha256)); unchanged();
  await nodes["experiment-run"].listeners.click();
  assert.equal(count("/api/experiment/run"), 1);
  assert.deepEqual(requests.find(request => request.route === "/api/experiment/run").body,
    {file: "2", version: "modules-v1", entry: "encode_row", source_sha256: base.source_sha256, input_text: raw,
      allow_execution: true, modules: ["0", "1", "2"], module_set_sha256: metadata.sha256});
  assert.equal(nodes["experiment-result-text"].textContent, resultText); unchanged();

  // Reload hydration is GET-only and restores the complete checked set and source identity.
  const completed = current, beforeReload = requests.length;
  run("resetExperiment()"); await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(requests.slice(beforeReload).map(request => [request.route, request.method]), [["/api/experiment/current", "GET"]]);
  assert.deepEqual(boxes().filter(box => box.checked).map(box => box.value), ["0", "1"]);
  assert.equal(nodes["experiment-input"].value, raw); assert.equal(nodes["experiment-result-text"].textContent, resultText);
  assert.equal(run("state.experiment.target.module_set.sha256"), metadata.sha256); unchanged();
  current = {...completed, module_set: {...metadata, sha256: "d".repeat(64)}};
  await run("pollExperiment()");
  assert.equal(run("state.experiment.unknown"), true); assert.equal(nodes["experiment-run"].disabled, true);
  assert.equal(run("state.experiment.job.module_set.sha256"), metadata.sha256, "same-job metadata mutation cannot replace the prior identity");
  current = completed; await run("pollExperiment()"); assert.equal(run("state.experiment.unknown"), false);

  // Invalid module provenance never becomes an executable/recovered target.
  const invalid = [null, {...metadata, import_root: ".."}, {...metadata, sha256: "not-a-sha"},
    {...metadata, files: [...metadata.files, metadata.files[0]]},
    {...metadata, files: metadata.files.map((file, index) => index ? file : {...file, path: "missing.py"})},
    {...metadata, entry_path: "other.py"}, {...metadata, entry_module: "bad/module"}];
  for (const [index, module_set] of invalid.entries()) {
    current = {...completed, id: `invalid-modules-${index}`, module_set};
    run("resetExperiment(true)"); await run("pollExperiment()");
    assert.equal(run("state.experiment.unknown"), true); assert.equal(run("state.experiment.target"), null);
    assert.equal(nodes["experiment-run"].disabled, true); assert.equal(nodes["experiment-result-text"].textContent, "");
  }
  assert.equal(count("/api/experiment/run"), 1); unchanged();

  // Switching source invalidates a pending module-set prepare, even in the same version.
  run("resetExperiment(true)"); await run("openFile(state.project.files[2],state.project.version)");
  await run("openExperiment(state.source.outline.items[0],state.source)"); select("0"); select("1");
  let resolvePrepare; const delayed = new Promise(resolve => {resolvePrepare = resolve;});
  prepareReply = () => delayed;
  const stale = nodes["experiment-module-prepare"].listeners.click();
  await run("openFile(state.project.files[3],state.project.version)");
  prepareReply = body => reply(target(body));
  await run("openExperiment(state.source.outline.items[0],state.source)");
  const newIdentity = nodes["experiment-identity"].textContent;
  resolvePrepare(reply({...base, module_set: metadata})); await stale;
  assert.equal(run("state.experiment.target.file"), "3"); assert.equal(run("state.experiment.target.module_set"), undefined);
  assert.equal(nodes["experiment-identity"].textContent, newIdentity); assert.equal(nodes["experiment-modules-bound"].hidden, true);
  assert.equal(count("/api/experiment/run"), 1); unchanged();
  context.fetch = originalFetch;
}
async function checkExperimentCallInputs(context, nodes) {
  const run = code => vm.runInContext(code, context), originalFetch = context.fetch, requests = [];
  const reply = value => ({ok: true, json: async () => value});
  const deferred = () => {let resolve; const promise = new Promise(done => {resolve = done;}); return {promise, resolve};};
  const files = [{id: "0", path: "owned.py", lines: 4}, {id: "1", path: "callers.py", lines: 100}, {id: "2", path: "notes.md", lines: 1}];
  const definition = (name, start) => ({name, kind: "function", start_line: start, definition_line: start, end_line: start + 1, stub: false});
  const sourceLines = [["def echo(value, text, nothing, markup, flag=False):", "    return value", "def other(value):", "    return value"],
    ["# Literal caller, not a proof of binding", "echo(", String.raw`    9007199254740993, "你好\n引號\"", None, "<img src=x onerror=alert(1)>",`, "    flag=False,", ")", ...Array(95).fill("# context")], ["Notes"]];
  const target = body => ({file: body.file, path: files[Number(body.file)].path, version: body.version,
    entry: body.entry, source_sha256: "a".repeat(64), source_bytes: 40});
  const oldInput = '{"args":[1],"kwargs":{}}', oldResult = '{"return":"OLD <img src=x onerror=alert(1)>"}';
  const raw = String.raw` {"args":[9007199254740993,"你好\n引號\"",null,"<img src=x onerror=alert(1)>"],"kwargs":{"flag":false}}` + "\n";
  const response = () => ({file: "1", path: "callers.py", version: "call-input-v1", source_sha256: "b".repeat(64),
    entry: "echo", start: 2, end: 6, call_start: 2, call_end: 5, input_text: raw});
  let current, inputReply = () => reply(response());
  context.fetch = async (route, options) => {
    const body = options.body === undefined ? undefined : JSON.parse(options.body);
    requests.push({route, method: options.method, body});
    if (route.startsWith("/api/source?")) {
      const index = Number(new URLSearchParams(route.split("?")[1]).get("file")), file = files[index];
      return reply({file: file.id, path: file.path, version: "call-input-v1", lines: sourceLines[index],
        outline: {status: "available", items: index ? [] : [definition("echo", 1), definition("other", 3)]}});
    }
    if (route === "/api/experiment/prepare") return reply(target(body));
    if (route === "/api/experiment/current") return reply(current);
    if (route === "/api/experiment/input") return inputReply(body);
    throw Error(`unexpected source-call request (execution and models forbidden): ${route}`);
  };
  const setup = async () => {
    context.callProject = {name: "literal-call", version: "call-input-v1", files, excluded: [], experiments_enabled: true};
    run("state.pending=false; state.refreshing=false; showProject(callProject,true)");
    await run("openFile(state.project.files[0],state.project.version)");
    await run("openExperiment(state.source.outline.items[0],state.source)");
    current = {...target({file: "0", version: "call-input-v1", entry: "echo"}), id: "prior-call-result", status: "completed",
      input_text: oldInput, elapsed_seconds: 1, result_text: oldResult};
    await run("pollExperiment()"); await run("openFile(state.project.files[1],state.project.version)");
    context.callReading = {...answered("call-reading", "KEEP_CALL_READING"), version: "call-input-v1", files};
    nodes.question.value = "KEEP_CALL_QUESTION";
    run('renderJob(callReading); state.focus=[{file:"0",path:"owned.py",start:1,end:2}]; state.anchor=6; state.end=2; renderSelections(); experimentControls()');
    requests.length = 0; inputReply = () => reply(response());
    assert.equal(nodes["experiment-from-call"].disabled, false);
  };
  const reading = () => run("JSON.stringify({job:state.job,focus:state.focus,history:state.history,selected:state.selectedHistory})");
  const capture = () => ({reading: reading(), answer: nodes.answer.textContent, question: nodes.question.value,
    job: run("state.experiment.job"), jobText: run("JSON.stringify(state.experiment.job)"), target: run("state.experiment.target"),
    modules: run("JSON.stringify(state.experiment.moduleDraft)"), result: nodes["experiment-result-text"].textContent});
  const unchanged = before => {
    assert.equal(reading(), before.reading); assert.equal(nodes.answer.textContent, before.answer); assert.equal(nodes.question.value, before.question);
    assert.equal(run("state.experiment.job"), before.job); assert.equal(run("JSON.stringify(state.experiment.job)"), before.jobText);
    assert.equal(run("state.experiment.target"), before.target); assert.equal(run("JSON.stringify(state.experiment.moduleDraft)"), before.modules);
    assert.equal(nodes["experiment-result-text"].textContent, before.result);
  };

  // Different caller/target files and hashes remain distinct; JSON text never crosses JS number parsing.
  await setup(); const before = capture(), source = run("state.source"), delayed = deferred();
  inputReply = () => delayed.promise;
  assert.match(nodes["experiment-call-selection"].textContent, /callers\.py.*L2–L6.*owned\.py.*echo/);
  assert.match(nodes["experiment-call-selection"].textContent, /不驗證.*綁定/);
  const pending = nodes["experiment-from-call"].listeners.click();
  assert.equal(run("state.experiment.inputPending"), true); assert.equal(nodes["experiment-run"].disabled, true);
  assert.equal(nodes["experiment-input"].readOnly, false, "pending static inspection must not prevent typing a replacement draft");
  await run("importExperimentCallInputs()");
  assert.deepEqual(requests, [{route: "/api/experiment/input", method: "POST",
    body: {file: "1", version: "call-input-v1", start: 2, end: 6, entry: "echo"}}]);
  delayed.resolve(reply(response())); await pending;
  assert.equal(nodes["experiment-input"].value, raw); assert.equal(run("state.experiment.input"), raw);
  assert.equal(run("state.experiment.inputPending"), false); assert.equal(nodes["experiment-input-state"].hidden, false);
  assert.equal(run("state.source"), source); assert.equal(run("state.anchor"), 6); assert.equal(run("state.end"), 2);
  assert.match(nodes["experiment-call-note"].textContent, /callers\.py.*L2–L5.*echo/);
  assert.match(nodes["experiment-call-note"].textContent, /不代表實際/);
  assert(nodes["experiment-identity"].textContent.includes("b".repeat(64)));
  assert.equal(nodes["experiment-call-note"].querySelectorAll("img").length, 0); unchanged(before);
  assert.equal(requests.length, 1, "carryover must not prepare modules, run, poll or submit an AI job");
  nodes["experiment-input"].value = oldInput; nodes["experiment-input"].listeners.input();
  assert.equal(run("state.experiment.callInputSource"), null); assert.equal(nodes["experiment-call-note"].textContent, "");
  assert(!nodes["experiment-identity"].textContent.includes("b".repeat(64))); unchanged(before); assert.equal(requests.length, 1);

  // Both rendered button and its handler refuse ineligible or concurrently occupied flows.
  const blocked = ["state.experiment.visible=false", "state.experiment.target=null", "state.anchor=null",
    "state.anchor=1; state.end=81", "state.source.version='old'", "state.source.path='notes.md'",
    "state.project.experiments_enabled=false", "state.project.comparison={}", "state.experiment.preparing=true",
    "state.experiment.inputPending=true", "state.experiment.moduleDraft=['1']", "state.experiment.job={status:'running'}",
    "state.experiment.job={status:'incomplete',cleanup_unknown:true}", "state.job={status:'running'}",
    "state.refreshing=true", "state.pairPending=true"];
  for (const condition of blocked) {
    await setup(); run(condition + "; experimentControls()");
    assert.equal(nodes["experiment-from-call"].disabled, true, condition);
    await run("importExperimentCallInputs()"); assert.equal(requests.length, 0, condition);
  }
  await setup(); run("state.anchor=1; state.end=80; experimentControls()");
  assert.equal(nodes["experiment-from-call"].disabled, false, "the declared eighty-line boundary remains usable");

  // Exact response identity and contained call coordinates are required; valid caller SHA need not equal target SHA.
  const malformed = [null, {...response(), extra: true}, Object.fromEntries(Object.entries(response()).filter(([key]) => key !== "source_sha256")),
    ...["file", "path", "version", "entry"].map(key => ({...response(), [key]: "wrong"})),
    {...response(), source_sha256: "b".repeat(63)}, {...response(), start: 1}, {...response(), end: 7},
    {...response(), call_start: 1}, {...response(), call_end: 7}, {...response(), call_start: 5, call_end: 4},
    {...response(), call_start: 2.5}, {...response(), call_end: "5"}, {...response(), input_text: {}},
    {...response(), input_text: " \n"}, {...response(), input_text: '"' + "中".repeat(5461) + '"'}];
  for (const [index, value] of malformed.entries()) {
    await setup(); const retained = capture(); inputReply = () => reply(value);
    await nodes["experiment-from-call"].listeners.click();
    assert.equal(nodes["experiment-input"].value, oldInput, `malformed response ${index}`);
    assert.equal(run("state.experiment.inputPending"), false); assert(!run("state.experiment.callInputSource"));
    assert.match(nodes["experiment-call-note"].textContent, /未改寫|保留/); unchanged(retained); assert.equal(requests.length, 1);
  }

  // A late response must not resurrect an earlier source/selection, target, input or panel.
  const staleActions = [
    async () => {await run("openFile(state.project.files[0],state.project.version)");},
    async () => {run("state.anchor=3; experimentControls(); state.anchor=6; experimentControls()");},
    async () => {nodes["experiment-input"].value = '{"args":[2],"kwargs":{}}'; nodes["experiment-input"].listeners.input();},
    async () => {nodes["experiment-input"].value = "changed"; nodes["experiment-input"].listeners.input(); nodes["experiment-input"].value = oldInput; nodes["experiment-input"].listeners.input();},
    async () => {nodes["experiment-close"].listeners.click();},
    async () => {await run("openFile(state.project.files[0],state.project.version)"); await run("openExperiment(state.source.outline.items[1],state.source)");},
    async () => {run("showProject({...callProject},true)");},
    async () => {current = {...current, id: "newly-received-job", input_text: '{"args":[3],"kwargs":{}}'}; await run("pollExperiment()");},
    async () => {run("state.job={id:'new-model',status:'running'}; controls()");}
  ];
  for (const [index, change] of staleActions.entries()) {
    await setup(); const late = deferred(); inputReply = () => late.promise;
    const work = nodes["experiment-from-call"].listeners.click(); await change();
    const retained = capture(), retainedInput = nodes["experiment-input"].value, visible = run("state.experiment.visible"), currentSource = run("state.source");
    const requestCount = requests.length; late.resolve(reply(response())); await work;
    assert.equal(nodes["experiment-input"].value, retainedInput, `stale response ${index}`);
    assert.equal(run("state.experiment.visible"), visible); assert.equal(run("state.source"), currentSource);
    assert.equal(run("Boolean(state.experiment.inputPending)"), false); assert(!run("state.experiment.callInputSource"));
    unchanged(retained); assert.equal(requests.length, requestCount, "discarding a stale reply has no follow-up request");
  }

  for (const fail of [() => ({ok: false, json: async () => ({error: "No literal input <img src=x>"})}),
    () => {throw Error("offline <img src=x>");}]) {
    await setup(); const retained = capture(); inputReply = fail;
    await nodes["experiment-from-call"].listeners.click();
    assert.equal(nodes["experiment-input"].value, oldInput); assert.equal(run("state.experiment.inputPending"), false);
    assert.equal(nodes["experiment-call-note"].querySelectorAll("img").length, 0); unchanged(retained);
    assert.equal(requests.length, 1, "static-input failure must not retry, poll or execute");
  }
  context.fetch = originalFetch;
}
async function checkSourceContinuation(context, nodes) {
  const run = code => vm.runInContext(code, context), originalFetch = context.fetch, requests = [];
  const reply = value => ({ok: true, json: async () => value}), settle = () => new Promise(resolve => setImmediate(resolve));
  const files = [{id: "0", path: "entry.py", lines: 300}, {id: "1", path: "helper.py", lines: 300}, {id: "2", path: "other.py", lines: 4}];
  const focus = [{path: "entry.py", start_line: 1, end_line: 2}, {path: "entry.py", start_line: 4, end_line: 5},
    {path: "helper.py", start_line: 1, end_line: 2}, {path: "helper.py", start_line: 4, end_line: 5}, {path: "helper.py", start_line: 7, end_line: 8}];
  const scope = {focus: focus.map(span => `${span.path}:${span.start_line}-${span.end_line}`), origin: "project_candidates", supplements: ["helper.py:7-8"]};
  const rawQuestion = "  請說明 entry 的錯誤分支\n\t保留這份草稿 <img src=x>。  ";
  const base = (id, question, reading_scope) => ({...answered(id, `${id}_PROSE_MUST_NOT_ENTER_NEXT_QUESTION`), question,
    version: "continuation-v1", files, gpu: "released", elapsed_seconds: 1, reading_scope,
    result: {kind: "forge8.explain", ok: true, status: "answered", outcome: {ok: true, status: "answered",
      source_unchanged: true, snapshot_unchanged: true, acceptance: {ok: true},
      answer: {claims: [{text: `${id}_PROSE_MUST_NOT_ENTER_NEXT_QUESTION`, citations: []}]}}}});
  const makeParent = () => {
    const parent = base("parent-a", "A_ORIGINAL_QUESTION <img src=x>", scope); parent.kind = "project";
    parent.result.project_reading = {answer_attempted: true, focus, context: {added: [{path: "helper.py", start_line: 7, end_line: 8}], skipped: {}},
      discovery: {ok: true, status: "located", snapshot_sha256: "continuation-v1", source_unchanged: true,
        snapshot_unchanged: true, ingress_unchanged: true, acceptance: {ok: true}, candidates: [
          {path: "entry.py", name: "entry", kind: "function", start_line: 1, end_line: 2},
          {path: "helper.py", name: "helper", kind: "function", start_line: 1, end_line: 2}]}};
    parent.result.outcome.coverage = {observed: {ranges: files.slice(0, 2).map(file => ({path: file.path,
      ranges: focus.filter(span => span.path === file.path).map(({start_line, end_line}) => ({start_line, end_line}))}))}};
    return parent;
  };
  let current, archived, parent, second, evicted = 0, continueReply;
  context.fetch = async (route, options) => {
    const body = options.body === undefined ? undefined : JSON.parse(options.body);
    requests.push({route, method: options.method, body});
    if (route === "/api/jobs/current") return reply(current);
    if (route === "/api/history") return reply({version: "continuation-v1", entries: archived, evicted});
    if (route === "/api/continue-question") return continueReply(body);
    if (route === "/api/jobs") {current = {id: "manual-question", status: "running", version: "continuation-v1", question: body.question}; return reply({id: current.id});}
    if (route === `/api/jobs/${current.id}/cancel`) {current = {...current, status: "cancelled", gpu: "released", elapsed_seconds: 1}; return reply({status: "cancelled"});}
    if (route === "/api/refresh") {current = {id: null, status: "idle"}; archived = []; return reply({...context.continuationProject});}
    if (route.startsWith("/api/source?")) {
      const file = files[Number(new URLSearchParams(route.split("?")[1]).get("file"))];
      return reply({path: file.path, version: "continuation-v1", lines: Array(file.lines).fill("# owned inert source"), outline: {status: "available", items: []}});
    }
    throw Error(`unexpected source-continuation request: ${route}`);
  };
  const choose = id => {nodes["history-select"].value = id; nodes["history-select"].listeners.change();};
  const posts = () => requests.filter(request => request.method === "POST");
  const manual = () => run("JSON.stringify(state.focus)");
  const setup = async () => {
    context.continuationProject = {name: "continuation", version: "continuation-v1", files, excluded: []};
    run("state.pending=false; state.refreshing=false; showProject(continuationProject,true)");
    parent = makeParent(); second = base("parent-b", "B_ORIGINAL_QUESTION", {focus: ["other.py:1-2"], origin: "user_focus", supplements: []});
    current = second; archived = [parent, second]; evicted = 0; context.continuationCurrent = current;
    run("renderJob(continuationCurrent)"); await run("loadHistory()");
    await run("openFile(state.project.files[2],state.project.version,1,2)");
    nodes.question.value = rawQuestion;
    run('state.focus=[{file:"2",path:"other.py",start:1,end:2}]; renderSelections()'); choose(parent.id);
    continueReply = body => {current = {id: "continued-one", kind: "continue", status: "running", version: body.version, question: body.question}; return reply({id: current.id});};
    requests.length = 0; assert.equal(nodes["continue-source"].disabled, false);
  };
  await setup(); const beforeFocus = manual(), beforeAnswer = nodes.answer.textContent, beforeSource = run("state.source"), beforeHistory = run("JSON.stringify(state.history)");
  nodes["question-editor"].open = false; await nodes["continue-source"].listeners.click();
  assert.equal(requests.length, 0); assert.equal(run("state.continuation.parent_id"), parent.id);
  assert.equal(nodes["question-editor"].open, true); assert.equal(nodes["continuation-panel"].hidden, false);
  assert(nodes["continuation-parent"].textContent.includes(parent.question)); assert.equal(nodes["continuation-parent"].querySelectorAll("img").length, 0);
  const expectedRanges = focus.map(span => `${span.path} · L${span.start_line}–L${span.end_line}`);
  assert.deepEqual(nodes["continuation-ranges"].querySelectorAll("button").map(button => button.textContent), expectedRanges);
  assert.deepEqual(JSON.parse(run("JSON.stringify(state.continuation.scope)")), scope);
  assert.notEqual(run("state.continuation.scope.focus"), scope.focus); assert.notEqual(run("state.continuation.scope.supplements"), scope.supplements);
  assert.equal(manual(), beforeFocus); assert.equal(nodes.question.value, rawQuestion); assert.equal(nodes.answer.textContent, beforeAnswer);
  assert.equal(run("state.source"), beforeSource); assert.equal(run("JSON.stringify(state.history)"), beforeHistory);
  assert(nodes["ask-project"].disabled && nodes.locate.disabled, "continuation must not quietly switch into rediscovery");
  assert.equal(nodes.selections.dataset.continuation, "true"); assert.match(nodes["selection-count"].textContent, /沿用 5 段.*手動 1 段未送出/);
  choose(second.id); assert.equal(run("state.continuation.parent_id"), parent.id);
  assert(nodes["continuation-parent"].textContent.includes(parent.question)); assert.equal(requests.length, 0);
  await nodes["continuation-ranges"].querySelectorAll("button").at(-1).listeners.click();
  assert.deepEqual(requests, [{route: "/api/source?file=1&version=continuation-v1", method: "GET", body: undefined}]);
  assert.equal(run("state.anchor"), 7); assert.equal(run("state.end"), 8); assert.equal(manual(), beforeFocus); assert.equal(nodes.question.value, rawQuestion);

  let release; const gate = new Promise(resolve => {release = resolve;});
  continueReply = body => {current = {id: "continued-one", kind: "continue", status: "running", version: body.version, question: body.question}; return gate;};
  const submission = nodes["question-form"].listeners.submit({preventDefault() {}});
  await run("submitQuestion()");
  assert.deepEqual(posts(), [{route: "/api/continue-question", method: "POST", body: {parent: parent.id, question: rawQuestion, version: "continuation-v1"}}]);
  release(reply({id: current.id})); await submission; await settle();
  assert.equal(run("state.job.kind"), "continue"); assert.equal(nodes.cancel.hidden, false);
  assert.equal(manual(), beforeFocus); assert.equal(nodes.question.value, rawQuestion);
  current = {...base("continued-one", rawQuestion, scope), kind: "continue"}; archived = [parent, second, current];
  await run("poll()"); choose("");
  assert.deepEqual(nodes.answer.querySelectorAll("button").filter(button => button.dataset.retained).map(button => button.textContent), expectedRanges,
    "a continued answer retains all five exact source links, not merely its claim citations or three manual ranges");
  await nodes["continue-source"].listeners.click();
  assert.equal(run("state.continuation.parent_id"), "continued-one", "an explicit second binding uses the child's flat source scope, not a recursive conversation");
  assert.deepEqual(JSON.parse(run("JSON.stringify(state.continuation.scope)")), scope); assert.equal(posts().length, 1);
  continueReply = body => {current = {id: "continued-two", kind: "continue", status: "running", version: body.version, question: body.question}; return reply({id: current.id});};
  await nodes["question-form"].listeners.submit({preventDefault() {}}); await settle(); await nodes.cancel.listeners.click(); await settle();
  assert.deepEqual(posts().map(item => item.route), ["/api/continue-question", "/api/continue-question", "/api/jobs/continued-two/cancel"]);
  assert.equal(posts()[1].body.parent, "continued-one"); assert.equal(manual(), beforeFocus); assert.equal(nodes.question.value, rawQuestion);

  await setup(); run("state.focus=[]; renderSelections()"); assert.equal(nodes.ask.disabled, true);
  await nodes["continue-source"].listeners.click(); assert.equal(nodes.ask.disabled, false);
  assert.equal(manual(), "[]"); assert.equal(requests.length, 0, "an automatic answer needs no reconstruction of manual selections before continuing");
  context.sixContinuation = {...base("six-source", "SIX_RANGES", {...scope, focus: [...scope.focus, "entry.py:7-8"]}), kind: "continue"};
  run("clearContinuation(); renderJob(sixContinuation)"); choose(""); await nodes["continue-source"].listeners.click();
  assert.equal(nodes["continuation-ranges"].querySelectorAll("button").length, 6); assert.equal(requests.length, 0);

  // The explicit off-ramp restores the original manual route without changing its editor content.
  await setup(); await nodes["continue-source"].listeners.click(); await nodes["continuation-clear"].listeners.click();
  assert.equal(run("state.continuation"), null); assert.equal(nodes["continuation-panel"].hidden, true);
  assert.equal(manual(), beforeFocus); assert.equal(nodes.question.value, rawQuestion); assert.equal(requests.length, 0);
  await nodes["question-form"].listeners.submit({preventDefault() {}}); await settle();
  assert.deepEqual(posts(), [{route: "/api/jobs", method: "POST", body: {question: rawQuestion, version: "continuation-v1", focus: [{file: "2", start: 1, end: 2}]}}]);

  await setup(); await nodes["continue-source"].listeners.click(); archived = [second]; evicted = 1;
  await run("loadHistory()"); assert.equal(run("state.continuation"), null);
  assert(nodes["continuation-note"].textContent); assert.equal(manual(), beforeFocus); assert.equal(nodes.question.value, rawQuestion); assert.equal(posts().length, 0);
  await setup(); await nodes["continue-source"].listeners.click(); await nodes.refresh.listeners.click();
  assert.equal(run("state.project.version"), "continuation-v1"); assert.equal(run("state.continuation"), null);
  assert.equal(nodes["continuation-panel"].hidden, true); assert.equal(nodes.question.value, rawQuestion);
  assert.deepEqual(posts().map(item => item.route), ["/api/refresh"], "even byte-identical refresh must end the explicit parent binding");
  await setup(); await nodes["continue-source"].listeners.click(); run("state.continuation.version='old'");
  await nodes["question-form"].listeners.submit({preventDefault() {}});
  assert.equal(run("state.continuation"), null); assert.equal(posts().length, 0); assert.equal(manual(), beforeFocus); assert.equal(nodes.question.value, rawQuestion);

  // Only exact, fully bounded scopes can enable the binding; this cannot rescue cancelled/ungated output.
  const invalidScopes = [null, {...scope, extra: true}, {focus: scope.focus, origin: scope.origin}, {...scope, origin: "guessed"},
    {...scope, focus: []}, {...scope, focus: [...scope.focus, ...scope.focus.slice(0, 2)]}, {...scope, origin: "user_focus", supplements: []},
    {...scope, focus: ["entry.py:1-2", "entry.py:1-2"]}, {...scope, focus: ["entry.py:0-2"]}, {...scope, focus: ["entry.py:1-81"]},
    {...scope, focus: ["entry.py:1-80", "entry.py:81-160", "entry.py:161-240", "helper.py:1-80"], supplements: []},
    {...scope, focus: ["missing.py:1-2"]}, {...scope, focus: ["../entry.py:1-2"]}, {...scope, focus: ["entry.py:01-2"]},
    {...scope, supplements: ["helper.py:9-10"]}, {...scope, supplements: ["helper.py:7-8", "helper.py:7-8"]},
    {...scope, focus: ["entry.py:1-60"], supplements: ["entry.py:1-41"]},
    {origin: "user_focus", focus: ["entry.py:1-2"], supplements: ["entry.py:1-2"]}];
  for (const reading_scope of invalidScopes) {
    await setup(); context.invalidContinuation = {...second, reading_scope}; run("renderJob(invalidContinuation)"); choose("");
    assert.equal(run("validReadingScope(invalidContinuation)"), null); assert.equal(nodes["continue-source"].disabled, true);
    await run("selectContinuation()"); assert.equal(run("state.continuation"), null); assert.equal(requests.length, 0);
  }
  for (const override of [{version: "old"}, {gpu: "unknown"}, {error: "ungated"}, {status: "running"}, {status: "cancelled"},
    {status: "located", kind: "locate"}, {comparison: {}}, {status: "incomplete"}, {result: {...second.result, ok: false}}]) {
    await setup(); context.invalidContinuation = {...second, ...override}; run("renderJob(invalidContinuation)"); choose("");
    assert.equal(nodes["continue-source"].disabled, true); await run("selectContinuation()"); assert.equal(requests.length, 0);
  }
  for (const change of ["state.pending=true", "state.refreshing=true", "state.experiment.unknown=true"]) {
    await setup(); run(change + "; controls()"); assert.equal(nodes["continue-source"].disabled, true);
    await run("selectContinuation()"); assert.equal(requests.length, 0);
  }
  await setup();
  context.unverifiedContinuation = {...second, status: "incomplete", result: {...second.result, ok: false, status: "stalled",
    outcome: {...second.result.outcome, ok: false, status: "stalled", answer: null, unverified_prose: "Unverified prior explanation"}}};
  run("renderJob(unverifiedContinuation)"); choose("");
  assert.equal(nodes["continue-source"].disabled, false, "a fully finished, source-gated but reference-rejected answer may reuse its source, not promote its prose");
  await nodes["continue-source"].listeners.click(); assert.equal(run("state.continuation.parent_id"), second.id);
  assert.equal(requests.length, 0); assert(nodes.answer.textContent.includes("待核對"));
  for (const accepted of [false, true]) {
    await setup(); await nodes["continue-source"].listeners.click();
    continueReply = body => {
      if (accepted) { current = {id: "lost-continued", kind: "continue", status: "running", version: body.version, question: body.question}; throw Error("reply lost"); }
      return {ok: false, status: 409, json: async () => ({error: "parent no longer retained"})};
    };
    await nodes["question-form"].listeners.submit({preventDefault() {}}); await settle();
    assert.equal(posts().length, 1); assert.equal(posts()[0].route, "/api/continue-question");
    assert.equal(nodes.question.value, rawQuestion); assert.equal(manual(), beforeFocus);
    if (accepted) assert.equal(run("state.job.id"), "lost-continued"); else assert(nodes.error.textContent.includes("parent no longer retained"));
  }
  context.fetch = originalFetch;
}
async function checkInsufficientRecovery(context, nodes) {
  const run = code => vm.runInContext(code, context), originalFetch = context.fetch, requests = [];
  const reply = value => ({ok: true, json: async () => value}), clone = value => JSON.parse(JSON.stringify(value));
  const settle = () => new Promise(resolve => setImmediate(resolve));
  const deferred = () => {let resolve; const promise = new Promise(done => {resolve = done;}); return {promise, resolve};};
  const files = [{id: "0", path: "entry.py", lines: 300}, {id: "1", path: "helper.py", lines: 300}, {id: "2", path: "other.py", lines: 4}];
  const project = {name: "recovery", version: "recovery-v1", files, excluded: []};
  const reason = "缺少 quote_field 的實作。\n<img src=x onerror=alert(1)> **不是答案** [E1:L7-L8]";
  const rawQuestion = "  保留我的新問題\n\t不沿用模型的缺失說明。  ";
  const make = (scope = {origin: "user_focus", focus: ["entry.py:7-8"], supplements: []}) => ({
    id: "insufficient-parent", kind: "explain", status: "incomplete", version: project.version, question: "ORIGINAL_QUESTION", elapsed_seconds: 1,
    files, gpu: "released", reading_scope: clone(scope), result: {kind: "forge8.explain", status: "insufficient_evidence", ok: false,
      outcome: {status: "insufficient_evidence", ok: false, answer: null, failure_reason: reason,
        source_unchanged: true, snapshot_unchanged: true, acceptance: {ok: true}}}});
  const makeProject = scope => {
    const job = make(scope); job.kind = "project";
    const focus = scope.focus.map(selector => {const [, path, start, end] = selector.match(/^(.+):(\d+)-(\d+)$/); return {path, start_line: Number(start), end_line: Number(end)};});
    job.result.project_reading = {answer_attempted: true, focus,
      context: {added: focus.filter(span => scope.supplements.includes(`${span.path}:${span.start_line}-${span.end_line}`)), skipped: {}},
      discovery: {ok: true, status: "located", snapshot_sha256: project.version, source_unchanged: true, snapshot_unchanged: true,
        ingress_unchanged: true, acceptance: {ok: true}, candidates: focus.map(span => ({...span, name: "entry", kind: "function"}))}};
    job.result.outcome.coverage = {observed: {ranges: files.filter(file => focus.some(span => span.path === file.path)).map(file => ({path: file.path,
      ranges: focus.filter(span => span.path === file.path).map(({start_line, end_line}) => ({start_line, end_line}))}))}};
    return job;
  };
  let current, sourceReply;
  const source = id => ({path: files[Number(id)].path, version: project.version, lines: Array(files[Number(id)].lines).fill("# inert retained source"), outline: {status: "available", items: []}});
  context.fetch = async (route, options) => {
    const body = options.body === undefined ? undefined : JSON.parse(options.body); requests.push({route, method: options.method, body});
    if (route.startsWith("/api/source?")) return sourceReply(new URLSearchParams(route.split("?")[1]).get("file"), options.signal);
    if (route === "/api/jobs") {current = {id: "new-manual", kind: "explain", status: "running", version: project.version, question: body.question}; return reply({id: current.id});}
    if (route === "/api/jobs/current") return reply(current);
    if (route === "/api/history") return reply({version: project.version, entries: [context.recoveryJob], evicted: 0});
    throw Error(`unexpected recovery request: ${route}`);
  };
  const setup = async (job = make()) => {
    sourceReply = id => reply(source(id)); current = job;
    context.recoveryProject = clone(project); context.recoveryJob = job;
    run("state.pending=false; state.refreshing=false; showProject(recoveryProject,true); state.history=[recoveryJob]; renderJob(recoveryJob)");
    await run("openFile(state.project.files[2],state.project.version,1,2)");
    nodes.question.value = rawQuestion;
    run('state.focus=[{file:"2",path:"other.py",start:1,end:2}]; renderSelections()');
    requests.length = 0;
  };
  const focus = () => run("JSON.stringify(state.focus)");
  const snapshot = () => ({focus: focus(), question: nodes.question.value, answer: nodes.answer.textContent,
    history: run("JSON.stringify(state.history)"), source: run("state.source"), continuation: run("state.continuation")});
  const unchanged = saved => {
    assert.equal(focus(), saved.focus); assert.equal(nodes.question.value, saved.question); assert.equal(nodes.answer.textContent, saved.answer);
    assert.equal(run("JSON.stringify(state.history)"), saved.history); assert.equal(run("state.source"), saved.source); assert.equal(run("state.continuation"), saved.continuation);
  };
  const noPosts = () => assert.equal(requests.filter(item => item.method === "POST").length, 0);
  await setup();
  assert.equal(nodes["status-label"].textContent, "模型指出資訊不足");
  assert(nodes["history-select"].textContent.includes("模型指出資訊不足"));
  const box = nodes.answer.children.find(node => node.className === "insufficient-output");
  const literal = box.children.find(node => node.className === "insufficient-reason");
  assert.equal(literal.textContent, reason); assert.equal(literal.children.length, 0, "ordinary reason uses inert text, not Markdown/citation parsing");
  assert.equal(box.querySelectorAll("img").length, 0); assert.equal(box.querySelectorAll("button").length, 1);
  assert.equal(run("recoveryJob.result.outcome.answer"), null); assert.equal(run("recoveryJob.result.ok"), false);
  assert.equal(nodes["continue-source"].disabled, false); assert.equal(nodes["replace-source"].disabled, false);
  assert.equal(nodes["question-editor"].open, true); assert.equal(requests.length, 0);
  await box.querySelectorAll("button")[0].listeners.click();
  assert.equal(run("state.source.path"), "entry.py"); assert.equal(run("state.anchor"), 7); assert.equal(run("state.end"), 8);
  assert.equal(focus(), '[{"file":"2","path":"other.py","start":1,"end":2}]'); noPosts();

  for (const mutation of [job => {job.gpu = "unknown";}, job => {job.status = "cancelled";}, job => {job.error = "transport";},
    job => {job.result.error = "bad finalization";}, job => {job.result.kind = "other";}, job => {job.result.ok = true;},
    job => {job.result.status = "stalled";}, job => {job.result.outcome.status = "stalled";}, job => {job.result.outcome.ok = true;},
    job => {job.result.outcome.answer = {};}, job => {delete job.result.outcome.answer;}, job => {job.result.outcome.source_unchanged = false;},
    job => {job.result.outcome.snapshot_unchanged = false;}, job => {job.result.outcome.acceptance.ok = false;}, job => {job.version = "old";}]) {
    const job = make(); mutation(job); await setup(job);
    assert.equal(run("gatedInsufficient(recoveryJob)"), null); assert.equal(nodes["replace-source"].hidden, true);
    assert.equal(nodes["continue-source"].disabled, true); assert(!nodes.answer.textContent.includes("模型指出資訊不足"));
    await nodes["replace-source"].listeners.click(); assert.equal(requests.length, 0);
  }
  for (const invalid of ["", " \n\t", "x".repeat(1001), "wrong\u202eordering", "bad\u0000control", "bad\ud800surrogate", "line\u2028separator", 42, null]) {
    const job = make(); job.result.outcome.failure_reason = invalid; await setup(job);
    assert.equal(run("gatedInsufficient(recoveryJob)"), null); assert.equal(nodes["replace-source"].hidden, true);
  }
  const unicode = make(); unicode.result.outcome.failure_reason = "😀".repeat(1000); await setup(unicode);
  assert.equal(run("gatedInsufficient(recoveryJob)"), unicode.result.outcome.failure_reason, "reason bound counts Unicode characters");
  const comparison = make(); comparison.comparison = {}; await setup(comparison);
  assert.equal(nodes["replace-source"].hidden, true); assert(nodes.answer.textContent.includes(reason));
  assert(!nodes.answer.textContent.includes("資訊不足回報的來源、內容或請求收尾未通過檢查"), "comparison reason is not mislabeled as failed source checks");

  const ordered = {origin: "project_candidates", focus: ["helper.py:3-4", "entry.py:7-8"], supplements: ["entry.py:7-8"]};
  await setup(makeProject(ordered)); await nodes["continue-source"].listeners.click();
  const saved = snapshot(); await nodes["replace-source"].listeners.click();
  assert.deepEqual(JSON.parse(focus()), [{file: "1", path: "helper.py", start: 3, end: 4}, {file: "0", path: "entry.py", start: 7, end: 8}]);
  assert.equal(requests.length, 2, "supplement already belongs to focus; never append or fetch it again"); noPosts();
  assert.equal(nodes.question.value, saved.question); assert.equal(nodes.answer.textContent, saved.answer);
  assert.equal(run("JSON.stringify(state.history)"), saved.history); assert.equal(run("state.source"), saved.source);
  assert.equal(run("state.continuation"), null); assert.equal(nodes["continuation-panel"].hidden, true);
  assert.equal(nodes.selections.querySelectorAll("button").filter(button => button.textContent.includes("查看名稱來源")).length, 2);
  assert(nodes["replace-source-note"].textContent.includes("新問題與提示標頭"));
  await nodes["question-form"].listeners.submit({preventDefault() {}}); await settle();
  assert.deepEqual(requests.filter(item => item.method === "POST"), [{route: "/api/jobs", method: "POST",
    body: {question: rawQuestion, version: project.version, focus: [{file: "1", start: 3, end: 4}, {file: "0", start: 7, end: 8}]}}]);
  assert(!JSON.stringify(requests.filter(item => item.method === "POST")).includes("ORIGINAL_QUESTION"));

  const four = {origin: "project_candidates", focus: ["entry.py:1-2", "entry.py:4-5", "entry.py:7-8", "entry.py:10-11"], supplements: []};
  await setup(makeProject(four)); const fourSaved = snapshot();
  assert.equal(nodes["replace-source"].hidden, false); assert.equal(nodes["replace-source"].disabled, true);
  assert.equal(nodes.answer.querySelectorAll("button").filter(button => button.dataset.retained).length, 4);
  await nodes["replace-source"].listeners.click(); unchanged(fourSaved); assert.equal(requests.length, 0);
  assert(nodes["replace-source-note"].textContent.includes("不會擷取前三段"));
  await nodes["continue-source"].listeners.click(); assert.equal(run("state.continuation.scope.focus.length"), 4); assert.equal(requests.length, 0);
  const badProject = makeProject(ordered); badProject.result.outcome.coverage.observed.ranges = []; await setup(badProject);
  assert.equal(nodes["replace-source"].hidden, true); assert(!nodes.answer.textContent.includes("模型指出資訊不足"));

  for (const mode of ["answered", "unverified", "continued"]) {
    const job = make();
    if (mode === "continued") {job.kind = "continue"; job.continuation = {parent_id: "old", parent_question: "OLDER", scope: clone(job.reading_scope)};}
    else if (mode === "answered") {job.status = "answered"; job.result.status = job.result.outcome.status = "answered"; job.result.ok = job.result.outcome.ok = true; job.result.outcome.answer = {claims: [{text: "ACCEPTED_PROSE", citations: []}]};}
    else {job.result.status = job.result.outcome.status = "stalled"; job.result.outcome.unverified_prose = "UNVERIFIED_PROSE";}
    await setup(job); assert.equal(nodes["replace-source"].disabled, false, mode);
    const before = snapshot(); await nodes["replace-source"].listeners.click();
    assert.equal(nodes.answer.textContent, before.answer); assert.equal(nodes.question.value, rawQuestion); assert.equal(run("state.focus[0].start"), 7); noPosts();
  }

  // Conservative source-only preflight includes numbered gutters and evidence JSON.
  // The real next-question prompt budget remains server-owned.
  for (const [line, expected] of [["x".repeat(3993), true], ["x".repeat(3994), false], ["😀".repeat(3993), true]]) {
    await setup(make({origin: "user_focus", focus: ["entry.py:1-1"], supplements: []}));
    sourceReply = id => {const value = source(id); value.lines[0] = line; return reply(value);};
    const before = snapshot(); await nodes["replace-source"].listeners.click();
    assert.equal(run("state.focus[0].start") === 1 && run("state.focus[0].file") === "0", expected);
    if (!expected) {unchanged(before); assert(nodes.error.textContent.includes("保守預算"));} noPosts();
  }
  for (const line of ["x".repeat(3000), '"'.repeat(1550), "\\".repeat(1550)]) {
    await setup(make({origin: "user_focus", focus: ["entry.py:1-1", "entry.py:2-2", "entry.py:3-3"], supplements: []}));
    sourceReply = id => {const value = source(id); value.lines.fill(line, 0, 3); return reply(value);};
    const before = snapshot(); await nodes["replace-source"].listeners.click(); unchanged(before);
    assert(nodes.error.textContent.includes("9,000")); assert.equal(requests.length, 3); noPosts();
  }
  await setup(make({origin: "user_focus", focus: ["entry.py:1-2", "entry.py:1-1"], supplements: []}));
  sourceReply = id => {const value = source(id); value.lines.fill("x".repeat(1900), 0, 2); return reply(value);};
  await nodes["replace-source"].listeners.click();
  assert.deepEqual(JSON.parse(focus()).map(span => [span.start, span.end]), [[1, 2], [1, 1]], "contained evidence can reuse budget without merging or dropping original windows"); noPosts();
  for (const corrupt of [() => null, value => ({...value, version: "old"}), value => ({...value, path: "other.py"}),
    value => ({...value, lines: value.lines.slice(1)}), value => ({...value, lines: value.lines.map((line, index) => index === 0 ? 3 : line)}),
    () => {throw Error("GET offline <img src=x>");}]) {
    await setup(makeProject(ordered)); const before = snapshot();
    sourceReply = id => reply(id === "0" ? corrupt(source(id)) : source(id));
    await nodes["replace-source"].listeners.click(); unchanged(before); assert.equal(requests.length, 2); noPosts();
    assert(nodes["replace-source-note"].textContent.includes("未取代任何選段")); assert.equal(nodes.error.querySelectorAll("img").length, 0);
  }

  const completion = {schema_version: 1, scope: "resident_request", server_session_id: "recovery-session", request_completed: true,
    slot_idle: true, transport_secret_cleared: true, session_finalization: "pending"};
  const resident = make(); resident.gpu = "resident"; resident.request_completion = {...completion}; resident.result.kind += ".request";
  resident.result.request_completion = {...completion}; resident.result.outcome.request_completion = {...completion};
  resident.result.server = {scope: "resident_request", server_session_id: completion.server_session_id, request_completion: {...completion}};
  resident.result.outcome.acceptance = {ok: true, reason: null, evidence: {...completion}};
  await setup(resident); assert.equal(nodes["replace-source"].disabled, false);
  await nodes["replace-source"].listeners.click(); assert.equal(run("state.focus[0].file"), "0"); assert.equal(run("state.job.gpu"), "resident"); noPosts();
  const mismatched = clone(resident); mismatched.result.outcome.acceptance.evidence.server_session_id = "other-session";
  await setup(mismatched); assert.equal(nodes["replace-source"].hidden, true);

  // Slow source GETs cannot overwrite a changed editor/answer/navigation/ownership state.
  const changes = [() => run("state.sourceRequest++"),
    () => run("const priorFocus=state.focus; state.focus=[]; renderSelections(); state.focus=priorFocus; renderSelections()"),
    () => {nodes.question.value += " edited"; nodes.question.listeners.input(); nodes.question.value = rawQuestion; nodes.question.listeners.input();},
    () => {nodes["history-select"].value = "insufficient-parent"; nodes["history-select"].listeners.change(); nodes["history-select"].value = ""; nodes["history-select"].listeners.change();},
    () => run("showProject({...state.project},true)"), () => run("state.historyRequest++"),
    () => run("state.pending=true; controls()"), () => run("state.job={id:'another-job',status:'running'}; controls()"),
    () => run("state.experiment.unknown=true; controls()"), () => run("state.experiment.job={id:'another-trial',status:'running'}; controls()"),
    () => run("state.refreshing=true; controls()"), () => run("state.pairPending=true; controls()"),
    () => run("state.project.model_policy={mode:'resident',idle_timeout_seconds:600}; state.modelUnknown=true; controls()"),
    () => run("state.project.model_policy={mode:'resident',idle_timeout_seconds:600}; state.modelReleasePending=true; controls()"),
    () => run("selectContinuation()"), () => run("selectContinuation(); clearContinuation()")];
  for (const [index, change] of changes.entries()) {
    await setup(); const late = deferred(); sourceReply = () => late.promise;
    const work = nodes["replace-source"].listeners.click(); assert.equal(requests.length, 1); assert.equal(nodes["replace-source"].disabled, true);
    change(); const before = snapshot(); late.resolve(reply(source("0"))); await work;
    unchanged(before); assert.equal(requests.length, 1, `stale case ${index} has no follow-up request`); assert(!run("state.sourceRecovery?.pending"));
  }
  await setup(); const lateFailure = deferred(); sourceReply = () => lateFailure.promise;
  const work = nodes["replace-source"].listeners.click(); nodes.question.value = "Newer draft"; nodes.question.listeners.input();
  lateFailure.resolve({ok: false, json: async () => ({error: "stale failure"})}); await work;
  assert.equal(nodes.question.value, "Newer draft"); assert(nodes["replace-source-note"].textContent.includes("過期讀取"));
  assert(!nodes.error.textContent.includes("stale failure")); assert.equal(requests.length, 1);
  await setup(); const beforeTimeout = snapshot(), originalTimer = context.setTimeout; let timedOut;
  context.setTimeout = (callback, delay) => {assert.equal(delay, 10000); timedOut = callback; return 1;};
  sourceReply = (_id, signal) => new Promise((_resolve, reject) => signal.addEventListener("abort", () => reject(Error("aborted")), {once: true}));
  const timedWork = nodes["replace-source"].listeners.click(); timedOut(); await timedWork;
  unchanged(beforeTimeout); assert(nodes.error.textContent.includes("10 秒")); assert.equal(nodes["replace-source"].disabled, false);
  assert.equal(requests.length, 1, "timed-out GET is not retried"); context.setTimeout = originalTimer;
  await setup(); context.fetch = originalFetch;
}
async function checkResidentModel(context, nodes) {
  const run = code => vm.runInContext(code, context), reply = value => ({ok: true, json: async () => value});
  const settle = () => new Promise(resolve => setImmediate(resolve)), clone = value => JSON.parse(JSON.stringify(value));
  const deferred = () => {let resolve; const promise = new Promise(done => {resolve = done;}); return {promise, resolve};};
  const originalFetch = context.fetch, originalTimer = context.setTimeout, requests = [], timers = [];
  context.setTimeout = (callback, delay) => {timers.push({callback, delay}); return timers.length;};
  const files = [{id: "0", path: "a.py", lines: 4}];
  const project = {name: "resident", version: "resident-v1", files, excluded: [], reader: "qwen35", model_policy: {mode: "resident", idle_timeout_seconds: 600}};
  const completion = {schema_version: 1, scope: "resident_request", server_session_id: "session-one", request_completed: true,
    slot_idle: true, transport_secret_cleared: true, session_finalization: "pending"};
  const acceptance = (metadata = completion) => ({ok: true, reason: null, evidence: {...metadata, source_checks: true}});
  const serverPayload = (metadata = completion) => ({scope: "resident_request", server_session_id: metadata.server_session_id, request_completion: {...metadata}});
  const first = {id: "resident-a", kind: "explain", status: "answered", version: project.version, files,
    question: "A_ORIGINAL_QUESTION", elapsed_seconds: 20, gpu: "resident", request_completion: {...completion},
    reading_scope: {focus: ["a.py:1-2"], origin: "user_focus", supplements: []},
    result: {kind: "forge8.explain.request", status: "answered", ok: true, request_completion: {...completion}, server: serverPayload(),
      outcome: {status: "answered", ok: true, source_unchanged: true, snapshot_unchanged: true, acceptance: acceptance(), request_completion: {...completion},
        coverage: {observed: {ranges: [{path: "a.py", ranges: [{start_line: 1, end_line: 2}]}]}},
        answer: {claims: [{text: "RESIDENT_ANSWER [E1:L1-L2]", citations: [{evidence_id: "E1", path: "a.py", start_line: 1, end_line: 2}]}]}}}};
  const idle = (id = "session-one", remaining = 600) => ({enabled: true, id, state: "idle", reader: "qwen35", idle_remaining_seconds: remaining, release_allowed: true, receipts: []});
  const unloaded = () => ({enabled: true, id: null, state: "unloaded", reader: "qwen35", idle_remaining_seconds: null, release_allowed: false, receipts: []});
  let model = idle(), current = first, archived = [first], modelReply = () => reply(model);
  let releaseReply = () => {model = unloaded(); return reply({state: "releasing"});};
  context.fetch = async (route, options) => {
    const body = options.body === undefined ? undefined : JSON.parse(options.body);
    requests.push({route, method: options.method, body});
    if (route === "/api/model") {assert.equal(options.method, "GET"); return modelReply();}
    if (route === "/api/model/release") {assert.equal(options.method, "POST"); return releaseReply(body);}
    if (route === "/api/project") return reply(project);
    if (route === "/api/jobs/current") return reply(current);
    if (route === "/api/history") return reply({version: project.version, entries: archived, evicted: 0});
    if (route === "/api/experiment/current") return reply({id: null, status: "idle"});
    if (route === "/api/continue-question") {
      assert.deepEqual(body, {question: "PRESERVED_DRAFT", version: project.version, parent: first.id});
      current = {id: "resident-b", kind: "continue", question: body.question, status: "running", version: project.version};
      model = {...idle(), state: "busy", idle_remaining_seconds: null, release_allowed: false}; return reply({id: current.id});
    }
    if (route === "/api/jobs/resident-b/cancel") {
      current = {id: "resident-b", question: "PRESERVED_DRAFT", version: project.version, status: "cancelled", gpu: "released", elapsed_seconds: 1};
      model = unloaded(); archived = [first, current]; return reply({status: "cancelling"});
    }
    if (route === "/api/refresh") {current = {id: null, status: "idle"}; archived = []; return reply({...project});}
    assert.equal(route, "/api/source?file=0&version=resident-v1"); assert.equal(options.method, "GET");
    return reply({version: project.version, path: "a.py", lines: ["first", "second", "third", "fourth"], outline: {status: "unsupported", items: []}});
  };
  context.residentProject = project; context.residentJob = first;
  const checkProjectPhaseCopy = resident => {
    context.savedPhaseJob = run("state.job");
    const before = requests.length;
    const rows = [
      ["Project question: locating source before answering.", "專案提問：先從目錄尋找相關原碼，尚未進行回答。"],
      ["Project question: preparing complete candidate source.", resident ?
        "專案提問：定位請求已完成，正在檢查全部候選原碼能否完整放入回答。" :
        "專案提問：定位模型已釋放，正在檢查全部候選原碼能否完整放入回答。"],
      ["Project question: answering from the automatically selected source.", resident ?
        "專案提問：依自動選中的完整原碼回答；原有手動選段保持不變。模型狀態請見上方。" :
        "專案提問：重新載入模型，依自動選中的完整原碼回答；原有手動選段保持不變。"]
    ];
    for (const [phase, expected] of rows) {
      context.projectPhase = phase;
      run('renderJob({id:"project-phase-copy",kind:"project",status:"running",phase:projectPhase})');
      assert.equal(nodes.phase.textContent, expected, resident ? "resident phases describe request progress, not process release/reload" : "one-shot lifecycle phase copy stays accurate");
      if (resident) assert(!/已釋放|重新載入|沿用常駐模型/.test(nodes.phase.textContent), "a workflow phase cannot prove live server reuse after possible idle expiry");
    }
    run("renderJob(savedPhaseJob)");
    assert.equal(requests.length, before, "phase rendering must not query or mutate the model");
  };
  run("showProject(residentProject)"); await settle(); await run("poll()"); await settle();
  assert.equal(nodes["model-status"].hidden, false); assert(nodes["model-label"].textContent.includes("常駐"));
  assert(nodes["ask-project-note"].textContent.includes("共用常駐")); assert(!nodes["ask-project-note"].textContent.includes("分兩次載入"));
  checkProjectPhaseCopy(true);
  assert(nodes.answer.textContent.includes("RESIDENT_ANSWER")); assert(nodes.answer.textContent.includes("請求層級紀錄"));
  assert.equal(run("state.history.length"), 1); assert.equal(nodes["continue-source"].disabled, false);
  nodes.question.value = "PRESERVED_DRAFT"; run('state.focus=[{file:"0",path:"a.py",start:3,end:4}]; renderSelections()');
  await nodes["continue-source"].listeners.click(); nodes["question-editor"].open = true;
  const preserve = run("JSON.stringify([state.focus,state.continuation])"), answerNodes = [...nodes.answer.children], options = [...nodes["history-select"].children];
  const citation = nodes.answer.querySelectorAll("button").find(button => button.className === "citation");
  await citation.listeners.click(); const preview = citation.parentElement.children[1];
  assert.equal(nodes.ask.disabled, false); assert.equal(nodes["model-release"].disabled, false);
  const posts = () => requests.filter(item => item.method === "POST");
  const beforePollPosts = posts().length;
  model = idle("session-one", 0); await run("pollModel()"); run("updateModelCountdown()");
  assert.equal(run("state.model.state"), "idle"); assert(nodes["model-countdown"].textContent.includes("等待服務確認"));
  assert.equal(posts().length, beforePollPosts, "zero on a browser countdown is not authority to release or reload");
  assert.deepEqual(nodes.answer.children, answerNodes); assert.deepEqual(nodes["history-select"].children, options);
  assert.equal(preview.hidden, false); assert.equal(nodes["question-editor"].open, true);
  assert(timers.some(item => item.callback.name === "pollModel" && item.delay === 5000), "resident polling continues after a terminal question");

  // Independent model-status failures preserve completed answers and exact source bindings.
  modelReply = () => {throw Error("model status offline");}; await run("pollModel()");
  assert.equal(run("state.job.status"), "answered"); assert.deepEqual(nodes.answer.children, answerNodes);
  assert.equal(nodes.ask.disabled, true); assert.equal(nodes.refresh.disabled, true); assert.equal(nodes.question.disabled, false);
  assert.equal(nodes["history-select"].disabled, false); assert.equal(run("JSON.stringify([state.focus,state.continuation])"), preserve);
  assert(nodes["model-note"].textContent.includes("不會重送")); assert.equal(nodes["model-recheck"].hidden, false);
  modelReply = () => reply(model); model = idle(); await nodes["model-recheck"].listeners.click();
  assert.equal(nodes.ask.disabled, false); assert.equal(nodes["model-release"].disabled, false);

  // Lost release reply sends exactly one mutation; current-session GET is authoritative.
  releaseReply = body => {assert.deepEqual(body, {session_id: "session-one"}); model = unloaded(); throw Error("release response lost");};
  const releasesBefore = posts().length;
  await nodes["model-release"].listeners.click();
  assert.equal(posts().length, releasesBefore + 1); assert.equal(posts().at(-1).route, "/api/model/release");
  assert.equal(run("state.model.state"), "unloaded"); assert.equal(nodes["model-release"].disabled, true);
  assert.equal(nodes.ask.disabled, false); assert.equal(run("state.job.gpu"), "resident", "request-time residency is immutable, not live GPU state");
  assert.deepEqual(nodes.answer.children, answerNodes); assert.equal(preview.hidden, false);
  assert.equal(nodes.question.value, "PRESERVED_DRAFT"); assert.equal(run("JSON.stringify([state.focus,state.continuation])"), preserve);
  assert.equal(nodes["continue-source"].disabled, false, "released live server does not invalidate prior resident request provenance");
  model = idle(); await run("pollModel()");
  releaseReply = () => {throw Error("release reply unavailable");}; modelReply = () => {throw Error("release status unavailable");};
  const uncertainPosts = posts().length; await nodes["model-release"].listeners.click(); await nodes["model-release"].listeners.click();
  assert.equal(posts().length, uncertainPosts + 1); assert.equal(run("state.modelUnknown"), true);
  assert.equal(nodes.ask.disabled, true); assert.equal(nodes["model-release"].disabled, true); assert.deepEqual(nodes.answer.children, answerNodes);
  model = unloaded(); modelReply = () => reply(model); await nodes["model-recheck"].listeners.click();
  assert.equal(nodes.ask.disabled, false);

  // A failed final integrity receipt is not the same thing as an unknown/running GPU.
  const receipt = {id: "session-one", reason: "explicit", ok: false, process_reclaimed: true, asset_identity_unchanged: false,
    request_manifests_unchanged: true, supervisor_secret_cleared: true, transport_secrets_cleared: true,
    receipt_path: "D:/Forge8/private/<img src=x>/closed.json"};
  model = {...unloaded(), receipts: [receipt]}; await run("pollModel()");
  assert.equal(run("state.modelUnknown"), false); assert.equal(run("state.model.state"), "unloaded");
  assert.equal(nodes["model-label"].textContent, "模型未載入"); assert.equal(nodes["model-receipt-warning"].hidden, false);
  assert(nodes["model-receipt-warning"].textContent.includes("驗證未通過")); assert(nodes["model-receipts-list"].textContent.includes("模型／執行環境身份"));
  assert(nodes["model-receipts-list"].textContent.includes(receipt.receipt_path)); assert.equal(nodes["model-receipts-list"].querySelectorAll("img").length, 0);
  assert.equal(nodes["model-receipts-list"].querySelectorAll("a").length, 0, "private receipt paths are inert text, not navigation authority");
  assert.equal(run("state.job.gpu"), "resident"); assert.deepEqual(nodes.answer.children, answerNodes);
  nodes["model-receipts"].open = true; const receiptNodes = [...nodes["model-receipts-list"].children]; await run("pollModel()");
  assert.deepEqual(nodes["model-receipts-list"].children, receiptNodes); assert.equal(nodes["model-receipts"].open, true);
  const passedReceipt = {...receipt, ok: true, asset_identity_unchanged: true};
  model = {...unloaded(), receipts: [passedReceipt]}; await run("pollModel()");
  assert.equal(nodes["model-receipt-warning"].hidden, true); assert(nodes["model-receipts-list"].textContent.includes("不代表解讀正確"));
  for (const receipts of [[{...receipt, ok: "false"}], [{...receipt, receipt_path: "x".repeat(4097)}],
    [{...receipt, receipt_path: "bad\npath"}], [{...receipt, receipt_path: ""}], [{...receipt, id: "bad/id"}],
    [{...receipt, reason: "invented"}], [{...receipt, extra: true}], [{...receipt, ok: true}],
    [{...receipt, transport_secrets_cleared: null}], [{...receipt, process_reclaimed: null}], [receipt, receipt], Array.from({length: 6}, (_, i) => ({...receipt, id: `session-${i}`}))]) {
    model = {...unloaded(), receipts}; await run("pollModel()"); assert.equal(run("state.modelUnknown"), true);
    assert.deepEqual(nodes.answer.children, answerNodes);
  }
  model = unloaded(); await run("pollModel()"); assert.equal(nodes["model-receipts"].hidden, true);

  // Concurrent stale GETs and stale release replies must not restore session-one over session-two.
  const older = deferred(); modelReply = () => older.promise; const oldPoll = run("pollModel()");
  model = idle("session-two"); modelReply = () => reply(model); await run("pollModel()");
  older.resolve(reply(idle("session-one"))); await oldPoll; assert.equal(run("state.model.id"), "session-two");
  const releaseGate = deferred(); releaseReply = body => {assert.deepEqual(body, {session_id: "session-two"}); return releaseGate.promise;};
  const releasing = nodes["model-release"].listeners.click(); assert.equal(nodes["model-release"].disabled, true);
  assert.equal(nodes.ask.disabled, true); assert.equal(nodes.question.disabled, false);
  await nodes["model-release"].listeners.click(); assert.equal(posts().at(-1).body.session_id, "session-two");
  const oneReleaseCount = posts().length; model = idle("session-three"); releaseGate.resolve(reply({state: "releasing", id: "session-two"})); await releasing;
  assert.equal(posts().length, oneReleaseCount); assert.equal(run("state.model.id"), "session-three");
  assert.equal(nodes.ask.disabled, false); assert.equal(nodes["model-release"].disabled, false);

  // Lifecycle transitions block mutations, not browsing, history, or editing an unsent draft.
  for (const status of ["checking", "loading", "busy", "releasing", "cleanup_unknown"]) {
    model = {...idle("session-three"), state: status, idle_remaining_seconds: null, release_allowed: false}; await run("pollModel()");
    assert.equal(nodes.ask.disabled, true, status); assert.equal(nodes.locate.disabled, true, status); assert.equal(nodes.refresh.disabled, true, status);
    assert.equal(nodes.question.disabled, false, status); assert.equal(nodes["history-select"].disabled, false, status);
    assert.equal(run("JSON.stringify([state.focus,state.continuation])"), preserve, status);
    assert.deepEqual(nodes.answer.children, answerNodes, status); assert.equal(nodes["model-release"].disabled, true, status);
  }
  const latchPosts = posts().length; await nodes["model-recheck"].listeners.click(); assert.equal(posts().length, latchPosts);
  assert.equal(run("state.model.state"), "cleanup_unknown", "rechecking cannot clear an unchanged cleanup latch");
  for (const status of ["checking", "releasing", "cleanup_unknown"]) {
    model = {...unloaded(), state: status}; await run("pollModel()");
    assert.equal(run("state.modelUnknown"), false); assert.equal(nodes.ask.disabled, true);
  }
  for (const bad of [{...idle(), release_allowed: "yes"}, {...idle(), id: null}, {...idle(), idle_remaining_seconds: -1},
    {...idle(), idle_remaining_seconds: Infinity}, {...idle(), state: "invented"}, {...unloaded(), release_allowed: true}, {...idle(), receipts: null}]) {
    model = bad; await run("pollModel()"); assert.equal(run("state.modelUnknown"), true); assert.equal(nodes.ask.disabled, true);
    assert.deepEqual(nodes.answer.children, answerNodes);
  }
  model = idle(); await run("pollModel()");

  // A resident GPU label alone cannot satisfy the new completion envelope.
  const emit = value => {context.residentCase = value; run('state.rendered=""; renderJob(residentCase)');};
  for (const mutate of [value => {delete value.request_completion;}, value => {delete value.result.request_completion;},
    value => {value.request_completion.slot_idle = false;}, value => {value.result.request_completion.server_session_id = "wrong";},
    value => {value.request_completion.unexpected = true;}, value => {value.result.outcome.acceptance.evidence.scope = "one_shot";},
    value => {value.result.outcome.acceptance.evidence.server_session_id = "wrong";}, value => {value.result.outcome.acceptance.evidence.transport_secret_cleared = false;},
    value => {value.result.outcome.acceptance.ok = false;}, value => {value.result.outcome.source_unchanged = false;},
    value => {value.result.outcome.snapshot_unchanged = false;}, value => {value.status = "cancelled";},
    value => {value.error = "request failed";}, value => {value.result.ok = false;}, value => {value.gpu = "released";},
    value => {value.result.kind = "forge8.explain";}, value => {value.version = "wrong";}, value => {delete value.id;},
    value => {delete value.result.outcome.request_completion;}, value => {value.result.server.server_session_id = "wrong";},
    value => {delete value.result.server;}, value => {value.request_completion.server_session_id = "bad/id";},
    value => {value.request_completion.schema_version = true;}]) {
    const invalid = clone(first); mutate(invalid); emit(invalid);
    assert(!nodes.answer.textContent.includes("RESIDENT_ANSWER")); assert.equal(nodes["continue-source"].disabled, true);
  }
  const review = clone(first); review.status = "incomplete"; review.result.ok = false; review.result.status = "stalled";
  Object.assign(review.result.outcome, {status: "stalled", ok: false, answer: null, unverified_prose: "RESIDENT_UNVERIFIED [E9:L1]"});
  emit(review); assert(nodes.answer.textContent.includes("RESIDENT_UNVERIFIED")); assert(nodes.answer.textContent.includes("引用未通過"));
  assert.equal(nodes["continue-source"].disabled, false);
  model = unloaded(); await run("pollModel()"); assert(nodes.answer.textContent.includes("RESIDENT_UNVERIFIED"));

  const discovery = {ok: true, status: "located", snapshot_sha256: project.version, source_unchanged: true,
    snapshot_unchanged: true, ingress_unchanged: true, acceptance: acceptance(), request_completion: {...completion},
    candidates: [{path: "a.py", name: "first", kind: "function", start_line: 1, end_line: 2}]};
  const located = clone(first); located.kind = "locate"; located.status = "located";
  located.result = {kind: "forge8.locate.request", ok: true, status: "located", request_completion: {...completion}, server: serverPayload(), outcome: discovery};
  emit(located); assert(nodes.answer.textContent.includes("AI 建議閱讀位置")); assert.equal(nodes["continue-source"].disabled, true);
  const projected = clone(first); projected.kind = "project";
  projected.reading_scope.origin = "project_candidates";
  projected.result.project_reading = {answer_attempted: true, focus: [{path: "a.py", start_line: 1, end_line: 2}], discovery};
  emit(projected); assert(nodes.answer.textContent.includes("RESIDENT_ANSWER")); assert(nodes.answer.textContent.includes("模型選材"));
  assert.equal(nodes["continue-source"].disabled, false);
  const otherCompletion = {...completion, server_session_id: "earlier-discovery-session"};
  projected.result.project_reading.discovery = {...discovery, request_completion: otherCompletion, acceptance: acceptance(otherCompletion)};
  emit(projected); assert(nodes.answer.textContent.includes("RESIDENT_ANSWER"));
  assert.equal(nodes["continue-source"].disabled, false, "independently checked discovery and reading can belong to two generations after idle expiry");
  projected.result.project_reading.discovery = {...discovery, request_completion: otherCompletion};
  emit(projected); assert(!nodes.answer.textContent.includes("RESIDENT_ANSWER"), "discovery must match its own gate, not just have a plausible other session ID");
  projected.result.project_reading.discovery = {...discovery, acceptance: {ok: true}};
  emit(projected); assert(!nodes.answer.textContent.includes("RESIDENT_ANSWER"));
  const selectionOnly = clone(first); selectionOnly.kind = "project"; selectionOnly.status = "incomplete";
  Object.assign(selectionOnly.result, {status: "selection_required", ok: false, outcome: null,
    project_reading: {answer_attempted: false, focus: [], discovery}});
  emit(selectionOnly); assert(nodes.answer.textContent.includes("候選尚未進入回答"));
  assert(nodes.answer.querySelectorAll("button").some(button => button.dataset.discovery === "true"));
  assert.equal(nodes["continue-source"].disabled, true, "completed discovery is not a completed source-reading scope");
  selectionOnly.result.project_reading.discovery = {...discovery, acceptance: {ok: true}};
  emit(selectionOnly); assert(!nodes.answer.querySelectorAll("button").some(button => button.dataset.discovery === "true"));

  // Exact-source continuation stays source-only; cancellation targets active B while viewing A.
  emit(first); current = first; archived = [first]; model = idle(); await run("pollModel()"); await run("loadHistory()");
  await nodes["continue-source"].listeners.click(); await nodes["question-form"].listeners.submit({preventDefault() {}}); await settle();
  assert.equal(run("state.job.id"), "resident-b"); assert.equal(nodes["model-release"].disabled, true);
  nodes["history-select"].value = first.id; nodes["history-select"].listeners.change();
  assert(nodes.answer.textContent.includes("RESIDENT_ANSWER"));
  await nodes.cancel.listeners.click(); await settle();
  assert.equal(posts().at(-1).route, "/api/jobs/resident-b/cancel"); assert(nodes.answer.textContent.includes("RESIDENT_ANSWER"));
  assert.equal(nodes.question.value, "PRESERVED_DRAFT");

  // Native reload is GET-only and restores completed request plus independent residency.
  current = first; archived = [first]; model = idle("session-reloaded"); const beforeReload = posts().length;
  await run(app.slice(app.lastIndexOf("(async () => {"))); await settle();
  assert.equal(posts().length, beforeReload); assert(nodes.answer.textContent.includes("RESIDENT_ANSWER"));
  assert.equal(run("state.model.id"), "session-reloaded");
  const stale = deferred(); modelReply = () => stale.promise; const stalePoll = run("pollModel()");
  modelReply = () => reply(unloaded()); await nodes.refresh.listeners.click(); await settle();
  stale.resolve(reply(idle("stale-after-refresh"))); await stalePoll;
  assert.equal(run("state.model.state"), "unloaded"); assert.equal(run("state.history.length"), 0);
  assert.equal(run("state.continuation"), null); assert.equal(nodes.question.value, "PRESERVED_DRAFT");

  // Idle GPU residency does not prohibit an explicitly selected CPU-only trial; active work does.
  run("state.project.experiments_enabled=true; state.experiment.unknown=false; state.experiment.visible=true; state.experiment.input='{}'; state.experiment.target={file:'0',path:'a.py',version:state.project.version,entry:'first',source_sha256:'a'.repeat(64),source_bytes:24}; state.experiment.baseTarget=state.experiment.target");
  nodes["experiment-input"].value = "{}"; model = idle(); modelReply = () => reply(model); await run("pollModel()");
  assert.equal(nodes["experiment-run"].disabled, false);
  run('renderJob({id:"busy-question",status:"running"})'); assert.equal(nodes["experiment-run"].disabled, true);
  run('renderJob({id:null,status:"idle"})'); model = {...idle(), state: "cleanup_unknown", idle_remaining_seconds: null, release_allowed: false}; await run("pollModel()");
  assert.equal(nodes["experiment-run"].disabled, true); assert.equal(nodes["experiment-input"].readOnly, false);
  const legacyCount = requests.length; run('showProject({name:"legacy",version:"legacy",files:[],excluded:[],model_policy:{mode:"one_shot"}})');
  await run("pollModel()"); run("updateModelCountdown()"); await settle();
  assert.equal(requests.length, legacyCount, "one-shot policy never polls resident state"); assert.equal(nodes["model-status"].hidden, true);
  assert(nodes["ask-project-note"].textContent.includes("分兩次載入"));
  checkProjectPhaseCopy(false);
  run("delete state.project.model_policy"); checkProjectPhaseCopy(false);
  context.fetch = originalFetch; context.setTimeout = originalTimer;
}
async function checkQuestionTransport(context, nodes) {
  const run = code => vm.runInContext(code, context), original = {fetch: context.fetch, setTimeout: context.setTimeout, clearTimeout: context.clearTimeout};
  const timers = new Map(), requests = [], holds = [], settle = () => new Promise(resolve => setImmediate(resolve));
  const reply = value => ({ok: true, status: 200, json: async () => value});
  const question = "  ORIGINAL_QUESTION\r\n\tKeep indentation and <img src=x>.  ";
  const project = {name: "transport", version: "transport-v1", files: [{id: "0", path: "a.py", lines: 2}], excluded: []};
  let nextTimer = 0, current, postReply, statusReply, historyReply, cancelReply;
  context.setTimeout = (callback, milliseconds) => { const id = ++nextTimer; timers.set(id, {callback, milliseconds}); return id; };
  context.clearTimeout = id => timers.delete(id);
  const deadlines = () => [...timers].filter(([, timer]) => timer.milliseconds === 10000);
  const expire = () => {
    const pending = deadlines(); assert.equal(pending.length, 1, "exactly one outstanding request must have a 10-second deadline");
    const [id, timer] = pending[0]; timers.delete(id); timer.callback();
  };
  const held = (signal, stage) => {
    assert(signal && typeof signal.addEventListener === "function", `${stage} requires an AbortController signal`);
    const hold = {signal, stage, aborted: false}; holds.push(hold);
    return new Promise((resolve, reject) => {
      const abort = () => { hold.aborted = true; const error = Error("request aborted"); error.name = "AbortError"; reject(error); };
      if (signal.aborted) abort(); else signal.addEventListener("abort", abort, {once: true});
    });
  };
  const delayed = (stage, options) => stage === "fetch" ? held(options.signal, stage) : {ok: true, status: 200, json: () => held(options.signal, stage)};
  const posts = () => requests.filter(item => item.method === "POST");
  const gets = route => requests.filter(item => item.method === "GET" && item.route === route);
  const preserved = () => JSON.stringify({question: nodes.question.value, focus: run("state.focus"), source: run("state.source"), history: run("state.history")});
  context.fetch = async (route, options) => {
    const body = options.body === undefined ? undefined : JSON.parse(options.body);
    requests.push({route, method: options.method, body, signal: options.signal});
    if (route === "/api/jobs") return postReply(options, body);
    if (route === "/api/jobs/current") return statusReply(options);
    if (route === "/api/history") return historyReply(options);
    if (route === `/api/jobs/${current.id}/cancel`) return cancelReply(options);
    throw Error(`unexpected bounded transport request: ${route}`);
  };
  const setup = (previous = "idle") => {
    timers.clear(); requests.length = 0; holds.length = 0;
    current = previous === "idle" ? {id: null, status: "idle"} : {id: "old-terminal", status: "cancelled", question: "OLD_QUESTION", version: project.version, gpu: "released", elapsed_seconds: 1};
    context.transportProject = project; context.transportCurrent = current;
    run("state.pending=false; state.refreshing=false; showProject(transportProject,true); renderJob(transportCurrent); state.source={file:transportProject.files[0],path:'a.py',version:transportProject.version,lines:['def entry():','    return 1'],outline:{status:'available',items:[]}}; state.anchor=1; state.end=2; state.focus=[{file:'0',path:'a.py',start:1,end:2}]; renderSelections()");
    nodes.question.value = question; nodes.question.listeners.input();
    postReply = () => reply({id: "new-question"}); statusReply = () => reply(current);
    historyReply = () => reply({version: project.version, entries: [], evicted: 0});
    cancelReply = () => reply({status: "cancelling"});
    assert.equal(nodes.ask.disabled, false); assert.equal(run("polling"), false);
  };
  const unknownLocked = () => {
    assert.equal(run("state.job.status"), "unknown"); assert.equal(run("state.pending"), false);
    for (const id of ["ask", "ask-project", "locate", "refresh", "change-mode", "add-selection"]) assert.equal(nodes[id].disabled, true, `${id} must remain locked while submission is uncertain`);
    assert.equal(nodes.cancel.disabled, true, "an unidentified request cannot cancel the previous job");
    assert(!nodes.answer.textContent.includes("本題未能送出")); assert(!nodes.phase.textContent.includes("本題未能送出"));
    assert(run("state.submission !== null"), "one unchanged GET cannot discard uncertain submission identity");
  };
  try {
    // A timed-out fetch or response body, and a genuine network failure, are
    // not definitive rejections. The old current job may precede publication.
    for (const previous of ["idle", "terminal"]) for (const stage of ["fetch", "json", "network"]) {
      setup(previous); const before = preserved();
      postReply = options => { if (stage === "network") throw Error("network reply lost"); return delayed(stage, options); };
      const submission = nodes["question-form"].listeners.submit({preventDefault() {}});
      await settle();
      if (stage !== "network") {
        assert.equal(run("state.pending"), true); assert.equal(nodes.cancel.disabled, true);
        assert.equal(holds.length, 1); expire();
      }
      await submission; await settle(); unknownLocked();
      assert.equal(preserved(), before); assert.equal(posts().length, 1); assert.equal(deadlines().length, 0);
      assert.deepEqual(posts()[0].body, {question, version: project.version, focus: [{file: "0", start: 1, end: 2}]});
      if (stage !== "network") assert(holds[0].aborted && holds[0].signal.aborted);
      await run("submitQuestion()"); await run("poll()"); unknownLocked();
      assert.equal(posts().length, 1, "neither a repeated click nor GET reconciliation may replay the question");
      assert(gets("/api/jobs/current").length >= 2);
      current = {id: "recovered-current", status: "running", question, version: project.version, elapsed_seconds: 1};
      await run("poll()");
      assert.equal(run("state.job.id"), current.id); assert.equal(run("state.job.status"), "running");
      assert.equal(run("state.submission"), null); assert.equal(nodes.cancel.disabled, false);
      assert.equal(posts().length, 1); assert.equal(preserved(), before); assert.equal(deadlines().length, 0);
    }

    // A successful HTTP exchange is not an acknowledgement if it cannot name
    // a new safe job ID, including an undecodable successful response body.
    const invalidAcknowledgements = [null, {}, {id: null}, {id: 42}, {id: "../unsafe"}, {id: "unsafe\n"}, {id: "x".repeat(201)}, {id: "old-terminal"}];
    for (const acknowledgement of [...invalidAcknowledgements, "invalid-json"]) {
      setup("terminal"); const before = preserved();
      postReply = () => acknowledgement === "invalid-json" ? {ok: true, status: 200, json: async () => {throw SyntaxError("invalid JSON body");}} : reply(acknowledgement);
      await nodes["question-form"].listeners.submit({preventDefault() {}}); await settle(); unknownLocked();
      assert.equal(preserved(), before); assert.equal(posts().length, 1); assert.equal(deadlines().length, 0);
      await run("poll()"); unknownLocked(); assert.equal(posts().length, 1);
      current = {id: "recovered-malformed-reply", status: "running", question, version: project.version};
      await run("poll()"); assert.equal(run("state.job.id"), current.id); assert.equal(run("state.submission"), null);
      assert.equal(nodes.cancel.disabled, false); assert.equal(preserved(), before); assert.equal(deadlines().length, 0);
    }

    // A server-side failure may occur after acceptance. Only the explicit
    // client-error response takes the existing definite-rejection path.
    for (const httpStatus of [400, 409, 500, 503]) {
      setup("terminal"); const before = preserved();
      postReply = () => ({ok: false, status: httpStatus, json: async () => ({error: `HTTP ${httpStatus} response`})});
      await nodes["question-form"].listeners.submit({preventDefault() {}}); await settle();
      if (httpStatus >= 500) { unknownLocked(); await run("poll()"); unknownLocked(); }
      else {
        assert.equal(run("state.job.status"), "incomplete"); assert.equal(run("state.submission"), null);
        assert(nodes.answer.textContent.includes("本題未能送出")); assert.equal(nodes.ask.disabled, false);
      }
      assert.equal(posts().length, 1); assert.equal(preserved(), before); assert.equal(deadlines().length, 0);
    }

    for (const malformed of [null, {}, {id: null, status: "running"}, {id: "../unsafe", status: "running"}, {id: "unsafe\n", status: "running"},
      {id: "non-null-idle", status: "idle"}, {id: "known", status: "unsupported-status"}]) {
      setup(); current = {id: "known-before-malformed-status", status: "running", question, version: project.version};
      context.transportCurrent = current; run("renderJob(transportCurrent)"); const before = preserved();
      statusReply = () => reply(malformed); await run("poll()");
      assert.equal(run("state.job.status"), "unknown"); assert.equal(run("state.job.id"), current.id);
      for (const id of ["ask", "ask-project", "locate", "refresh"]) assert.equal(nodes[id].disabled, true);
      assert.equal(run("polling"), false); assert.equal(posts().length, 0); assert.equal(deadlines().length, 0);
      statusReply = () => reply(current); await run("poll()");
      assert.equal(run("state.job.status"), "running"); assert.equal(preserved(), before); assert.equal(deadlines().length, 0);
    }

    // The status mutex and request deadline must be released even if headers
    // arrive but the JSON response body never finishes.
    for (const stage of ["fetch", "json"]) {
      setup(); current = {id: "running-before-timeout", status: "running", question, version: project.version};
      context.transportCurrent = current; run("renderJob(transportCurrent)"); const before = preserved();
      statusReply = options => delayed(stage, options);
      const status = run("poll()"); await settle(); assert.equal(run("polling"), true);
      await run("poll()"); assert.equal(gets("/api/jobs/current").length, 1, "overlapping status polls remain serialized");
      expire(); await status; await settle();
      assert.equal(run("polling"), false); assert.equal(run("state.job.status"), "unknown");
      assert.equal(run("state.job.id"), current.id); assert.equal(deadlines().length, 0); assert(holds[0].aborted);
      statusReply = () => reply(current); await run("poll()");
      assert.equal(gets("/api/jobs/current").length, 2); assert.equal(run("polling"), false);
      assert.equal(run("state.job.status"), "running"); assert.equal(preserved(), before);
      assert.equal(posts().length, 0); assert.equal(deadlines().length, 0);
    }

    // Cancellation response loss is reconciled by GET, not by replaying the
    // cancellation or treating a client abort as observed process cleanup.
    for (const stage of ["fetch", "json"]) {
      setup(); current = {id: "cancelled-after-timeout", status: "running", question, version: project.version};
      context.transportCurrent = current; run("renderJob(transportCurrent)");
      const before = preserved();
      cancelReply = options => { current = {...current, status: "cancelled", gpu: "released", elapsed_seconds: 1}; return delayed(stage, options); };
      const cancellation = nodes.cancel.listeners.click(); await settle();
      assert.equal(nodes.cancel.disabled, true); expire(); await cancellation; await settle();
      assert.equal(posts().length, 1); assert.equal(posts()[0].route, "/api/jobs/cancelled-after-timeout/cancel");
      assert.deepEqual(posts()[0].body, {}); assert(holds[0].aborted); assert(gets("/api/jobs/current").length >= 1);
      assert.equal(run("state.job.status"), "cancelled"); assert.equal(run("polling"), false);
      assert.equal(nodes.cancel.hidden, true); assert.equal(nodes.ask.disabled, false);
      assert.equal(preserved(), before); assert.equal(deadlines().length, 0);
    }

    // Terminal status polling also awaits history. A stalled history body must
    // not hold the same mutex indefinitely or poison the valid current status.
    for (const stage of ["fetch", "json"]) {
      setup(); current = {id: "terminal-history-timeout", status: "cancelled", question, version: project.version, gpu: "released", elapsed_seconds: 1};
      historyReply = options => delayed(stage, options);
      const status = run("poll()"); await settle(); assert.equal(run("polling"), true);
      assert.equal(gets("/api/history").length, 1); expire(); await status; await settle();
      assert.equal(run("polling"), false); assert.equal(run("state.job.status"), "cancelled");
      assert(nodes["history-retry"].hidden === false); assert(holds[0].aborted); assert.equal(deadlines().length, 0);
      historyReply = () => reply({version: project.version, entries: [], evicted: 0});
      await nodes["history-retry"].listeners.click(); assert.equal(gets("/api/history").length, 2);
      assert.equal(run("state.historyError"), ""); assert.equal(nodes["history-retry"].hidden, true);
      assert.equal(posts().length, 0); assert.equal(nodes.question.value, question); assert.equal(deadlines().length, 0);
    }

    // The old terminal poll may still own the mutex inside history when a new
    // explicit POST succeeds. Its eventual timeout must not replace the new
    // status or suppress the follow-up GET scheduled by that older poll.
    setup("terminal"); const beforeNewQuestion = preserved();
    historyReply = options => delayed("json", options);
    const oldPoll = run("poll()"); await settle();
    assert.equal(run("polling"), true); assert.equal(nodes.ask.disabled, false);
    const oldGeneration = run("state.statusRequest");
    postReply = (options, body) => {
      current = {id: "new-while-history-held", status: "running", question: body.question, version: project.version};
      return reply({id: current.id});
    };
    await nodes["question-form"].listeners.submit({preventDefault() {}}); await settle();
    const newGeneration = run("state.statusRequest"); assert(newGeneration > oldGeneration);
    assert.equal(run("state.job.id"), current.id); assert.equal(run("state.job.status"), "running");
    assert.equal(run("polling"), true); assert.equal(gets("/api/jobs/current").length, 1);
    assert.equal(deadlines().length, 1, "only the old history deadline remains after the new POST completes");
    expire(); await oldPoll; await settle();
    assert.equal(run("polling"), false); assert.equal(run("state.statusRequest"), newGeneration);
    assert.equal(run("state.job.id"), current.id); assert.equal(run("state.job.status"), "running");
    assert.notEqual(run("state.historyLoadedJob"), "old-terminal", "stale completion cannot mark the old poll as the current generation");
    const retries = [...timers].filter(([, timer]) => timer.milliseconds === 1000);
    assert.equal(retries.length, 1, "finishing the old poll must schedule GET recovery for the active new job");
    const [retryId, retry] = retries[0]; timers.delete(retryId); await retry.callback();
    assert.equal(gets("/api/jobs/current").length, 2); assert.equal(posts().length, 1);
    assert.equal(run("state.job.id"), current.id); assert.equal(run("state.job.status"), "running");
    assert.equal(run("polling"), false); assert.equal(preserved(), beforeNewQuestion); assert.equal(deadlines().length, 0);
  } finally {
    timers.clear(); context.fetch = original.fetch; context.setTimeout = original.setTimeout; context.clearTimeout = original.clearTimeout;
  }
}
async function checkBrowseOnly() {
  const nodes = Object.fromEntries([...html.matchAll(/\bid="([^"]+)"/g)].map(match => [match[1], new Node()]));
  const requests = [], timers = new Map(), stored = [], settle = () => new Promise(resolve => setImmediate(resolve));
  const files = [{id: "0", path: "app.py", lines: 4}, {id: "1", path: "helper.py", lines: 2}];
  const project = {name: "No model assets", version: "browse-v1", files, excluded: [], browse_only: true, reader: null, experiments_enabled: false};
  const definition = {name: "render", kind: "function", start_line: 3, definition_line: 3, end_line: 4, stub: false};
  const sources = {"0": {path: "app.py", version: project.version, lines: ["from helper import normalize", "", "def render(value):", "    return normalize(value)"], outline: {status: "available", items: [definition]}},
    "1": {path: "helper.py", version: project.version, lines: ["def normalize(value):", "    return value"], outline: {status: "available", items: [{...definition, name: "normalize", start_line: 1, definition_line: 1, end_line: 2}]}}};
  const candidate = {binding_id: "B1", name: "normalize", kind: "from import", start_line: 1, end_line: 1, use_lines: [4], conditional: false};
  const traceback = 'Traceback (most recent call last):\n  File "app.py", line 4, in render\nValueError: <img src=x>';
  const reply = value => ({ok: true, status: 200, json: async () => value}); let nextTimer = 0;
  const context = {URLSearchParams, AbortController, TextEncoder, location: {hash: "#token=test", pathname: "/", search: ""}, history: {replaceState() {}},
    sessionStorage: {setItem: (key, value) => stored.push([key, value]), getItem: () => null},
    document: {getElementById: id => nodes[id], createElement: tag => new Node(tag), createDocumentFragment: () => new Node("fragment"), createTextNode: value => {const node = new Node("#text"); node.data = value; return node;}},
    setTimeout: (callback, milliseconds) => {const id = ++nextTimer; timers.set(id, {callback, milliseconds}); return id;}, clearTimeout: id => timers.delete(id),
    fetch: async (route, options) => {
      const body = options.body === undefined ? undefined : JSON.parse(options.body);
      requests.push({route, method: options.method, body});
      if (route === "/api/project") return reply(project);
      if (route.startsWith("/api/source?")) return reply(sources[new URLSearchParams(route.split("?")[1]).get("file")]);
      if (route === "/api/definitions?q=render&version=browse-v1&mode=keywords") return reply({version: project.version, mode: "keywords", terms: ["render"],
        matches: [{file: "0", path: "app.py", ...definition, matched_terms: [{term: "render", field: "name", line: null}], preview: {line: 3, text: sources["0"].lines[2]}}], truncated: false, total_matches: 1, unavailable_files: 0});
      if (route === "/api/context") return reply({...body, path: "app.py", status: "available", reason: null, scope: "same-file lexical scopes", semantics_verified: false,
        bindings: [{id: "B1", name: "normalize", classification: "module", scope: "render", scope_line: 3, use_lines: [4], reason: null}], candidates: [candidate]});
      if (route === "/api/import-source") return reply({...body, path: "app.py", scope: "snapshot import candidates", semantics_verified: false, status: "available", reason: null,
        routes: [{outcome: "declaration", reason: null, steps: [{file: "0", path: "app.py", name: "normalize", kind: "from import", start_line: 1, end_line: 1, conditional: false},
          {file: "1", path: "helper.py", name: "normalize", kind: "function", start_line: 1, end_line: 2, conditional: false}]}]});
      if (route === "/api/traceback") return reply({version: project.version, scope: "unverified_user_traceback", matched: 1,
        frames: [{reported_path: "app.py", file: "0", path: "app.py", function: "render", line: 4, reason: null}]});
      if (route === "/api/refresh") {assert.deepEqual(body, {}, "browse-only refresh cannot become a comparison request"); return reply({...project});}
      throw Error(`browse-only made a forbidden request: ${route}`);
    }};
  vm.createContext(context); const run = code => vm.runInContext(code, context);
  run(fs.readFileSync(path.join(root, "src/forge8/web/reading-note.js"), "utf8"));
  await run(app); await settle();
  assert.deepEqual(requests, [{route: "/api/project", method: "GET", body: undefined}], "actual bootstrap must not poll jobs, history, model or experiments");
  assert.equal(timers.size, 0); assert.equal(run("state.job.status"), "idle");
  assert.equal(nodes["reader-badge"].textContent, "純原碼閱讀 · 無 AI"); assert.equal(nodes["reader-title"].textContent, "閱讀工具");
  assert.equal(nodes["browse-only-notice"].hidden, false); assert.equal(nodes["reading-notice"].hidden, true);
  const browseNotice = html.slice(html.indexOf('id="browse-only-notice"'), html.indexOf('id="experiment-enabled"'));
  assert(browseNotice.includes("移除 --browse-only 與 --state 及其路徑後重新啟動一般 read"));
  assert(browseNotice.includes("仍會建立本機原碼快照"), "asset-free browsing must still disclose local source snapshot creation");
  for (const id of ["ai-reading-results", "ai-discovery-actions", "ask", "change-mode", "model-status", "experiment-enabled", "experiment-panel"]) assert.equal(nodes[id].hidden, true, `${id} is not part of pure browsing`);
  assert(nodes["question-label"].textContent.includes("traceback")); assert(nodes["question-editor-summary"].textContent.includes("traceback"));
  assert(!nodes["selection-limits"].textContent.includes("自動選材")); assert(!nodes["selection-limits"].textContent.includes("載入模型"));
  assert.equal(nodes.refresh.disabled, false); assert.equal(nodes.question.disabled, false);
  nodes.question.value = traceback; nodes.question.listeners.input();
  nodes["search-mode"].value = "keywords"; nodes["search-mode"].listeners.change(); nodes["search-query"].value = "render";
  await nodes["search-form"].listeners.submit({preventDefault() {}});
  const hit = nodes["search-results"].querySelectorAll("button")[0]; assert(hit.className.includes("keyword-hit"));
  await hit.listeners.click(); assert.equal(run("state.source.path"), "app.py"); assert.equal(run("state.anchor"), 3); assert.equal(run("state.end"), 4);
  assert.equal(run("state.focus.length"), 0); assert.equal(nodes["add-selection"].disabled, false);
  nodes["add-selection"].listeners.click(); assert.equal(run("state.focus.length"), 1); assert.equal(nodes.ask.disabled, true);
  const focus = run("JSON.stringify(state.focus)");
  await nodes.selections.querySelectorAll("button").find(button => button.textContent.includes("查看名稱來源")).listeners.click();
  assert.equal(nodes["source-context"].hidden, false); assert(nodes["context-results"].textContent.includes("normalize"));
  await nodes["context-results"].querySelectorAll("button").find(button => button.textContent === "追蹤匯入來源").listeners.click();
  const importButtons = nodes["context-results"].querySelectorAll("button").filter(button => button.textContent === "查看來源");
  assert.equal(importButtons.length, 2); await importButtons[1].listeners.click();
  assert.equal(run("state.source.path"), "helper.py"); assert.equal(run("JSON.stringify(state.focus)"), focus); assert.equal(nodes.question.value, traceback);
  assert.equal(nodes["traceback-locate"].disabled, false); await nodes["traceback-locate"].listeners.click();
  assert(nodes["traceback-summary"].textContent.includes("不執行紀錄中的呼叫"));
  await nodes["traceback-results"].querySelectorAll("button")[0].listeners.click();
  assert.equal(run("state.source.path"), "app.py"); assert.equal(run("state.anchor"), 4);
  assert.equal(run("JSON.stringify(state.focus)"), focus); assert.equal(nodes.question.value, traceback); assert.equal(timers.size, 0);
  assert.deepEqual(requests.filter(item => item.method === "POST").map(item => item.route), ["/api/context", "/api/import-source", "/api/traceback"], "only explicit static navigation POSTs are allowed");

  // UI guards complement the backend's disabled capabilities, rather than
  // relying solely on hidden buttons or an assumed lack of asset settings.
  const beforeHidden = requests.length;
  for (const id of ["ask", "ask-project", "locate", "cancel", "change-mode", "experiment-run", "experiment-baseline-pin", "experiment-baseline-clear"]) nodes[id].disabled = false;
  await nodes["question-form"].listeners.submit({preventDefault() {}}); await nodes["ask-project"].listeners.click(); await nodes.locate.listeners.click();
  await nodes.cancel.listeners.click(); await nodes["change-mode"].listeners.click(); await nodes["experiment-run"].listeners.click();
  await nodes["experiment-baseline-pin"].listeners.click(); await nodes["experiment-baseline-clear"].listeners.click();
  await run("poll()"); await run("loadHistory()"); await run("pollModel()"); await run("pollExperiment()");
  run("state.project.experiments_enabled=true; state.project.model_policy={mode:'resident'}; resetExperiment(); controls()");
  await run("pollModel()"); await run("pollExperiment()");
  assert.equal(requests.length, beforeHidden); assert.equal(timers.size, 0); assert.equal(nodes.ask.disabled, true);
  assert.equal(nodes["model-status"].hidden, true); assert.equal(nodes["experiment-panel"].hidden, true);
  run("state.project.experiments_enabled=false; delete state.project.model_policy");
  context.browseObservation = {test: "Existing read-only observation", python: "3.12", test_passed: true, calls: []};
  run("state.project.observation=browseObservation; renderObservation()");
  assert.equal(nodes.observation.hidden, false); assert(nodes["observation-status"].textContent.includes("Existing read-only observation"));
  assert.equal(requests.length, beforeHidden, "displaying an imported observation does not execute it");
  run("delete state.project.observation");

  await nodes.refresh.listeners.click(); await settle();
  assert.equal(requests.at(-1).route, "/api/refresh"); assert.equal(requests.at(-1).method, "POST");
  assert.equal(run("browseOnly()"), true); assert.equal(nodes["ai-reading-results"].hidden, true);
  assert.equal(run("state.focus.length"), 0); assert.equal(nodes["source-context"].hidden, true);
  assert.equal(nodes.question.value, traceback); assert.equal(nodes.ask.disabled, true); assert.equal(timers.size, 0);
  assert.deepEqual(stored, [["forge8.read.token.v1", "test"]], "traceback and source are not added to browser storage");

  // Rendering the ordinary project contract still restores its existing copy
  // and controls; browse-only is an explicit mode, never an asset-error fallback.
  const requestCount = requests.length;
  run("showProject({...state.project,browse_only:false,reader:'qwen35'},true); state.focus=[{file:'0',path:'app.py',start:3,end:4}]; renderSelections()");
  assert.equal(nodes["browse-only-notice"].hidden, true); assert.equal(nodes["reading-notice"].hidden, false);
  assert.equal(nodes["ai-reading-results"].hidden, false); assert.equal(nodes["ai-discovery-actions"].hidden, false);
  assert.equal(nodes.ask.hidden, false); assert.equal(nodes.ask.disabled, false); assert.equal(nodes["change-mode"].hidden, false);
  assert.equal(nodes["question-label"].textContent, "你想釐清什麼？"); assert.equal(nodes["question-editor-summary"].textContent, "調整選段與問題");
  assert.equal(nodes["reader-badge"].textContent, "Qwen3.5 · 試用"); assert(nodes.question.placeholder.includes("函式回傳什麼"));
  assert.equal(nodes.question.value, traceback); assert.equal(requests.length, requestCount); assert.equal(timers.size, 0);
}
async function checkScenario(mode) {
  const nodes = Object.fromEntries([...html.matchAll(/\bid="([^"]+)"/g)].map(match => [match[1], new Node()]));
  const stored = [], submissions = [];
  const question = " \tExplain this:\r\n```python\nif ready:\r\n\tvalue = '<img src=x onerror=alert(1)>'\n```\rNext paragraph.\t \n";
  let serverJob = answered("previous", "OLD_QUESTION_ANSWER"), statusOffline = mode === "temporary-offline";
  const context = {URLSearchParams, AbortController, TextEncoder, location: {hash: "#token=test", pathname: "/", search: ""}, history: {replaceState() {}},
    sessionStorage: {setItem: (key, value) => stored.push([key, value]), getItem: () => null},
    document: {getElementById: id => nodes[id], createElement: tag => new Node(tag), createDocumentFragment: () => new Node("fragment"), createTextNode: value => {const node = new Node("#text"); node.data = value; return node;}},
    setTimeout: () => 1, clearTimeout() {}, fetch: async (route, options) => {
      if (route === "/api/jobs") {
        submissions.push(JSON.parse(options.body));
        if (mode === "lost-accepted-reply") serverJob = {...answered("newly-created", "NEW_QUESTION_ANSWER"), question};
        if (mode === "rejected-reset") serverJob = {id: null, status: "idle"};
        if (mode === "lost-accepted-reply") throw Error("lost response");
        return {ok: false, status: 409, json: async () => ({error: "stale source version; refresh before asking"})};
      }
      assert.equal(route, "/api/jobs/current");
      if (statusOffline) throw Error("temporary network failure");
      return {ok: true, json: async () => serverJob};
    }};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(root, "src/forge8/web/reading-note.js"), "utf8"), context);
  vm.runInContext(app.slice(0, app.lastIndexOf("(async () => {")), context);
  for (const [reader, label] of [["qwen35", "Qwen3.5 · 試用"], ["gemma12b", "Gemma 4 12B · 實驗選用"], ...["<img onerror=alert(1)>", "constructor", "__proto__"].map(reader => [reader, "本機模型 · 試用"])]) {
    context.projectReader = reader;
    vm.runInContext('showProject({name:"test",version:"v1",files:[],excluded:[],reader:projectReader})', context);
    assert.equal(nodes["reader-badge"].textContent, label);
    assert.equal(nodes["reader-badge"].querySelectorAll("img").length, 0);
  }
  vm.runInContext('renderJob({id:"gemma",status:"running",phase:"Gemma4 12B preview reader: one bounded thinking pass; larger model may take longer"})', context);
  assert(nodes.phase.textContent.includes("Gemma 4 12B")); assert(!nodes.phase.textContent.includes("Qwen"));
  assert(!nodes.phase.textContent.includes("bounded thinking"), "the Gemma progress phase must be readable Chinese");
  const deploymentError = "Reader gemma12b: missing gemma-4-12b-it-qat-q4_0.gguf\nVerify: '<img src=x>' --manifest 'config/models/gemma4_12b_qat_q4.json' [E1:L1]";
  context.installFailure = {id: "missing-model", status: "incomplete", gpu: "not_acquired", question: "Preserved question",
    result: {status: "integrity_failed", ok: false, error: deploymentError, outcome: null}};
  vm.runInContext("renderJob(installFailure)", context);
  assert(nodes.phase.textContent.includes("模型或執行環境"));
  assert(!nodes.phase.textContent.includes("調整問題或選段"), "missing deployment files cannot be fixed by rephrasing the question");
  const diagnostic = nodes.answer.children.find(node => node.className === "answer-prose");
  assert.equal(diagnostic.textContent, deploymentError); assert.equal(diagnostic.children.length, 0);
  assert.equal(nodes.answer.querySelectorAll("img").length, 0); assert.equal(nodes.answer.querySelectorAll("button").length, 0);
  assert.equal(nodes["question-editor"].open, true); assert.equal(submissions.length, 0);
  vm.runInContext('renderJob({...installFailure,id:"ordinary-failure",result:{status:"stalled",error:"not enough source"}})', context);
  assert(nodes.phase.textContent.includes("調整問題或選段"), "ordinary reading failures retain their existing guidance");
  vm.runInContext('showProject({name:"test",version:"v1",files:[{id:"0",path:"a.py",lines:3}],excluded:[]}); state.focus=[{file:"0",path:"a.py",start:1,end:2}];', context);
  assert.equal(nodes["question-editor"].open, true);
  context.previous = serverJob; vm.runInContext("renderJob(previous)", context);
  assert.equal(nodes["question-editor"].open, false, "restored successful answer must release editor space");
  assert(nodes.answer.textContent.includes("OLD_QUESTION_ANSWER"));
  assert(nodes.answer.textContent.includes("PREVIOUS_QUESTION"), "answer must retain its own question on reload");
  assert.equal(nodes.question.value, "", "restored answer must not overwrite editor");
  delete context.previous.question; context.previous.result.question = "RESULT_QUESTION";
  vm.runInContext('state.rendered = ""; renderJob(previous)', context);
  assert(nodes.answer.textContent.includes("RESULT_QUESTION"), "older jobs can recover the authoritative result question");
  nodes["question-editor"].open = true; vm.runInContext("renderJob(previous)", context);
  assert.equal(nodes["question-editor"].open, true, "unchanged polling must not close an explicitly reopened editor");
  nodes.question.value = question; nodes.question.listeners.input();
  await nodes["question-form"].listeners.submit({preventDefault() {}});
  await new Promise(resolve => setImmediate(resolve));
  if (statusOffline) {
    assert.equal(vm.runInContext("state.job.status", context), "unknown");
    statusOffline = false; await vm.runInContext("poll()", context);
  }
  assert.equal(submissions.length, 1, "only the user's submit action may start a question");
  assert.equal(submissions[0].question, question, "POST preserves every textarea character, including code indentation and boundary whitespace");
  assert.equal(nodes.question.value, question);
  assert.equal(vm.runInContext("state.focus.length", context), 1);
  assert(!nodes.answer.textContent.includes("OLD_QUESTION_ANSWER"), "rejected question redisplayed previous answer as current");
  if (mode === "lost-accepted-reply") {
    assert.equal(nodes["question-editor"].open, false);
    assert(nodes.answer.textContent.includes("NEW_QUESTION_ANSWER"));
    assert.equal(vm.runInContext("state.job.status", context), "answered");
    const details = nodes.answer.querySelectorAll("details").find(node => node.className === "answer-question");
    assert.equal(details.children[1].textContent, question, "the restored answer retains its exact original multiline question");
    assert.equal(nodes.answer.querySelectorAll("img").length, 0, "question HTML remains literal text");
  } else {
    assert.equal(nodes["question-editor"].open, true, "failed submission must preserve visible editing and the user's draft");
    assert.equal(vm.runInContext("state.job.status", context), "incomplete");
    assert(nodes.answer.textContent.includes("stale source version"));
    assert.equal(nodes.ask.disabled, false);
  }
  if (mode !== "rejected") return;
  let sourceCalls = 0;
  context.fetch = async route => {
    sourceCalls++; assert(route.startsWith("/api/source?"));
    return {ok: true, json: async () => ({path: "a.py", version: "v1", lines: ["<img onerror=alert(1)>", "second line"]})};
  };
  context.reference = {path: "a.py", start_line: 1, end_line: 2};
  context.citationJob = {version: "v1", files: [{id: "0", path: "a.py"}]};
  const wrapper = vm.runInContext('citationButton(reference, citationJob, "citation")', context);
  const button = wrapper.children[0], preview = wrapper.children[1];
  await button.listeners.click();
  assert.equal(preview.hidden, false); assert(preview.textContent.includes("1│ <img onerror=alert(1)>"));
  assert.equal(vm.runInContext("state.source", context), null, "expanding a citation must not navigate away from the sentence");
  await button.listeners.click(); assert.equal(preview.hidden, true);
  await button.listeners.click(); assert.equal(sourceCalls, 1, "reopening an excerpt reuses only that citation's versioned text");
  assert.equal(preview.querySelectorAll("button").find(node => node.className.includes("source-context")).disabled, true, "whole-file citation has no extra context to expand");
  await preview.querySelectorAll("button").find(node => node.className.includes("source-open")).listeners.click();
  assert.equal(vm.runInContext("state.source.path", context), "a.py", "explicit full-source navigation remains available");
  context.citationJob = {version: "other-version", files: [{id: "0", path: "a.py"}]};
  const mismatch = vm.runInContext('citationButton(reference, citationJob, "citation")', context);
  await mismatch.children[0].listeners.click();
  assert(mismatch.children[1].textContent.includes("版本或範圍不一致"));
  assert(!mismatch.children[1].textContent.includes("<img"), "mismatched source bytes cannot be presented as evidence");
  const emit = value => {context.update = value; vm.runInContext("renderJob(update)", context);};
  emit({id: "verifying", status: "running", phase: "hashing the pinned local runtime and model", elapsed_seconds: 73});
  assert(nodes.phase.textContent.includes("SHA-256"));
  assert(nodes.phase.textContent.includes("本階段尚未啟動模型"));
  assert.equal(nodes.cancel.hidden, false); assert.equal(nodes.cancel.disabled, false);
  assert.equal(nodes.elapsed.textContent, "1:13", "verification retains honest elapsed time and cancellation");
  const draftPreview = '**UNVERIFIED_DRAFT** <img onerror=alert(1)> [E1:L1-L2]';
  const running = {id: "streaming", status: "running", preview: draftPreview, question: "DRAFT_QUESTION", reasoning_content: "NEVER_DISPLAY_THOUGHTS"};
  emit(running);
  assert.equal(nodes["question-editor"].open, true, "streaming cannot collapse the editor or replace its question");
  assert(nodes.answer.textContent.includes("生成中草稿 · 尚未完成／引用未檢查（最多顯示 12,000 字元）"));
  assert(nodes.answer.textContent.includes("DRAFT_QUESTION"));
  assert(nodes.answer.textContent.includes(draftPreview)); assert(!nodes.answer.textContent.includes("NEVER_DISPLAY_THOUGHTS"));
  assert.equal(nodes.answer.querySelectorAll("button").length, 0, "draft citations must not be interactive");
  const body = nodes.answer.children.at(-1), text = body.children[0];
  emit({...running, preview: draftPreview + "\n第二段"});
  assert.equal(nodes.answer.children.at(-1), body); assert.equal(body.children[0], text, "preview growth reuses the existing text node");
  assert.equal(text.data, draftPreview + "\n第二段"); const calls = text.appendCalls;
  emit({...running, preview: draftPreview + "\n第二段"}); assert.equal(text.appendCalls, calls, "unchanged polls do not mutate preview DOM");
  emit({...running, preview: "REPLACEMENT"}); assert.equal(text.data, "REPLACEMENT", "non-prefix updates cannot concatenate unrelated text");
  emit({...running, id: "new-job", preview: "NEW_DRAFT"}); assert.notEqual(nodes.answer.children.at(-1), body);
  assert(!nodes.answer.textContent.includes("REPLACEMENT"));
  for (const status of ["cancelling", "unknown", "incomplete", "cancelled", "answered", "idle"]) {
    emit({...running, id: status});
    emit({...answered(status, "ACCEPTED_FINAL"), status, preview: draftPreview});
    assert(!nodes.answer.textContent.includes("UNVERIFIED_DRAFT"), `draft must be cleared for ${status}`);
    assert.equal(vm.runInContext("draft", context), null);
    if (status === "answered") assert(nodes.answer.textContent.includes("ACCEPTED_FINAL"));
    if (["incomplete", "cancelled"].includes(status)) assert.equal(nodes["question-editor"].open, true);
  }
  await checkRejectedPreview(context, nodes);
  assert.deepEqual(stored, [["forge8.read.token.v1", "test"]], "drafts, thoughts and rejected excerpts must not enter session storage");
  await checkReferenceFormats(context, nodes);
  await checkAnswerFormatting(context, nodes);
  await checkStructuredReadingDelivery(context, nodes);
  await checkOutline(context, nodes);
  await checkDefinitionSearch(context, nodes);
  await checkObservedPaths(context, nodes);
  await checkReadingTrail(context, nodes);
  await checkDiscovery(context, nodes);
  await checkProjectQuestion(context, nodes);
  await checkUnverifiedProse(context, nodes);
  checkPackedSelections(context, nodes);
  if (mode === "rejected") { await checkTraceback(context, nodes); await checkDefinitionExpansion(context, nodes); await checkReadingHistory(context, nodes); await checkArchivedSourceNavigation(context, nodes); await checkInlineExperiments(context, nodes); await checkGeneratorExperiments(context, nodes); await checkExperimentBaseline(context, nodes); await checkPairedExperiments(context, nodes); await checkPairedInputSearch(context, nodes); await checkExperimentModules(context, nodes); await checkExperimentCallInputs(context, nodes); await checkSourceContinuation(context, nodes); await checkInsufficientRecovery(context, nodes); await checkResidentModel(context, nodes); await checkQuestionTransport(context, nodes); checkProgressiveDisclosure(context, nodes); }
  assert.deepEqual(stored, [["forge8.read.token.v1", "test"]], "pasted logs must not enter browser storage");
}
(async () => {
  for (const mode of ["rejected", "rejected-reset", "lost-accepted-reply", "temporary-offline"]) await checkScenario(mode);
  await checkBrowseOnly();
  process.stdout.write("PASS recovery/citation provenance, literal streaming draft, cached definitions, bounded reading return, explicit source discovery and unverified prose: gated host-source links, preserved drafts, no automatic model follow-up, failure and stale clearing.\n");
})().catch(error => { process.stderr.write(error.stack + "\n"); process.exitCode = 1; });
