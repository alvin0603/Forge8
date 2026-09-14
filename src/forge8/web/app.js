"use strict";
const $ = id => document.getElementById(id);
const suppliedToken = new URLSearchParams(location.hash.slice(1)).get("token");
let token = suppliedToken || "";
history.replaceState(null, "", location.pathname + location.search);
try {
  if (suppliedToken !== null) sessionStorage.setItem("forge8.read.token.v1", token);
  else token = sessionStorage.getItem("forge8.read.token.v1") || "";
} catch { /* Storage-blocking browsers can still use the supplied private URL. */ }
const state = { project: null, source: null, focus: [], anchor: null, end: null, extending: false, rangeHint: "", page: 0, readingTrail: [], job: null, submission: null, pending: false, refreshing: false, pairPending: false, pairRequest: 0, context: null, contextRequest: 0, rendered: "", sourceRequest: 0, searchRequest: 0, statusRequest: 0, tracebackRequest: 0, tracebackPending: false, history: [], historyEvicted: 0, historyRequest: 0, historyLoadedJob: null, historyError: "", historyBoundary: "", selectedHistory: null };
state.experiment = null;
state.continuation = null;
state.continuationNotice = "";
state.sourceRecovery = null;
state.readingExport = null;
state.readingExportRevision = 0;
state.readingExportNotice = "";
state.questionRevision = 0;
state.callSelectionRevision = 0;
state.model = null;
state.modelUnknown = false;
state.modelError = "";
state.modelRequest = 0;
state.modelReleasePending = false;
state.modelReceivedAt = 0;
const pageSize = 200;
const definitionKinds = {class: "類別", function: "函式", "async function": "非同步函式"};
const contextKinds = {...definitionKinds, assignment: "賦值", "annotated assignment": "註記賦值", import: "匯入宣告", "from import": "from 匯入宣告",
  parameter: "參數宣告", "loop target": "迴圈目標", "with target": "with 目標", "exception target": "例外名稱",
  "pattern target": "模式比對名稱", "named assignment": "命名運算式賦值", "augmented assignment": "增量賦值"};
const contextClassifications = {parameter: "函式參數", local: "區域名稱", free: "外層作用域名稱", module: "模組名稱查找",
  class: "類別本體名稱查找", annotation: "註記作用域", unresolved: "來源未確定"};
const contextReasons = {scope_mapping: "無法可靠對應這次名稱讀取的作用域；不以同名宣告代替。",
  implicit_class_cell: "__class__ 涉及 Python 隱含的類別儲存格；不改指向外層同名變數。",
  annotation_scope: "註記的求值作用域未能確定；不推測執行時的綁定。", class_namespace_lookup: "類別本體查找仍取決於當時的命名空間；不選定執行時的值。",
  unbinding: "此作用域包含名稱解除綁定；不代表使用時仍有值。", annotation_only: "只有型別註記不會建立執行時的值；名稱可能尚未賦值。",
  unsupported_binding: "遇到尚未支援的名稱綁定形式；不推測來源。"};
const projectContextReasons = {ambiguous: "多個同名宣告", conditional: "條件式宣告", limit: "補充數量／行數上限", unavailable: "無可用靜態宣告清單", capacity: "原碼預算不足"};
const readerLabels = {qwen35: "Qwen3.5 · 試用", gemma12b: "Gemma 4 12B · 實驗選用"};
let pollTimer, experimentTimer, polling = false, draft = null, definitionTarget = null, selectionTarget = null, historyRendered = "", continuationRendered = "";
let modelTimer, modelCountdownTimer, modelReceiptsRendered = "";
const active = () => state.pending || ["running", "cancelling", "unknown"].includes(state.job?.status);
const browseOnly = () => state.project?.browse_only === true;
const residentEnabled = () => !browseOnly() && state.project?.model_policy?.mode === "resident";
const modelMutationBlocked = () => residentEnabled() && (!state.model || state.modelUnknown || state.modelReleasePending ||
  ["checking", "loading", "busy", "releasing", "cleanup_unknown"].includes(state.model.state));
const experimentsEnabled = () => !browseOnly() && state.project?.experiments_enabled === true;
const pairedExperiment = target => target?.mode === "head_current";
const experimentBusy = () => Boolean(state.experiment && (state.experiment.submitting || state.experiment.unknown || state.experiment.job?.cleanup_unknown === true || ["running", "cancelling"].includes(state.experiment.job?.status)));
const experimentBaselineBusy = () => Boolean(state.experiment && (state.experiment.baselinePending || state.experiment.baselineUnknown || state.experiment.inputComparison?.settling));
const terminal = job => ["answered", "located", "incomplete", "cancelled"].includes(job?.status);
const answerJob = () => state.selectedHistory === null ? state.job : state.history.find(job => job.id === state.selectedHistory);
const element = (tag, text, className = "") => { const node = document.createElement(tag); node.textContent = text; node.className = className; return node; };
const showError = message => { $("error").textContent = message || ""; $("error").hidden = !message; };
const inChanges = () => Boolean(state.project?.comparison);
const questionLimit = () => inChanges() ? 1300 : 2000;
const changeRoles = ["before", "after", "caller"];
const changeRoleLabels = {before: "① HEAD 原碼", after: "② 目前對應原碼", caller: "③ 目前呼叫端（固定）"};
function sourceLabel(path, comparison = state.project?.comparison) {
  if (!comparison) return path;
  if (path.startsWith("before/")) return `HEAD ${comparison.head.slice(0, 12)} · ${path.slice(7)}`;
  if (path.startsWith("after/")) return `目前已儲存 · ${path.slice(6)}`;
  return `${path} · 比較資料，非程式碼`;
}
function comparisonSpan(path, side, span, project = state.project) {
  const file = project?.files.find(file => file.path === `${side}/${path}`);
  if (!file || !span || !Number.isSafeInteger(span.start_line) || !Number.isSafeInteger(span.end_line) ||
      span.start_line < 1 || span.end_line < span.start_line || span.end_line > file.lines) return null;
  return {file: file.id, path: file.path, start: span.start_line, end: span.end_line, role: side};
}
function comparisonIssue() {
  if (!inChanges()) return "";
  for (const role of changeRoles) if (!state.focus.some(item => item.role === role)) return `請加入${changeRoleLabels[role]}。可從下方檔案清單與字面搜尋選取，不啟動模型。`;
  if (state.focus.length !== 3 || state.focus.some((item, index) => item.role !== changeRoles[index])) return "三段角色不完整；請重新確認 HEAD、目前原碼與固定呼叫端。";
  const [before, after, caller] = state.focus;
  if (!before.path.startsWith("before/") || !after.path.startsWith("after/") || !caller.path.startsWith("after/") || before.path.slice(7) !== after.path.slice(6)) return "前兩段必須是同一路徑的兩個版本；呼叫端必須選自目前版本。其他選段已保留，請調整對應段。";
  const entry = state.project.comparison.catalogue.files.find(item => item.path === before.path.slice(7));
  if (!entry || entry.status !== "modified" || entry.line_endings_only) return "前兩段必須選自有內容修改的同一檔案；未修改或只有換行差異的檔案不送出比較。原有選段已保留。";
  if (new Set(state.focus.map(item => `${item.file}:${item.start}:${item.end}`)).size !== 3) return "三段不能完全重複同一檔案的同一範圍；請另選固定呼叫端的位置，或調整選段。";
  return "";
}
function validComparisonJob(job) {
  if (!inChanges() && !job.comparison) return true;
  return job.version === state.project?.version && Boolean(job.comparison) &&
    job.comparison.head === state.project?.comparison?.head &&
    job.comparison.original_snapshot_sha256 === state.project?.comparison?.original_snapshot_sha256;
}
function selectedCharacters(source, start, end) {
  return [...source.lines.slice(start - 1, end).join("\n")].length;
}
function packSourceRange(lines, start, end, slots) {
  const reject = note => ({chunks: [], note});
  if (!Array.isArray(lines) || ![start, end, slots].every(Number.isSafeInteger) || start < 1 || end < start || end > lines.length || slots < 0 || slots > 3)
    return reject("選取座標無效；請重新選取原碼行。");
  const capacity = () => reject(`完整範圍放不進剩餘 ${slots} 段（每段 80 行／含行號 4,000 字元）；請移除其他選段或縮小範圍，原有選段未變動。`);
  if (!slots || end - start + 1 > slots * 80) return capacity();
  const chunks = []; let first = start, characters = 0;
  for (let line = start; line <= end; line++) {
    if (typeof lines[line - 1] !== "string") return reject("原碼行不可用；請重新開啟檔案。");
    // WorkspaceTools.read_text renders each source line as f"{number:>6}|{text}".
    const length = [...lines[line - 1]].length + Math.max(6, String(line).length) + 1;
    if (length > 4000) return reject(`L${line} 單行含行號超過 4,000 字元，無法分段；請選取其他範圍，原有選段未變動。`);
    if (line > first && (line - first >= 80 || characters + 1 + length > 4000)) {
      chunks.push({start: first, end: line - 1});
      if (chunks.length >= slots) return capacity();
      first = line; characters = 0;
    }
    characters += length + (line > first ? 1 : 0);
  }
  chunks.push({start: first, end});
  return {chunks, note: chunks.length > 1 ? `完整加入 ${chunks.length} 段，不省略任何行；提問後、模型載入前仍會檢查共用原碼預算。` : ""};
}
async function selectComparisonPair(path, before, after) {
  if (active() || state.refreshing || state.pairPending || !inChanges()) return;
  const project = state.project, selections = [comparisonSpan(path, "before", before), comparisonSpan(path, "after", after)];
  if (selections.some(item => !item || item.end - item.start >= 80)) { showError("這組原碼缺少一側或超過 80 行；未更動選段，請手動選取兩側上下文。"); return; }
  const request = ++state.pairRequest;
  state.pairPending = true; controls(); showError(""); $("changes-feedback").textContent = "正在檢查兩側選段；尚未更動原有三段，不啟動模型…";
  try {
    const sources = await Promise.all(selections.map(item => api(`/api/source?${new URLSearchParams({file: item.file, version: project.version})}`)));
    if (request !== state.pairRequest || state.project !== project || state.refreshing) return;
    for (let index = 0; index < selections.length; index++) {
      const item = selections[index], source = sources[index];
      if (source.version !== project.version || source.path !== item.path || !Array.isArray(source.lines) || source.lines.some(line => typeof line !== "string") || item.end > source.lines.length ||
          selectedCharacters(source, item.start, item.end) > 4000) throw new Error("兩側來源版本、範圍或每段 4,000 字元限制不符；原有選段全部保留，請手動縮小範圍。");
    }
    state.focus = selections; $("change-slot").value = "caller";
    $("question-editor").open = true; renderSelections();
    $("changes-feedback").textContent = "已用這組 HEAD／目前原碼取代全部選段。接著選一段目前呼叫端；需要的常數或設定請自行納入前兩段。";
  } catch (error) { if (request === state.pairRequest && state.project === project) { showError(error.message); $("changes-feedback").textContent = "配對未完成，原有選段已保留；沒有啟動模型。"; } }
  finally { if (request === state.pairRequest) { state.pairPending = false; controls(); } }
}
function renderChanges() {
  const comparison = state.project?.comparison, project = state.project;
  $("changes-panel").hidden = !comparison; $("changes-list").replaceChildren(); $("changes-feedback").textContent = "";
  $("change-slot-control").hidden = $("change-selection-guide").hidden = !comparison;
  $("selection-limits").hidden = Boolean(comparison);
  for (const id of ["locate", "locate-note", "ask-project", "ask-project-note", "traceback-locate", "traceback-summary", "traceback-results"]) $(id).hidden = Boolean(comparison);
  $("change-mode").textContent = comparison ? "返回一般閱讀" : "理解這次修改";
  $("change-mode").setAttribute("aria-pressed", String(Boolean(comparison)));
  $("question").maxLength = questionLimit();
  $("question").placeholder = comparison ? "例如：哪種輸入會讓修改前後不同？把第三段呼叫端保持相同時會發生什麼？缺少什麼資訊？" : "例如：這個函式回傳什麼？參數省略或輸入無效時，又會發生什麼？";
  $("ask").textContent = comparison ? "比較行為與這段呼叫端 ↗" : "解釋選取的程式碼 ↗";
  if (!comparison) return;
  const catalogue = comparison.catalogue;
  $("changes-summary").textContent = `HEAD ${comparison.head} · ${catalogue.files.length} 個變動路徑${catalogue.detail_limited ? " · 部分細節受限" : ""}。差異與同名配對只供導航，尚未驗證行為。`;
  if (!catalogue.files.length) { $("changes-list").append(element("p", "納入比較的原碼沒有差異；不代表被排除的檔案或未儲存內容沒有改變。可返回一般閱讀。", "muted")); return; }
  const statuses = {modified: "已修改", added: "相對 HEAD 新增", absent: "目前快照中缺少（非刪除判定）"};
  const navigate = (path, side, span, label) => {
    const file = project.files.find(file => file.path === `${side}/${path}`), selected = span ? comparisonSpan(path, side, span, project) : null;
    const button = element("button", label, "quiet"); button.type = "button";
    button.dataset.changeBrowse = "true"; button.dataset.available = String(Boolean(file && (!span || selected)));
    button.addEventListener("click", () => {
      if (button.disabled || state.project !== project || state.pending || state.refreshing || !file) return;
      return openFile(file, project.version, selected?.start ?? null, selected?.end ?? null);
    });
    return button;
  };
  const pair = (parent, path, before, after, label) => {
    const selections = [comparisonSpan(path, "before", before, project), comparisonSpan(path, "after", after, project)];
    const available = selections.every(item => item && item.end - item.start < 80);
    const button = element("button", label, "change-pair"); button.type = "button";
    button.dataset.changePair = "true"; button.dataset.available = String(available);
    button.title = "明確取代全部選段，包含先前的呼叫端；不修改原碼或啟動模型。";
    button.addEventListener("click", () => { if (state.project === project && !button.disabled) return selectComparisonPair(path, before, after); });
    parent.append(button);
    if (!available) parent.append(element("p", "缺少一側或超過每段 80 行，不能直接成對加入；可查看原碼後手動選取上下文。", "muted"));
  };
  for (const [index, entry] of catalogue.files.entries()) {
    const card = element("details", "", "change-file"); card.open = index === 0;
    card.append(element("summary", `${statuses[entry.status] || "狀態不明"} · ${entry.path}`));
    const navigation = element("div", "", "change-navigation");
    navigation.append(navigate(entry.path, "before", null, "看 HEAD 檔案"), navigate(entry.path, "after", null, "看目前檔案")); card.append(navigation);
    const detail = entry.detail;
    card.append(element("p", entry.line_endings_only ? "只有換行差異；沒有把相同行內容標成行為改變。" :
      detail.hunks === "exact" ? "精確行差異（不是行為判定）。" : detail.hunks === "coarse" ? "粗略包圍範圍，內含未改行；為限制比對成本，沒有計算精確差異。" : "差異細節超過目錄上限；此路徑仍完整列出，請查看兩側原碼。", "muted"));
    if (detail.units !== "available") card.append(element("p", "無可用的唯一完整定義配對（語言、解析、重複定義或容量限制）。請用行差異導航或手動選段；不推測名稱綁定。", "muted"));
    for (const unit of entry.units) {
      const row = element("div", "", "change-unit"); row.append(element("strong", `${definitionKinds[unit.kind] || unit.kind} ${unit.name}`));
      const links = element("div", "", "change-navigation");
      if (unit.before) links.append(navigate(entry.path, "before", unit.before, `HEAD L${unit.before.start_line}–${unit.before.end_line}`));
      if (unit.after) links.append(navigate(entry.path, "after", unit.after, `目前 L${unit.after.start_line}–${unit.after.end_line}`));
      row.append(links); pair(row, entry.path, unit.before, unit.after, "以這組定義取代全部選段"); card.append(row);
    }
    if (entry.hunks.length) {
      const hunks = element("details", "", "change-hunks"); hunks.open = !entry.units.length;
      hunks.append(element("summary", `${entry.hunks.length} 組行差異 · 可手動擴展上下文`));
      for (const hunk of entry.hunks) {
        const row = element("div", "", "change-unit"), spans = {};
        for (const side of ["before", "after"]) {
          const position = hunk[side], label = side === "before" ? "HEAD" : "目前";
          spans[side] = position.count ? {start_line: position.start + 1, end_line: position.start + position.count} : null;
          row.append(spans[side] ? navigate(entry.path, side, spans[side], `${label} L${spans[side].start_line}–${spans[side].end_line}`) :
            element("p", `${label}：${position.start ? `第 ${position.start} 行之後` : "檔案開頭"}的空邊界，沒有可引用行；請手動選周邊原碼。`, "muted"));
        }
        pair(row, entry.path, spans.before, spans.after, "以這組行範圍取代全部選段"); hunks.append(row);
      }
      card.append(hunks);
    }
    $("changes-list").append(card);
  }
}
async function api(path, body, signal) {
  if (!token) throw new Error("缺少工作階段金鑰。請從終端機重新開啟 forge8 read 提供的完整網址。");
  const headers = { Authorization: `Bearer ${token}` };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { method: body === undefined ? "GET" : "POST", headers, body: body === undefined ? undefined : JSON.stringify(body), cache: "no-store", credentials: "omit", redirect: "error", ...(signal ? {signal} : {}) });
  let data;
  try { data = await response.json(); } catch { throw new Error("本機服務沒有回傳有效資料，請檢查終端機的服務狀態。"); }
  if (!response.ok) {
    const error = new Error(response.status === 401 || response.status === 403 ? "無法授權此工作階段，請使用終端機提供的原始網址重新開啟。" : String(data.error || data.message || `本機請求失敗（${response.status}）`));
    error.httpStatus = response.status;
    throw error;
  }
  return data;
}
async function jobApi(path, body) {
  // This bounds the control exchange, including the JSON body, not generation.
  // Aborting a request is NOT evidence that a mutation was rejected server-side.
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 10000);
  try { return await api(path, body, controller.signal); }
  catch (error) {
    if (controller.signal.aborted) throw new Error("本機工作請求在 10 秒內未收到完整回覆；狀態尚未確認，不代表模型已停止。");
    throw error;
  } finally { clearTimeout(timer); }
}
const validJobId = value => typeof value === "string" && value.length >= 1 && value.length <= 200 && !/[^A-Za-z0-9_-]/.test(value);
function controls() {
  const busy = active() || experimentBusy() || state.refreshing || state.pairPending, comparison = inChanges();
  const modelBlocked = modelMutationBlocked();
  const range = state.anchor === null ? 0 : Math.abs(state.end - state.anchor) + 1;
  const tooLong = $("question").value.length > questionLimit();
  $("ask").disabled = browseOnly() || busy || modelBlocked || !state.project || (!state.continuation && !state.focus.length) || !$("question").value.trim() || tooLong || Boolean(comparisonIssue());
  $("ask").textContent = state.continuation ? "用前題原碼回答新問題 ↗" : "解釋選取的程式碼 ↗";
  $("locate").disabled = browseOnly() || busy || modelBlocked || comparison || Boolean(state.continuation) || !state.project || !$("question").value.trim() || tooLong;
  $("ask-project").disabled = $("locate").disabled;
  $("traceback-locate").disabled = busy || comparison || !state.project || !$("question").value.trim() || state.tracebackPending;
  $("question").disabled = busy;
  $("refresh").disabled = busy || modelBlocked || !state.project;
  $("change-mode").disabled = browseOnly() || $("refresh").disabled;
  const role = $("change-slot").value, side = role === "before" ? "before/" : "after/";
  const characters = state.source && range ? selectedCharacters(state.source, Math.min(state.anchor, state.end), Math.max(state.anchor, state.end)) : 0;
  const packing = !comparison && !browseOnly() && range ? packSourceRange(state.source?.lines, Math.min(state.anchor, state.end), Math.max(state.anchor, state.end), 3 - state.focus.length) : null;
  const rangeBlocked = packing ? !packing.chunks.length : range > 80 || characters > 4000 || (!comparison && state.focus.length >= 3);
  $("add-selection").disabled = busy || !range || rangeBlocked || state.source?.version !== state.project?.version || (comparison && (!changeRoles.includes(role) || !state.source?.path.startsWith(side)));
  $("add-selection").textContent = comparison ? `加入／取代${changeRoleLabels[role] || "選段"}` : packing?.chunks.length > 1 ? `完整加入 ${packing.chunks.length} 段` : "加入選段";
  $("add-selection").title = packing?.note || "";
  selectionTarget = $("add-selection").disabled ? null : {project: state.project, source: state.source, version: state.source.version,
    anchor: state.anchor, end: state.end, focus: [...state.focus], role, chunks: packing?.chunks || [{start: Math.min(state.anchor, state.end), end: Math.max(state.anchor, state.end)}]};
  $("change-slot").disabled = busy; $("clear-change-selections").disabled = busy || !state.focus.length;
  $("change-selection-status").textContent = comparisonIssue() || "三段已就緒。請確認常數、設定與呼叫上下文已選入；缺少的資訊應保持未知，請檢查模型是否做了未證實的推論。";
  $("changes-list").querySelectorAll("button").forEach(button => { button.disabled = button.dataset.available !== "true" || (button.dataset.changePair ? busy : state.pending || state.refreshing); });
  $("extend-selection").disabled = busy || state.anchor === null || state.source?.version !== state.project?.version;
  $("find-selection").disabled = state.pending || state.refreshing || !state.source || state.source.version !== state.project?.version;
  const expansion = containingDefinition();
  definitionTarget = expansion.target ? {source: state.source, version: state.source.version, anchor: state.anchor, end: state.end, target: expansion.target} : null;
  $("expand-definition").disabled = busy || !definitionTarget || !expansion.available;
  $("definition-target").textContent = expansion.note;
  $("expand-definition").title = expansion.note;
  const previous = state.readingTrail.at(-1);
  $("source-back").disabled = state.pending || state.refreshing || !previous;
  $("source-back").title = previous ? `返回 ${previous.file.path} · 第 ${previous.page + 1} 頁${previous.anchor === null ? "" : ` · 選取 L${Math.min(previous.anchor, previous.end)}–L${Math.max(previous.anchor, previous.end)}`}` : "尚無可返回的閱讀位置";
  if ($("extend-selection").disabled) state.extending = false;
  $("extend-selection").setAttribute("aria-pressed", String(state.extending));
  $("selection-hint").textContent = state.extending ? `從 L${state.anchor} 延伸：請點終點行號（可跨頁）；再按按鈕可取消` : "點行號選起點；Shift＋點擊或按「延伸選取」選終點";
  $("range-label").textContent = range ? `L${Math.min(state.anchor, state.end)}–L${Math.max(state.anchor, state.end)} · ${range} 行${packing ? packing.note ? ` · ${packing.note}` : "" : range > 80 ? "（請縮小至 80 行內）" : characters > 4000 ? "（超過 4,000 字元；請縮小）" : ""}${comparison && !state.source?.path.startsWith(side) ? " · 請改選此角色的版本" : ""}` : state.rangeHint || "尚未選取行";
  $("question-count").textContent = `${$("question").value.length} / ${questionLimit()}${tooLong ? " · 原稿保留，請縮短後再送出" : ""}`;
  $("cancel").hidden = !active();
  $("cancel").disabled = !state.job?.id || state.pending || state.job?.status === "cancelling";
  $("cancel").textContent = state.job?.status === "cancelling" ? "正在取消…" : "取消本題";
  $("selections").querySelectorAll("button").forEach(button => { button.disabled = busy; });
  $("context-results").querySelectorAll("button").forEach(button => { button.disabled = busy || !state.context || state.context.pending || button.dataset.available !== "true"; });
  $("search-results").querySelectorAll("button").forEach(button => { if (button.dataset.definitionLine) button.disabled = state.pending || state.refreshing; });
  $("search-results").querySelectorAll("[data-call-path]").forEach(button => { button.disabled = button.dataset.opening === "true" || state.pending || state.refreshing || Boolean(state.experiment?.preparing); });
  $("outline-list").querySelectorAll("[data-call-query]").forEach(button => { button.disabled = state.pending || state.refreshing || state.source?.version !== state.project?.version; });
  $("traceback-results").querySelectorAll("button").forEach(button => { button.disabled = state.pending || state.refreshing; });
  $("answer").querySelectorAll("button").forEach(button => { if (button.dataset.discovery || button.dataset.retained) button.disabled = state.pending || state.refreshing; });
  $("history-select").disabled = browseOnly() || state.refreshing || !state.project;
  $("history-retry").disabled = browseOnly() || state.refreshing || !state.project;
  $("continue-source").disabled = busy || comparison || !validReadingScope(answerJob());
  $("continuation-clear").disabled = busy;
  renderContinuation();
  sourceRecoveryControls();
  readingNoteControls();
  $("continuation-ranges").querySelectorAll("button").forEach(button => { button.disabled = state.pending || state.refreshing; });
  experimentControls();
  renderModel();
}
function residentCompletion(job) {
  // Immutable request evidence, never the currently live server's state.
  // A .request result cannot masquerade as a sealed one-shot result.
  const result = job?.result, completion = residentMetadata(job?.request_completion);
  if (!["forge8.explain.request", "forge8.locate.request"].includes(result?.kind) || job.gpu !== "resident" || job.error ||
      typeof job.id !== "string" || !job.id || job.id.length > 200 ||
      !["answered", "located", "incomplete"].includes(job.status) || !completion) return null;
  const detail = result.kind === "forge8.explain.request" && job.kind === "project" && result.status === "selection_required" &&
    result.project_reading?.answer_attempted === false && result.outcome === null ? result.project_reading.discovery : result.outcome;
  if (result.server?.scope !== "resident_request" || result.server.server_session_id !== completion.server_session_id ||
      !residentDetailCompletion(detail)) return null;
  return [result.request_completion, result.server.request_completion, detail.request_completion].every(candidate =>
    residentMetadata(candidate) && Object.entries(completion).every(([key, value]) => candidate[key] === value)) ? completion : null;
}
function residentMetadata(value) {
  if (!value || typeof value !== "object" || Array.isArray(value) || Object.keys(value).length !== 7 ||
      typeof value.server_session_id !== "string" || !/^[A-Za-z0-9_-]{1,200}$/.test(value.server_session_id) ||
      value.schema_version !== 1 || value.scope !== "resident_request" || value.request_completed !== true || value.slot_idle !== true ||
      value.transport_secret_cleared !== true || value.session_finalization !== "pending") return null;
  return value;
}
function residentDetailCompletion(detail) {
  const completion = residentMetadata(detail?.request_completion);
  return completion && residentGateMatches(detail.acceptance, completion) ? completion : null;
}
function residentGateMatches(gate, completion) {
  return gate?.ok === true && gate.evidence && typeof gate.evidence === "object" && !Array.isArray(gate.evidence) &&
    Object.entries(completion).every(([key, value]) => gate.evidence[key] === value);
}
function requestFinished(job) {
  if (["forge8.explain.request", "forge8.locate.request"].includes(job?.result?.kind)) return residentCompletion(job) !== null;
  return job?.gpu === "released" && job.request_completion === undefined && job.result?.request_completion === undefined;
}
function readingResultKind(job, kind) {
  return job?.result?.kind === kind || (job?.result?.kind === `${kind}.request` && residentCompletion(job) !== null);
}
function validModelReceipts(receipts) {
  const flags = ["ok", "process_reclaimed", "asset_identity_unchanged", "request_manifests_unchanged", "supervisor_secret_cleared", "transport_secrets_cleared"];
  return Array.isArray(receipts) && receipts.length <= 5 && receipts.every(receipt => receipt && typeof receipt === "object" && !Array.isArray(receipt) &&
    Object.keys(receipt).length === 9 && typeof receipt.id === "string" && /^[A-Za-z0-9_-]{1,200}$/.test(receipt.id) &&
    ["explicit", "idle_timeout", "desk_close", "request_failed", "acquire_failed"].includes(receipt.reason) &&
    flags.every(key => typeof receipt[key] === "boolean") && (!receipt.ok || flags.every(key => receipt[key] === true)) &&
    typeof receipt.receipt_path === "string" && receipt.receipt_path.length > 0 && receipt.receipt_path.length <= 4096 &&
    !/[\u0000-\u001f\u007f-\u009f]/.test(receipt.receipt_path)) && new Set(receipts.map(receipt => receipt.id)).size === receipts.length;
}
function renderModelReceipts() {
  const receipts = state.model?.receipts || [], failedEarlier = state.model?.validation_failed === true,
    identity = JSON.stringify([receipts, failedEarlier]);
  if (identity === modelReceiptsRendered) return;
  modelReceiptsRendered = identity;
  const failed = receipts.filter(receipt => receipt.ok === false);
  $("model-receipt-warning").hidden = failed.length === 0 && !failedEarlier;
  $("model-receipt-warning").textContent = failed.length ? `最近取得的工作階段結束紀錄中，有 ${failed.length} 次驗證未通過。這與模型是否仍在執行是不同狀態；請勿把該工作階段的請求結果當作已通過最終驗證。詳見下方私人紀錄。` : failedEarlier ? "本次閱讀桌曾有工作階段結束驗證未通過；較早紀錄不在最近五筆中。請檢查私人執行目錄，不能視為全部已驗證。" : "";
  $("model-receipts").hidden = receipts.length === 0;
  $("model-receipts-list").replaceChildren();
  for (const receipt of [...receipts].reverse()) {
    const line = element("p", `${receipt.id} · ${receipt.ok ? "工作階段結束驗證通過（不代表解讀正確）" : "工作階段結束驗證未通過"}`);
    const checks = {process_reclaimed: "程序回收", asset_identity_unchanged: "模型／執行環境身份", request_manifests_unchanged: "請求紀錄完整性",
      supervisor_secret_cleared: "模型金鑰清理", transport_secrets_cleared: "請求金鑰清理"};
    const missing = Object.entries(checks).filter(([key]) => receipt[key] === false).map(([, label]) => label);
    if (!receipt.ok) line.append(element("span", `；未通過：${missing.length ? missing.join("、") : "完整紀錄核對或收據發布"}`));
    line.append(element("code", receipt.receipt_path)); $("model-receipts-list").append(line);
  }
}
function renderModel() {
  const enabled = residentEnabled(), model = state.model;
  $("model-status").hidden = !enabled;
  if (!enabled) return;
  renderModelReceipts();
  const unknown = state.modelUnknown || !model;
  const label = state.modelReleasePending ? "正在確認釋放請求" : unknown ? "模型狀態尚未確認" : {
    unloaded: "模型未載入", checking: "正在核對模型", loading: "正在載入模型", busy: "模型正在處理本題",
    idle: "模型常駐 · 可直接問下一題", releasing: "正在釋放模型", cleanup_unknown: "模型清理尚未確認"
  }[model.state];
  if ($("model-label").textContent !== label) $("model-label").textContent = label;
  $("model-reader").textContent = Object.hasOwn(readerLabels, model?.reader) ? readerLabels[model.reader] : "本機模型";
  const note = state.modelError || (unknown ? "正在查詢本機服務；沒有啟動或重送模型工作。" : {
    unloaded: "提問時才會核對並載入模型；瀏覽原碼不啟動模型。",
    checking: "完整核對模型與執行環境。問題可用上方按鈕取消。",
    loading: "正在載入已核對的模型；後續問題可沿用這次載入。",
    busy: "本題尚未完成；取消按鈕只針對目前問題。",
    idle: "仍占用顯示記憶體；閒置逾時或按釋放才會停止。下一題不帶入舊問答。",
    releasing: "等待模型停止與清理；原有問題、選段與答案保留。",
    cleanup_unknown: "不能確認程序已停止或清理完成。新的模型工作、試跑與重新讀取已鎖定；請查看終端機。"
  }[model.state]);
  if ($("model-note").textContent !== note) $("model-note").textContent = note;
  const remaining = !unknown && !state.modelReleasePending && model.state === "idle" && Number.isFinite(model.idle_remaining_seconds) ?
    Math.max(0, Math.ceil(model.idle_remaining_seconds - Math.max(0, Date.now() - state.modelReceivedAt) / 1000)) : null;
  $("model-countdown").textContent = remaining === null ? "" : remaining ? `預計 ${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, "0")} 後釋放` : "等待服務確認逾時釋放";
  $("model-release").disabled = unknown || state.modelReleasePending || !model?.release_allowed || model.state !== "idle" || !model.id || active() || experimentBusy() || state.refreshing;
  $("model-recheck").hidden = !state.modelUnknown && model?.state !== "cleanup_unknown";
  $("model-recheck").disabled = state.modelReleasePending;
}
async function modelApi(path, body) {
  const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 10000);
  try { return await api(path, body, controller.signal); }
  finally { clearTimeout(timeout); }
}
async function pollModel() {
  clearTimeout(modelTimer);
  if (!residentEnabled()) return;
  const project = state.project, request = ++state.modelRequest;
  try {
    const value = await modelApi("/api/model");
    if (request !== state.modelRequest || state.project !== project || !residentEnabled()) return;
    const states = ["unloaded", "checking", "loading", "busy", "idle", "releasing", "cleanup_unknown"];
    if (!value || value.enabled !== true || !states.includes(value.state) ||
        !(value.id === null || (typeof value.id === "string" && /^[A-Za-z0-9_-]{1,200}$/.test(value.id))) ||
        (["loading", "busy", "idle"].includes(value.state) && !value.id) || typeof value.reader !== "string" || value.reader.length > 100 ||
        typeof value.release_allowed !== "boolean" || (value.release_allowed && (value.state !== "idle" || !value.id)) ||
        (value.validation_failed !== undefined && typeof value.validation_failed !== "boolean") ||
        !(value.idle_remaining_seconds === null || (Number.isFinite(value.idle_remaining_seconds) && value.idle_remaining_seconds >= 0 && value.idle_remaining_seconds <= 86400)) ||
        (value.state !== "idle" && value.idle_remaining_seconds !== null) || !validModelReceipts(value.receipts)) throw new Error("模型狀態或結束紀錄回報不完整；未把未知狀態當作已釋放。");
    state.model = value; state.modelUnknown = false; state.modelError = ""; state.modelReceivedAt = Date.now();
  } catch (error) {
    if (request === state.modelRequest && state.project === project) {
      state.modelUnknown = true; state.modelError = `${error.message} 既有答案與草稿保留；只會重新查詢，不會重送載入或釋放。`;
    }
  } finally {
    if (request === state.modelRequest && state.project === project && residentEnabled()) {
      controls();
      updateModelCountdown();
      // Status reads do not renew the server's idle deadline or load a model.
      modelTimer = setTimeout(pollModel, state.modelUnknown || ["checking", "loading", "busy", "releasing"].includes(state.model?.state) ? 1000 : 5000);
    }
  }
}
function updateModelCountdown() {
  clearTimeout(modelCountdownTimer);
  if (!residentEnabled()) return;
  renderModel();
  if (!state.modelUnknown && !state.modelReleasePending && state.model?.state === "idle") modelCountdownTimer = setTimeout(updateModelCountdown, 1000);
}
async function releaseModel() {
  if ($("model-release").disabled || !residentEnabled() || modelMutationBlocked() || active() || !state.model?.id) return;
  const project = state.project, session = state.model.id;
  state.modelReleasePending = true; state.modelUnknown = true; state.modelRequest++; clearTimeout(modelTimer); controls();
  try { await modelApi("/api/model/release", {session_id: session}); }
  catch (error) { if (state.project === project) { state.modelUnknown = true; state.modelError = `${error.message} 釋放尚未確認；只查詢狀態，不會重送釋放。`; } }
  finally {
    if (state.project === project) { state.modelReleasePending = false; controls(); await pollModel(); }
  }
}
$("model-release").addEventListener("click", releaseModel);
$("model-recheck").addEventListener("click", () => { if (!$("model-recheck").disabled) return pollModel(); });
async function experimentApi(path, body) {
  const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 10000);
  try { return await api(path, body, controller.signal); }
  finally { clearTimeout(timeout); } // Browser cancellation never cancels a server-side experiment.
}
function validExperimentTarget(value, project, requireSize = false) {
  return value && project && value.version === project.version &&
    project.files.some(file => file.id === value.file && file.path === value.path && file.path.endsWith(".py")) &&
    typeof value.entry === "string" && !value.entry.startsWith("__") && /^[_\p{XID_Start}][_\p{XID_Continue}]*$/u.test(value.entry) &&
    typeof value.source_sha256 === "string" && /^[0-9a-f]{64}$/.test(value.source_sha256) &&
    (!requireSize || (Number.isSafeInteger(value.source_bytes) && value.source_bytes > 0 && value.source_bytes <= 65536)) &&
    validExperimentPair(value, project) && validExperimentModuleSet(value, project) && validExperimentGenerator(value) && validExperimentWatchSelection(value);
}
function validExperimentPair(target, project) {
  if (!pairedExperiment(target)) return target.mode === undefined && !project.comparison &&
    ["before", "head", "current_snapshot_sha256"].every(key => target[key] === undefined);
  const before = target.before, comparison = project.comparison;
  return Boolean(comparison && target.module_set === undefined && target.path.startsWith("after/") &&
    typeof target.head === "string" && /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/.test(target.head) && target.head === comparison.head &&
    typeof target.current_snapshot_sha256 === "string" && /^[0-9a-f]{64}$/.test(target.current_snapshot_sha256) &&
    target.current_snapshot_sha256 === comparison.original_snapshot_sha256 &&
    Number.isSafeInteger(target.source_bytes) && target.source_bytes > 0 && target.source_bytes <= 65536 &&
    before && before.file !== target.file && before.path === `before/${target.path.slice(6)}` &&
    project.files.some(file => file.id === before.file && file.path === before.path) &&
    typeof before.source_sha256 === "string" && /^[0-9a-f]{64}$/.test(before.source_sha256) &&
    Number.isSafeInteger(before.source_bytes) && before.source_bytes > 0 && before.source_bytes <= 65536);
}
function validExperimentGenerator(value) {
  return value.generator_steps === undefined || Number.isSafeInteger(value.generator_steps) && value.generator_steps >= 1 && value.generator_steps <= 12 &&
    !pairedExperiment(value) && value.module_set === undefined && value.trace_lines !== true && value.search === undefined;
}
function experimentWatchNames(view) {
  const value = (view?.watchDraft || "").trim();
  return value ? value.split(",").map(name => name.trim()) : [];
}
function validExperimentWatchNames(names) {
  const keywords = "False None True and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield".split(" ");
  return Array.isArray(names) && names.length <= 3 && new Set(names).size === names.length &&
    names.every(name => typeof name === "string" && /^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(name) && !keywords.includes(name));
}
function validExperimentWatchSelection(value) {
  return value.watch_names === undefined || validExperimentWatchNames(value.watch_names) && value.watch_names.length > 0 &&
    value.trace_lines === true && !pairedExperiment(value) && value.module_set === undefined && value.generator_steps === undefined && value.search === undefined;
}
function validExperimentWatch(trace, job, file) {
  const names = job.watch_names, watch = trace.watch;
  if (!names?.length) return watch === undefined;
  const shape = (value, keys) => value && typeof value === "object" && !Array.isArray(value) &&
    Object.keys(value).sort().join(",") === [...keys].sort().join(",");
  if (!shape(watch, ["names", "events", "truncated"]) || JSON.stringify(watch.names) !== JSON.stringify(names) ||
      typeof watch.truncated !== "boolean" || !Array.isArray(watch.events) || watch.events.length > 128 ||
      new TextEncoder().encode(JSON.stringify(watch)).length > 24576) return false;
  const calls = new Set();
  return watch.events.every(event => {
    if (!shape(event, ["event", "line", "call_id", "values"]) || !["line", "return", "exception"].includes(event.event) ||
        !Number.isSafeInteger(event.line) || event.line < 1 || event.line > file.lines ||
        !Number.isSafeInteger(event.call_id) || event.call_id < 1 || event.call_id > 128 || !shape(event.values, names) ||
        JSON.stringify(Object.keys(event.values)) !== JSON.stringify(names)) return false;
    if (!calls.has(event.call_id)) { if (event.call_id !== calls.size + 1) return false; calls.add(event.call_id); }
    return names.every(name => {
      const value = event.values[name];
      return value?.state === "value" ? shape(value, ["state", "json"]) && typeof value.json === "string" &&
        value.json.length > 0 && value.json.length <= 2048 && /^[\x20-\x7e]+$/.test(value.json) :
        shape(value, ["state"]) && ["unbound", "unsupported", "limited"].includes(value.state);
    });
  });
}
function sameExperimentIdentity(left, right) {
  return ["file", "path", "version", "entry", "source_sha256", "mode", "head", "current_snapshot_sha256", "generator_steps"].every(key => left[key] === right[key]) &&
    (left.trace_lines === true) === (right.trace_lines === true) && JSON.stringify(left.watch_names || []) === JSON.stringify(right.watch_names || []) &&
    JSON.stringify(left.module_set) === JSON.stringify(right.module_set) &&
    (!pairedExperiment(left) || (left.source_bytes === right.source_bytes &&
      ["file", "path", "source_sha256", "source_bytes"].every(key => left.before?.[key] === right.before?.[key])));
}
function validExperimentSearchPlan(plan, input, target) {
  const shape = (value, keys) => value && typeof value === "object" && !Array.isArray(value) && Object.keys(value).sort().join(",") === keys.sort().join(",");
  const sourced = plan?.strategy === "source-v1";
  return Boolean(shape(plan, ["strategy", "seed_input_text", "inputs", "max_initializations", "max_seconds", "limited", "sha256", ...(sourced ? ["sources"] : [])]) &&
    (sourced || plan.strategy === "nearby-v1") && typeof plan.sha256 === "string" && plan.sha256.length === 64 && /^[0-9a-f]{64}$/.test(plan.sha256) &&
    (!sourced || shape(plan.sources, ["before", "after", "entry"]) && target &&
      plan.sources.before === target.before?.source_sha256 && plan.sources.after === target.source_sha256 && plan.sources.entry === target.entry) &&
    plan.seed_input_text === input && typeof input === "string" && input.trim() && new TextEncoder().encode(input).length <= 16384 &&
    Array.isArray(plan.inputs) && plan.inputs.length >= 1 && plan.inputs.length <= 12 && plan.inputs[0]?.input_text === input && plan.inputs[0]?.location === "seed" &&
    plan.max_initializations === 2 * plan.inputs.length && plan.max_seconds === 120 && typeof plan.limited === "boolean" &&
    plan.inputs.every(item => shape(item, ["input_text", "location", ...(sourced && item?.hint !== undefined ? ["hint"] : [])]) &&
      (item.hint === undefined || sourced && item.location !== "seed" && shape(item.hint, ["side", "line"]) &&
        ["before", "after"].includes(item.hint.side) && Number.isSafeInteger(item.hint.line) && item.hint.line >= 1 && item.hint.line <= 65536) &&
      typeof item.input_text === "string" && item.input_text.trim() &&
      new TextEncoder().encode(item.input_text).length <= 16384 && typeof item.location === "string" && [...item.location].length <= 256 && item.location &&
      !/[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/u.test(item.location)) &&
    new Set(plan.inputs.map(item => item.input_text)).size === plan.inputs.length &&
    new TextEncoder().encode(JSON.stringify(plan)).length <= 512 * 1024);
}
function currentExperimentSearchPlan(view = state.experiment) {
  return view?.searchDraft === true && pairedExperiment(view.target) && view.searchPlanTarget &&
    sameExperimentIdentity(view.searchPlanTarget, view.target) && validExperimentSearchPlan(view.searchPlan, $("experiment-input").value, view.target) ? view.searchPlan : null;
}
function invalidateExperimentSearch(view) {
  view.searchRevision = (view.searchRevision || 0) + 1;
  view.searchPlan = null; view.searchPlanTarget = null; view.searchPending = false; view.searchError = "";
}
function validExperimentSearch(job) {
  const search = job.search;
  if (search === undefined) return true;
  if (!pairedExperiment(job) || !search || typeof search !== "object" || Array.isArray(search) ||
      Object.keys(search).sort().join(",") !== "case_index,completed,input_text,plan_sha256,stop_reason,strategy,total" ||
      !["nearby-v1", "source-v1"].includes(search.strategy) || typeof search.plan_sha256 !== "string" || search.plan_sha256.length !== 64 || !/^[0-9a-f]{64}$/.test(search.plan_sha256) ||
      !Number.isSafeInteger(search.total) || search.total < 1 || search.total > 12 || !Number.isSafeInteger(search.case_index) ||
      search.case_index < 1 || search.case_index > search.total || !Number.isSafeInteger(search.completed) ||
      search.completed < search.case_index - 1 || search.completed > search.case_index ||
      typeof search.input_text !== "string" || !search.input_text.trim() || new TextEncoder().encode(search.input_text).length > 16384 ||
      ![null, "different", "exhausted", "budget", "cancelled", "incomplete"].includes(search.stop_reason)) return false;
  if (search.case_index === 1 && search.input_text !== job.input_text) return false;
  if (["running", "cancelling"].includes(job.status)) return search.stop_reason === null && job.comparison_result === "unavailable";
  if (search.stop_reason === "different" || search.stop_reason === "exhausted") return job.status === "completed" &&
    search.completed === search.case_index && job.comparison_unchanged === true &&
    job.comparison_result === (search.stop_reason === "different" ? "different" : "same") &&
    (search.stop_reason !== "exhausted" || search.completed === search.total);
  return job.comparison_result === "unavailable" && job.comparison_unchanged === false &&
    (search.stop_reason === "cancelled" ? job.status === "cancelled" :
      ["budget", "incomplete"].includes(search.stop_reason) && job.status === "incomplete");
}
function renderExperimentSearch() {
  const view = state.experiment, paired = pairedExperiment(view?.target) || pairedExperiment(view?.baseTarget);
  $("experiment-search-option").hidden = !paired;
  $("experiment-search-enabled").checked = view?.searchDraft === true;
  $("experiment-search-enabled").disabled = !paired || !experimentsEnabled() || experimentBusy() || view?.preparing || active() || modelMutationBlocked() || state.refreshing || state.pairPending;
  const enabled = paired && view?.searchDraft === true, plan = enabled ? currentExperimentSearchPlan(view) : null;
  $("experiment-search-note").hidden = !enabled;
  $("experiment-search-note").textContent = view?.searchError || (view?.searchPending ? "正在產生輸入清單；尚未執行。" : plan ?
    `下方完整列出 ${plan.inputs.length} 組輸入；${plan.limited ? "候選受上限限制。" : ""}確認後最多初始化 ${plan.max_initializations} 次完整模組，搜尋預算 120 秒，另加清理；遇到第一組不同回報即停止。` :
    "先預覽再執行：從兩版程式的比較條件挑選候選值，再補上通用變化。每次只改一個值，最多 12 組，不保證符合輸入契約或走到該分支。預覽不執行程式或啟動模型。");
  $("experiment-search-plan").hidden = !plan;
  const key = plan ? JSON.stringify(plan) : "";
  if (view && view.searchRendered !== key) {
    view.searchRendered = key; $("experiment-search-inputs").replaceChildren();
    if (plan) $("experiment-search-plan").open = true;
    if (plan) for (const item of plan.inputs) {
      const row = element("li", item.location === "seed" ? "原始輸入" : `變動位置：${item.location}`);
      if (item.hint) row.append(element("span", ` · 條件線索：${item.hint.side === "before" ? "HEAD" : "目前"} L${item.hint.line}`));
      const text = element("pre", item.input_text); text.tabIndex = 0; row.append(text); $("experiment-search-inputs").append(row);
    }
  }
}
async function previewExperimentSearch(view) {
  const target = view.target, project = state.project, input = $("experiment-input").value;
  invalidateExperimentSearch(view);
  const revision = view.searchRevision, inputRevision = view.inputRevision || 0;
  const current = () => state.experiment === view && view.visible && state.project === project && view.target === target && view.searchDraft === true &&
    view.searchRevision === revision && (view.inputRevision || 0) === inputRevision && $("experiment-input").value === input &&
    !state.refreshing && !state.pairPending && !active() && !modelMutationBlocked() && !experimentBusy();
  view.searchPending = true; experimentControls();
  try {
    const prepared = await experimentApi("/api/experiment/prepare", {mode: "head_current", file: target.file, version: target.version,
      entry: target.entry, search: "source-v1", input_text: input});
    if (!current()) return;
    if (!validExperimentTarget(prepared, project, true) || !sameExperimentIdentity(prepared, target) ||
        prepared.search_plan?.strategy !== "source-v1" || !validExperimentSearchPlan(prepared.search_plan, input, target))
      throw new Error("輸入清單或兩側來源不一致；沒有執行，請重新預覽。");
    view.searchPlan = prepared.search_plan; view.searchPlanTarget = target;
  } catch (error) { if (current()) view.searchError = `${error.message} 未執行，也未自動重試。`; }
  finally { if (state.experiment === view && view.searchRevision === revision) {view.searchPending = false; experimentControls();} }
}
function validExperimentTrace(job, project) {
  if (!validExperimentWatchSelection(job) || job.trace_lines !== undefined && typeof job.trace_lines !== "boolean") return false;
  if (job.trace_lines === true && (pairedExperiment(job) || job.module_set !== undefined || project?.comparison)) return false;
  const trace = job.reported_trace;
  if (trace === undefined || trace === null) return true;
  const file = project?.files.find(file => file.id === job.file && file.path === job.path);
  return job.trace_lines === true && trace && typeof trace === "object" && !Array.isArray(trace) &&
    Object.keys(trace).sort().join(",") === "hook_intact,line_events,truncated" + (job.watch_names?.length ? ",watch" : "") &&
    typeof trace.truncated === "boolean" && typeof trace.hook_intact === "boolean" &&
    Array.isArray(trace.line_events) && trace.line_events.length <= 1000 && (!trace.truncated || trace.line_events.length === 1000) && Number.isSafeInteger(file?.lines) &&
    trace.line_events.every(line => Number.isSafeInteger(line) && line >= 1 && line <= file.lines) && validExperimentWatch(trace, job, file);
}
function usableExperimentTrace(job, project = state.project) {
  return job?.trace_lines === true && validExperimentTarget(job, project) && validExperimentTrace(job, project) &&
    job.status === "completed" && job.source_unchanged === true && job.runtime_unchanged === true &&
    job.cleanup_unknown !== true && job.reported_trace ? job.reported_trace : null;
}
function validPairedExperimentReport(job) {
  if (!pairedExperiment(job)) return ["observations", "comparison_result", "comparison_rule", "comparison_unchanged"].every(key => job[key] === undefined);
  if (job.phase !== undefined && !["checking", "before", "after"].includes(job.phase) ||
      job.comparison_result !== undefined && !["same", "different", "unavailable"].includes(job.comparison_result) ||
      job.comparison_rule !== undefined && job.comparison_rule !== "canonical-json-v1" ||
      job.comparison_unchanged !== undefined && typeof job.comparison_unchanged !== "boolean") return false;
  const observations = job.observations === undefined ? {} : job.observations;
  if (!observations || typeof observations !== "object" || Array.isArray(observations) || Object.keys(observations).some(key => !["before", "after"].includes(key))) return false;
  for (const observation of Object.values(observations)) {
    if (!observation || typeof observation !== "object" || Array.isArray(observation) ||
        ["stdout", "stderr"].some(key => typeof observation[key] !== "string" || observation[key].length > 196608) ||
        ["process_status", "host_status"].some(key => typeof observation[key] !== "string" || observation[key].length > 200) ||
        ["source_unchanged", "runtime_unchanged", "complete"].some(key => typeof observation[key] !== "boolean") ||
        typeof observation.report_sha256 !== "string" || !/^[0-9a-f]{64}$/.test(observation.report_sha256) ||
        observation.result_text !== undefined && (typeof observation.result_text !== "string" || observation.result_text.length > 393216)) return false;
  }
  return !["same", "different"].includes(job.comparison_result) || (job.status === "completed" && job.comparison_unchanged === true &&
    job.comparison_rule === "canonical-json-v1" && ["before", "after"].every(side => {
      const observation = observations[side];
      return observation?.complete === true && observation.source_unchanged === true && observation.runtime_unchanged === true && typeof observation.result_text === "string";
    }));
}
function validExperimentInputComparison(job, project = state.project) {
  const value = job.input_comparison;
  if (value === undefined) return true; // Original ordinary/module/paired responses remain supported.
  const shape = (object, keys) => object && typeof object === "object" && !Array.isArray(object) && Object.keys(object).sort().join(",") === [...keys].sort().join(",");
  const sha = text => typeof text === "string" && text.length === 64 && !/[^0-9a-f]/.test(text);
  if (pairedExperiment(job) || job.module_set !== undefined || project?.comparison || !shape(value,
      ["revision", "baseline", "current_id", "can_pin", "settling", "outcome", "reason", "current_report_sha256"]) ||
      !Number.isSafeInteger(value.revision) || value.revision < 0 || value.current_id !== job.id ||
      typeof value.can_pin !== "boolean" || typeof value.settling !== "boolean" ||
      !["same", "different", "unavailable"].includes(value.outcome) ||
      ![null, "no_baseline", "current_unavailable", "same_run", "source_changed", "runtime_changed", "trace_mode_changed"].includes(value.reason) ||
      value.current_report_sha256 !== null && !sha(value.current_report_sha256)) return false;
  if ((job.generator_steps !== undefined || job.watch_names?.length) && (value.can_pin || value.current_report_sha256 !== null || value.outcome !== "unavailable" ||
      !["no_baseline", "current_unavailable"].includes(value.reason))) return false;
  const baseline = value.baseline;
  if (baseline !== null && (!shape(baseline, ["id", "version", "file", "path", "entry", "source_sha256", "source_bytes", "input_text", "input_sha256", "runtime_sha256", "report_sha256", "result_text", "trace_lines"]) ||
      !validJobId(baseline.id) || !validExperimentTarget(baseline, project, true) ||
      !["source_sha256", "input_sha256", "runtime_sha256", "report_sha256"].every(key => sha(baseline[key])) || typeof baseline.trace_lines !== "boolean" ||
      typeof baseline.input_text !== "string" || new TextEncoder().encode(baseline.input_text).length > 16384 ||
      typeof baseline.result_text !== "string" || !baseline.result_text || new TextEncoder().encode(JSON.stringify(baseline)).length > 131072)) return false;
  const complete = job.status === "completed" && job.source_unchanged === true && job.runtime_unchanged === true &&
    job.cleanup_unknown !== true && typeof job.result_text === "string" && Boolean(job.result_text) &&
    new TextEncoder().encode(job.result_text).length <= 131072 && sha(value.current_report_sha256) && !value.settling;
  // The public baseline itself has the host's 128 KiB limit; its bounded
  // revision/current-result envelope needs separate space and is never clipped.
  if (value.can_pin && !complete || new TextEncoder().encode(JSON.stringify(value)).length > 131072 + 2048) return false;
  if (baseline?.id === job.id && (baseline.input_text !== job.input_text || baseline.result_text !== job.result_text ||
      baseline.report_sha256 !== value.current_report_sha256 || !experimentBaselineCompatible(job, baseline) || baseline.trace_lines !== (job.trace_lines === true))) return false;
  return value.outcome === "unavailable" || Boolean(complete && baseline && baseline.id !== job.id && value.reason === null &&
    ["file", "path", "version", "entry", "source_sha256", "source_bytes"].every(key => baseline[key] === job[key]) &&
    baseline.trace_lines === (job.trace_lines === true));
}
function acceptExperimentInputComparison(view, job) {
  const value = job.input_comparison;
  if (!value) { view.inputComparison = null; return; }
  const key = JSON.stringify(value.baseline);
  if (view.baselineRevision !== undefined && (value.revision < view.baselineRevision ||
      value.revision === view.baselineRevision && key !== view.baselineKey)) throw new Error("固定 A 的版本或內容發生變動；保留原紀錄，只重新查詢。");
  if (view.comparisonReport?.id === job.id && (view.comparisonReport.sha !== value.current_report_sha256 ||
      view.comparisonReport.text !== job.result_text)) throw new Error("同一試跑的已確認回報發生變動；未更新比較。");
  view.inputComparison = value; view.baselineRevision = value.revision; view.baselineKey = key;
  view.baseline = value.baseline === null ? null : Object.freeze({...value.baseline});
  view.comparisonReport = value.current_report_sha256 ? {id: job.id, sha: value.current_report_sha256, text: job.result_text} : null;
  if (!view.baselineMutation || value.revision >= view.baselineMutation.minimumRevision) {
    view.baselineMutation = null; view.baselineUnknown = false; view.baselineError = "";
  }
}
function experimentBaselineCompatible(target, baseline) {
  return Boolean(target && !pairedExperiment(target) && target.module_set === undefined &&
    (!baseline || ["file", "path", "version", "entry", "source_sha256"].every(key => target[key] === baseline[key])));
}
function renderExperimentBaseline() {
  const view = state.experiment, job = view?.job, value = view?.inputComparison, baseline = view?.baseline;
  const supported = experimentsEnabled() && !inChanges() && experimentBaselineCompatible(view?.target || view?.baseTarget, null) && !(view.moduleDraft || []).length;
  const compatible = supported && experimentBaselineCompatible(view?.target || view?.baseTarget, baseline);
  const bound = Boolean(job && value && job.input_comparison === value && value.current_id === job.id &&
    experimentBaselineCompatible(view?.target, job) && validExperimentInputComparison(job));
  const visible = Boolean(supported && (value || baseline || view.baselineUnknown || view.baselinePending));
  $("experiment-baseline-controls").hidden = !visible;
  const blocked = !view?.visible || !supported || !bound || !Number.isSafeInteger(view.baselineRevision) || value.revision >= Number.MAX_SAFE_INTEGER || view?.checking || view?.preparing || experimentBusy() || experimentBaselineBusy() ||
    active() || modelMutationBlocked() || state.refreshing || state.pairPending;
  $("experiment-baseline-pin").disabled = blocked || !value?.can_pin || baseline?.id === job?.id || Boolean(view?.generatorDraft) || job?.generator_steps !== undefined || experimentWatchNames(view).length > 0 || Boolean(job?.watch_names?.length);
  $("experiment-baseline-pin").textContent = baseline ? "以這次回報取代 A（不執行）" : "固定這次回報為 A（不執行）";
  $("experiment-baseline-clear").hidden = !baseline;
  $("experiment-baseline-clear").disabled = !view?.visible || !visible || !bound || !baseline || view.baselineRevision >= Number.MAX_SAFE_INTEGER || view?.checking || view?.preparing || experimentBusy() || experimentBaselineBusy() ||
    active() || modelMutationBlocked() || state.refreshing || state.pairPending;
  const reasons = {no_baseline: "尚未固定 A；可固定一筆已完整結束的單檔試跑。", current_unavailable: "B 尚無可比較的完整回報；不推測結果，也不會補跑。",
    same_run: "A 已固定；修改輸入後，仍須明確執行下一次試跑。", source_changed: "A 與 B 的來源或入口不同，沒有比較回報。",
    runtime_changed: "A 與 B 的執行環境不同，沒有比較回報。", trace_mode_changed: "A 與 B 的行紀錄設定不同，沒有比較回報。"};
  const verdict = compatible && bound && !view.baselineUnknown && !view.baselinePending && !view.unknown && !view.preparing;
  $("experiment-baseline-summary").textContent = view?.baselinePending ? "正在更新固定 A；沒有執行程式…" : view?.baselineUnknown ?
    `${view.baselineError || "固定 A 的更新尚未確認。"} 只重新查詢，不重送更新；若持續無法確認，請在原終端機結束服務。` : !compatible ?
    `已保留 A${baseline ? `（${baseline.path} · ${baseline.entry}）` : ""}，但目前準備的入口／模組不同；未將舊回報當成此入口的比較。` : value?.settling ? "試跑仍在完成收尾；待工作程序結束後才能固定或比較。" :
    verdict && value.outcome !== "unavailable" ? `A 與 B 的程式回報${value.outcome === "same" ? "相同" : "不同"} · 未驗證` :
    verdict ? reasons[value.reason] || "尚無可比較的完整回報。" : "A 保留不變；下一次仍需明確執行，草稿不是 B 的已送出輸入。";
  if (visible && (view?.generatorDraft || job?.generator_steps !== undefined)) $("experiment-baseline-summary").textContent += "\nGenerator 回報不固定為 A，也不與 A 比較；已有 A 保留，可明確清除。";
  if (visible && (experimentWatchNames(view).length || job?.watch_names?.length)) $("experiment-baseline-summary").textContent += "\n變數快照不固定為 A，也不與 A 比較；已有 A 保留，可明確清除。";
  if (view?.baselineNotice && !view.baselinePending && !view.baselineUnknown) $("experiment-baseline-summary").textContent += `\n${view.baselineNotice}`;
  const showA = Boolean(visible && compatible && baseline);
  $("experiment-baseline-a").hidden = !showA;
  $("experiment-baseline-grid").dataset.pinned = String(showA);
  $("experiment-panel").dataset.baseline = String(showA);
  $("experiment-panel").dataset.generator = String(job?.generator_steps !== undefined);
  const text = (id, value) => { if ($(id).textContent !== value) $(id).textContent = value; };
  text("experiment-baseline-a-identity", showA ? `${baseline.path} · ${baseline.entry}\n試跑 ${baseline.id} · 快照 ${baseline.version}\n完整模組 SHA-256 ${baseline.source_sha256}\n輸入 SHA-256 ${baseline.input_sha256}\n執行環境 SHA-256 ${baseline.runtime_sha256}\n私人回報 SHA-256 ${baseline.report_sha256}\n行紀錄：${baseline.trace_lines ? "已要求" : "未要求"}` : "");
  text("experiment-baseline-a-input", showA ? baseline.input_text : "");
  text("experiment-baseline-a-result", showA ? baseline.result_text : "");
  const currentInput = showA && job && job.id !== baseline.id && validExperimentTarget(job, state.project) && typeof job.input_text === "string" &&
    new TextEncoder().encode(job.input_text).length <= 16384 ? job.input_text : null;
  $("experiment-baseline-b-input").hidden = currentInput === null;
  text("experiment-baseline-b-input-text", currentInput === null ? "" : currentInput);
  $("experiment-baseline-b-title").hidden = !showA;
  text("experiment-baseline-b-title", job?.watch_names?.length ? "變數快照 · 最近一次手動試跑（不與 A 比較）" : job?.generator_steps !== undefined ? "Generator · 最近一次手動試跑（不與 A 比較）" : job?.id === baseline?.id ? "目前回報就是 A；尚未執行 B" : "B · 最近一次手動試跑");
  if (showA && job?.id === baseline.id) {
    $("experiment-result").hidden = true; // Keep this current A's logs and line inspector usable.
  }
}
async function updateExperimentBaseline(clear = false) {
  renderExperimentBaseline();
  if ($(clear ? "experiment-baseline-clear" : "experiment-baseline-pin").disabled || !experimentsEnabled() || inChanges()) return;
  const view = state.experiment, project = state.project, value = view.inputComparison;
  if (!value || !Number.isSafeInteger(view.baselineRevision) || view.baselineRevision >= Number.MAX_SAFE_INTEGER) return;
  const id = clear ? view.baseline?.id : view.job?.id;
  if (!validJobId(id)) return;
  const mutation = {minimumRevision: view.baselineRevision + 1};
  view.baselineMutation = mutation; view.baselinePending = true; view.baselineUnknown = true; view.baselineError = view.baselineNotice = ""; view.statusRequest++;
  renderExperiment();
  try { await experimentApi(clear ? "/api/experiment/baseline/clear" : "/api/experiment/baseline", {id, version: project.version, revision: view.baselineRevision}); }
  catch (error) {
    if (state.experiment === view && state.project === project && view.baselineMutation === mutation) {
      if (Number.isInteger(error.httpStatus) && error.httpStatus >= 400 && error.httpStatus < 500) {
        mutation.minimumRevision--; view.baselineNotice = `更新未完成：${error.message}`;
      }
      view.baselineError = error.message;
    }
  } finally {
    if (state.experiment === view && state.project === project && view.baselineMutation === mutation) {
      view.baselinePending = false; renderExperiment(); await pollExperiment();
    }
  }
}
function validExperimentModuleSet(target, project) {
  if (target.module_set === undefined) return true;
  const set = target.module_set;
  return set && set.import_root === "." && set.entry_path === target.path && set.entry === target.entry &&
    typeof set.entry_module === "string" && /^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$/.test(set.entry_module) &&
    typeof set.sha256 === "string" && /^[0-9a-f]{64}$/.test(set.sha256) && Array.isArray(set.files) && set.files.length >= 1 && set.files.length <= 4 &&
    set.files.every(file => file && typeof file.path === "string" && project.files.some(item => item.path === file.path && item.path.endsWith(".py")) &&
      Number.isSafeInteger(file.size_bytes) && file.size_bytes >= 0 && typeof file.sha256 === "string" && /^[0-9a-f]{64}$/.test(file.sha256)) &&
    new Set(set.files.map(file => file.path.toLowerCase())).size === set.files.length && set.files.reduce((sum, file) => sum + file.size_bytes, 0) <= 65536 &&
    set.files.some(file => file.path === target.path && file.sha256 === target.source_sha256 && file.size_bytes === target.source_bytes);
}
function experimentModuleIds(target) {
  return target?.module_set?.files.map(member => state.project.files.find(file => file.path === member.path).id) || [];
}
function experimentModulesDirty() {
  const view = state.experiment;
  if (pairedExperiment(view?.baseTarget)) return false;
  return JSON.stringify([...(view?.moduleDraft || [])].sort()) !== JSON.stringify(experimentModuleIds(view?.target).filter(id => id !== view?.baseTarget?.file).sort());
}
function renderExperimentModules() {
  const view = state.experiment, base = view?.baseTarget;
  if (!base || pairedExperiment(base)) { $("experiment-modules").hidden = true; return; }
  $("experiment-modules").hidden = false;
  const key = `${base.version}:${base.file}:${base.entry}`;
  if (view.moduleListKey === key) return;
  view.moduleListKey = key; $("experiment-module-filter").value = "";
  const rows = state.project.files.filter(file => file.id !== base.file && file.path.endsWith(".py")).map(file => {
    const row = element("label", ""), box = element("input", "");
    row.dataset.path = file.path; box.type = "checkbox"; box.value = file.id; box.checked = (view.moduleDraft || []).includes(file.id);
    box.addEventListener("change", () => {
      if (box.disabled || state.experiment !== view) return;
      view.moduleDraft = [...$("experiment-module-options").querySelectorAll("input")].filter(input => input.checked).map(input => input.value);
      experimentControls();
    });
    row.append(box, element("span", file.path)); return row;
  });
  $("experiment-module-options").replaceChildren(...rows);
}
function currentExperimentCallInput(request) {
  const {view, project, source, target, anchor, end, input, revision} = request;
  return state.experiment === view && view.visible && state.project === project && state.source === source && view.target === target &&
    state.anchor === anchor && state.end === end && $("experiment-input").value === input && (view.inputRevision || 0) === revision &&
    !view.preparing && !experimentBusy() && !active() && !state.refreshing && !state.pairPending && !experimentModulesDirty();
}
function experimentControls() {
  const view = state.experiment, busy = experimentBusy(), inputBytes = new TextEncoder().encode($("experiment-input").value).length;
  const paired = pairedExperiment(view?.target) || pairedExperiment(view?.baseTarget);
  $("experiment-call-input").hidden = paired;
  if (view?.callInputRequest && !currentExperimentCallInput(view.callInputRequest)) {
    view.callInputRequest = null; view.inputPending = false;
    view.callInputNote = "選取、入口或草稿已變更；未覆蓋輸入。";
  }
  const watchNames = experimentWatchNames(view), watchChanged = Boolean(view?.job && JSON.stringify(view.job.watch_names || []) !== JSON.stringify(watchNames));
  const generator = view?.generatorDraft || 0, generatorChanged = Boolean(view?.job && (view.job.generator_steps || 0) !== generator);
  $("experiment-input-state").hidden = !view?.job || typeof view.job.input_text !== "string" || view.job.input_text === $("experiment-input").value && !generatorChanged && !watchChanged;
  $("experiment-input").readOnly = busy || Boolean(view?.preparing);
  $("experiment-input-note").textContent = `${inputBytes.toLocaleString()} / 16,384 位元組${inputBytes > 16384 ? " · 超過上限，請縮短；未裁切原稿。" : " · 原文送出，保留大型整數；不自動修正 JSON。"}`;
  const modulesDirty = experimentModulesDirty(), modulesBlocked = paired || inChanges() || !experimentsEnabled() || !view?.baseTarget || view.preparing || busy || experimentBaselineBusy() || active() || modelMutationBlocked() || state.refreshing || state.pairPending;
  const single = Boolean(view?.target && !paired && !inChanges() && !view.target.module_set && !(view.moduleDraft || []).length);
  const generatorAllowed = single && !view?.traceDraft && !watchNames.length, generatorInvalid = Boolean(generator && (!generatorAllowed || !Number.isSafeInteger(generator) || generator < 1 || generator > 12));
  $("experiment-generator-option").hidden = !single && !generator;
  $("experiment-generator-steps").value = String(generator);
  $("experiment-generator-steps").disabled = !experimentsEnabled() || !view?.target || view.preparing || busy || experimentBaselineBusy() || active() || modelMutationBlocked() || state.refreshing || state.pairPending || !generator && (!generatorAllowed || modulesDirty);
  $("experiment-generator-note").hidden = !single && !generator;
  $("experiment-generator-submitted").hidden = !view?.job || paired || view.job.generator_steps === undefined && !generator;
  $("experiment-generator-submitted").textContent = view?.job ? `已送出模式：Generator ${view.job.generator_steps ? `最多 ${view.job.generator_steps} 次 next()，最後嘗試 close()` : "不推進"}。上方選單只修改下一次草稿。` : "";
  const traceAllowed = single && !generator;
  $("experiment-trace-option").hidden = $("experiment-trace-option-note").hidden = !traceAllowed;
  $("experiment-trace-lines").disabled = !traceAllowed || modulesBlocked || modulesDirty;
  $("experiment-trace-lines").checked = view?.traceDraft === true;
  const watchAllowed = traceAllowed && view?.traceDraft === true, watchInvalid = !validExperimentWatchNames(watchNames) || Boolean(watchNames.length && !watchAllowed);
  $("experiment-watch-option").hidden = !watchAllowed && !watchNames.length;
  $("experiment-watch-names").disabled = modulesBlocked;
  if ($("experiment-watch-names").value !== (view?.watchDraft || "")) $("experiment-watch-names").value = view?.watchDraft || "";
  $("experiment-watch-note").textContent = watchInvalid ? "請先啟用單檔行紀錄，輸入最多 3 個不重複的 ASCII 區域名稱，以逗號分隔；或清空以關閉。" :
    "只記錄入口函式已宣告的區域名稱；最多 128 個事件／24 KiB。行事件的值在該行執行前取得；return 也可能是例外展開，不代表成功。欄位只修改下次草稿，不執行。";
  $("experiment-watch-submitted").hidden = !view?.job || !view.job.watch_names?.length && !watchNames.length;
  $("experiment-watch-submitted").textContent = view?.job ? "已送出的區域名稱：" + (view.job.watch_names?.join(", ") || "未啟用") + "。上方欄位只修改下次草稿。" : "";
  if (modulesDirty) $("experiment-input-state").hidden = false;
  $("experiment-input-state").textContent = modulesDirty ? "模組選擇已修改；目前回報仍屬先前來源。請展開同專案模組並核對，再明確執行。" : "輸入或試跑設定已修改；目前回報仍屬上次送出的輸入與設定。按執行才會產生新的回報。";
  $("experiment-run").disabled = !experimentsEnabled() || !view?.target || inChanges() && !paired || modulesDirty || generatorInvalid || watchInvalid || view.preparing || view.searchPending || view.inputPending || busy || experimentBaselineBusy() || active() || modelMutationBlocked() || state.refreshing || state.pairPending || inputBytes > 16384 || !$("experiment-input").value.trim();
  const searchPlan = currentExperimentSearchPlan(view);
  $("experiment-run").textContent = paired && view?.searchDraft === true ? (searchPlan ?
    `確認上列 ${searchPlan.inputs.length} 組：最多初始化 ${searchPlan.max_initializations} 次完整模組並搜尋（120 秒＋清理）` : "預覽候選輸入（不執行）") : paired ? `依序初始化 HEAD／目前兩份完整模組，各呼叫一次 ${view?.target?.entry || "函式"}` :
    `${view?.target?.module_set ? `匯入所列 ${view.target.module_set.files.length} 份完整模組，再呼叫` : "執行完整模組，再呼叫"} ${view?.target?.entry || "函式"}`;
  if (generator) $("experiment-run").textContent = `執行完整模組與函式，最多 ${generator} 次 next()，最後嘗試 close()`;
  $("experiment-modules").hidden = !view?.baseTarget || paired || Boolean(generator);
  $("experiment-module-prepare").disabled = modulesBlocked || Boolean(generator);
  $("experiment-module-filter").disabled = modulesBlocked || Boolean(generator);
  $("experiment-module-options").querySelectorAll("input").forEach(box => { box.disabled = modulesBlocked || Boolean(generator) || !box.checked && (view?.moduleDraft || []).length >= 3; });
  $("experiment-module-note").textContent = modulesDirty ? "模組選擇已變更；先核對後才能執行。目前回報仍屬先前已確認的來源。" : "勾選只調整草稿；核對不執行。清空額外檔案並核對，可返回原本單檔模式。";
  const source = state.source, selected = source?.version === state.project?.version && source?.path.endsWith(".py") &&
    Number.isSafeInteger(state.anchor) && Number.isSafeInteger(state.end) && Math.min(state.anchor, state.end) >= 1 &&
    Math.max(state.anchor, state.end) <= source.lines.length && Math.abs(state.anchor - state.end) < 80;
  $("experiment-from-call").disabled = !view?.visible || !view.target || !selected || modulesBlocked || modulesDirty || view.inputPending;
  $("experiment-call-selection").textContent = `${selected ? `${source.path} · L${Math.min(state.anchor, state.end)}–L${Math.max(state.anchor, state.end)}` : "請先在原碼窗選取完整呼叫（最多 80 行）"} → ${view?.target ? `${view.target.path} · ${view.target.entry}` : "尚未確認試跑入口"}。只帶入同名呼叫的字面值；不追變數、不驗證實際名稱綁定。會取代 JSON 草稿，不執行程式。`;
  $("experiment-call-note").textContent = view?.inputPending ? "正在讀取保留原碼中的字面值；沒有執行呼叫…" : view?.callInputNote || "";
  $("experiment-close").disabled = busy;
  $("experiment-cancel").hidden = !busy || !view?.job?.id || view.job.cleanup_unknown === true;
  $("experiment-cancel").disabled = !view?.job?.id || view.submitting || view.cancelPending || view.job.status === "cancelling" || view.job.cleanup_unknown === true;
  $("experiment-cancel").textContent = view?.job?.status === "cancelling" ? "正在取消並清理…" : "取消這次試跑";
  $("experiment-recheck").hidden = !view?.unknown && !view?.baselineUnknown && view?.job?.cleanup_unknown !== true;
  $("experiment-recheck").disabled = !view || view.checking || view.submitting || view.cancelPending || view.baselinePending;
  $("outline-list").querySelectorAll("[data-experiment-entry]").forEach(button => {
    button.disabled = !experimentsEnabled() || active() || modelMutationBlocked() || busy || experimentBaselineBusy() || Boolean(view?.preparing) || state.refreshing || state.pairPending || state.source?.version !== state.project?.version;
  });
  renderExperimentTrace();
  renderExperimentBaseline();
  renderExperimentSearch();
}
function experimentRuntimeStatus(result) {
  const processLabels = {passed: "工作程序正常結束", failed: "工作程序未正常結束", timed_out: "工作程序逾時",
    launch_error: "工作程序發生啟動或清理錯誤", unavailable: "工作程序狀態未取得"};
  const hostLabels = {exited: "WASI 已結束", guest_exit: "WASI 由程式結束", trap: "WASI 已中止", unavailable: "WASI 狀態未取得"};
  const labels = [];
  if (result?.process_status) labels.push(Object.hasOwn(processLabels, result.process_status) ? processLabels[result.process_status] : `工作程序回報：${result.process_status}`);
  if (result?.host_status) labels.push(Object.hasOwn(hostLabels, result.host_status) ? hostLabels[result.host_status] : `WASI 回報：${result.host_status}`);
  return labels.length ? `${labels.join(" · ")}（僅為執行環境狀態，不代表函式正確）` : "";
}
function renderExperiment() {
  const view = state.experiment, target = view?.target, job = view?.job;
  const paired = pairedExperiment(target) || pairedExperiment(view?.baseTarget);
  $("experiment-panel").hidden = !view?.visible;
  $("experiment-panel").dataset.mode = paired ? "head_current" : "single";
  $("experiment-title").textContent = paired ? "同一輸入，比較兩個版本的回報" : "試一組輸入";
  renderExperimentModules();
  $("experiment-target").textContent = target ? `${target.path} · ${target.entry}` : view?.label || "正在確認本次試跑…";
  $("experiment-identity").textContent = target ? `${target.path} · ${target.entry}\n快照 ${target.version}\n完整模組 SHA-256 ${target.source_sha256}${Number.isSafeInteger(target.source_bytes) ? ` · ${target.source_bytes.toLocaleString()} 位元組` : ""}` : view?.label || "正在確認本次試跑狀態…";
  const modules = target?.module_set;
  $("experiment-modules-bound").hidden = !modules;
  $("experiment-modules-bound").textContent = modules ? `已確認 ${modules.files.length} 檔：${modules.files.map(file => file.path).join(" · ")}` : "";
  $("experiment-notice").textContent = modules ? `在獨立 CPython/WASI 匯入 ${modules.entry_module}，再呼叫函式；所列完整模組（包含父套件）的初始化也可能執行。僅提供選定模組的 ZIP，不掛載原始專案。` : "執行快照的完整模組（含初始化），再呼叫函式；不是只跑選取行。獨立 CPython/WASI，不掛載專案。";
  if (paired) {
    $("experiment-notice").textContent = "同一份 JSON 輸入，依序送入兩個獨立 CPython/WASI：HEAD 與目前版本各初始化完整模組，再各呼叫函式一次。不是只跑選取行；不掛載專案、不使用模型、不自動重試。";
    if (target) $("experiment-identity").textContent = `${target.path.slice(6)} · ${target.entry}\nHEAD ${target.head}\nHEAD 完整模組 SHA-256 ${target.before.source_sha256} · ${target.before.source_bytes} 位元組\n目前完整模組 SHA-256 ${target.source_sha256} · ${target.source_bytes} 位元組\n目前原碼快照 ${target.current_snapshot_sha256}\n比較快照 ${target.version}`;
  }
  if (modules) $("experiment-identity").textContent += `\n匯入根：本次閱讀來源根\n模組組合 SHA-256 ${modules.sha256}\n` + modules.files.map(file => `${file.path} · ${file.size_bytes} 位元組\nSHA-256 ${file.sha256}`).join("\n");
  if (view?.callInputSource) $("experiment-identity").textContent += `\n引數字面值來源（不代表呼叫綁定）：${view.callInputSource.path} · L${view.callInputSource.call_start}–L${view.callInputSource.call_end}\nSHA-256 ${view.callInputSource.source_sha256}`;
  const labels = {running: "試跑中；包含完整性檢查與清理。", cancelling: "正在取消；請等待原生程序停止與清理。", completed: "這次試跑已結束；只代表這組輸入的觀察。", incomplete: "試跑未完整完成；沒有自動重試。", cancelled: "試跑已取消；沒有自動重試。"};
  const runtimeStatus = experimentRuntimeStatus(job);
  const phase = job?.cleanup_unknown === true ? "警告：無法確認原生程序已停止與清理。已鎖定新的試跑、AI 問題與重新讀取；請在終端機確認服務與程序狀態。重新查詢不會重試執行或清理。" :
    view?.unknown ? "狀態尚未確認；連線中斷不代表程序已停止。只會重新查詢，不會自動重送執行。" :
    view?.submitting ? "正在送出這次明確授權的試跑，尚待服務確認…" : view?.preparing ? "正在核對完整模組與入口；尚未執行程式碼…" :
    job ? `${labels[job.status] || "狀態不明"}${Number.isFinite(job.elapsed_seconds) ? ` ${Math.floor(job.elapsed_seconds)} 秒。` : ""}${runtimeStatus ? ` ${runtimeStatus}。` : ""}` :
    active() ? "本機模型仍在工作；完成後才能試跑。輸入與原有問題均保留。" : modelMutationBlocked() ? "模型正在切換或清理狀態未確認；請等候或查看模型狀態。輸入草稿保留，尚未試跑。" : "先確認完整模組與 JSON，再按執行；開啟此面板不會執行程式碼。";
  $("experiment-status").textContent = [phase, view?.error, view?.generatorNotice, job?.error,
    paired && job?.status === "running" ? ({checking: "正在核對兩側原碼、輸入與回報完整性。", before: "正在處理 HEAD 版本；目前版本尚未開始。", after: "HEAD 階段已結束，正在處理目前版本。"}[job.phase] || "正在確認配對試跑階段。") : "",
    paired && job?.comparison_unchanged === false && !["running", "cancelling"].includes(job.status) ? "未確認兩側比較來源完整性；沒有形成有效的配對回報比較。" : "",
    job?.source_unchanged === false ? "警告：保留快照或輸入的完整性檢查未通過，不能把回報綁定到上方原碼與輸入。" : "",
    job?.runtime_unchanged === false ? "警告：執行環境完整性檢查未通過；回報不可作為可信結果。" : ""].filter(Boolean).join("\n");
  const result = !paired && typeof job?.result_text === "string" ? job.result_text : "";
  $("experiment-result").hidden = !result;
  if ($("experiment-result-text").textContent !== result) $("experiment-result-text").textContent = result;
  let hasLogs = false;
  for (const name of ["stdout", "stderr"]) {
    const text = !paired && typeof job?.[name] === "string" ? job[name] : "";
    $("experiment-" + name + "-wrap").hidden = !text;
    if ($("experiment-" + name).textContent !== text) $("experiment-" + name).textContent = text;
    hasLogs ||= Boolean(text);
  }
  $("experiment-logs").hidden = !hasLogs;
  renderPairedExperiment(job, paired);
  controls();
  // Reveal a newly arrived result inside this panel only. Never steal keyboard
  // focus or scroll the question/source/page; subsequent polling preserves position.
  const pairReady = paired && job && !["running", "cancelling"].includes(job.status) && Object.keys(job.observations || {}).length;
  if ((result || pairReady) && view && view.presentedResult !== job.id) {
    view.presentedResult = job.id;
    const panel = $("experiment-panel"), output = $("experiment-output-side");
    if (panel.clientHeight > 0 && panel.scrollHeight > panel.clientHeight) {
      const bounds = panel.getBoundingClientRect(), resultBounds = $(paired ? "experiment-pair-result" : "experiment-result").getBoundingClientRect();
      const revealTop = job.generator_steps !== undefined ? resultBounds.top : output.getBoundingClientRect().top;
      if (resultBounds.bottom > bounds.bottom - 12) panel.scrollTop += Math.max(0,
        Math.min(revealTop - bounds.top - 12, resultBounds.bottom - bounds.bottom + 12));
    }
  }
}
function renderPairedExperiment(job, paired) {
  $("experiment-pair-result").hidden = !paired || !job;
  const valid = paired && job && validPairedExperimentReport(job) && validExperimentSearch(job);
  const labels = {same: "這組輸入的兩側回報相同", different: "這組輸入的兩側回報不同", unavailable: "尚無可比較的完整回報"};
  $("experiment-pair-summary").textContent = valid ? labels[job.comparison_result || "unavailable"] : "配對回報尚未確認";
  const search = valid ? job.search : null;
  $("experiment-search-current").hidden = !search;
  $("experiment-search-current-input").textContent = search?.input_text || "";
  $("experiment-search-current-label").textContent = search ? `第 ${search.case_index}／${search.total} 組的實際輸入 · 原始輸入另見「本次已送出的輸入」` : "";
  if (search) $("experiment-pair-summary").textContent = search.stop_reason === "different" ?
    `找到不同回報：第 ${search.case_index} 組（已完成 ${search.completed} 組配對，後續未執行）` : search.stop_reason === "exhausted" ?
      `已測 ${search.completed} 組，未找到回報差異；不代表兩版等價` : search.stop_reason === "budget" ?
        `搜尋預算已到；已完成 ${search.completed}／${search.total} 組，搜尋未完成` : search.stop_reason === "cancelled" ?
          `搜尋已取消；已完成 ${search.completed}／${search.total} 組，未推測其餘結果` : search.stop_reason === "incomplete" ?
            `搜尋未完整完成；已完成 ${search.completed}／${search.total} 組，沒有自動補跑` :
            `正在比較第 ${search.case_index}／${search.total} 組；已完成 ${search.completed} 組`;
  // Polling binds the job's identity/input; this disclosure only renders its bounded original text.
  const submitted = valid && typeof job.id === "string" && job.id && typeof job.input_text === "string" &&
    new TextEncoder().encode(job.input_text).length <= 16384 ? job.input_text : null;
  const disclosure = $("experiment-submitted-input"), recordId = submitted === null ? "" : job.id;
  disclosure.hidden = submitted === null;
  if (disclosure.dataset.job !== recordId) { disclosure.open = false; disclosure.dataset.job = recordId; }
  if ($("experiment-submitted-input-text").textContent !== (submitted || "")) $("experiment-submitted-input-text").textContent = submitted || "";
  for (const side of ["before", "after"]) {
    const observation = valid ? job.observations?.[side] : null;
    const prefix = `experiment-${side}`;
    $(prefix + "-status").textContent = observation ? `${observation.complete ? "本側試跑已結束" : "本側試跑未完整完成"} · ${experimentRuntimeStatus(observation)}${!observation.source_unchanged || !observation.runtime_unchanged ? " · 原碼／執行環境完整性未通過" : ""}${typeof observation.result_text !== "string" ? " · 沒有可比較的 JSON 回報" : ""}` : "尚無本側回報；不推測結果，也不會補跑。";
    const result = observation?.result_text;
    $(prefix + "-result").hidden = typeof result !== "string";
    if ($(prefix + "-result").textContent !== (result || "")) $(prefix + "-result").textContent = result || "";
    $(prefix + "-report").textContent = observation ? `私人回報 SHA-256 ${observation.report_sha256}` : "";
    let hasLogs = false;
    for (const name of ["stdout", "stderr"]) {
      const text = observation?.[name] || "";
      $(prefix + "-" + name + "-wrap").hidden = !text;
      if ($(prefix + "-" + name).textContent !== text) $(prefix + "-" + name).textContent = text;
      hasLogs ||= Boolean(text);
    }
    $(prefix + "-logs").hidden = !hasLogs;
  }
}
function renderExperimentTrace() {
  const view = state.experiment, job = view?.job, trace = usableExperimentTrace(job);
  const visible = job?.trace_lines === true && !pairedExperiment(job) && !job.module_set;
  $("experiment-trace").hidden = !visible;
  if (!visible) {
    if (view) view.traceView = null;
    $("experiment-trace-source").hidden = true;
    if ($("experiment-trace-source").children.length) $("experiment-trace-source").replaceChildren();
    $("experiment-trace-input-text").textContent = ""; renderExperimentWatch(null, 0); return;
  }
  const key = JSON.stringify([job.id, job.version, job.source_sha256, job.input_text, job.reported_trace]);
  if (view.traceView?.key !== key) view.traceView = {key, index: 0, source: null, read: null, note: "", rendered: null};
  const inspector = view.traceView;
  if (inspector.read && !inspector.read.current()) {
    inspector.read = null; inspector.note = "閱讀狀態或草稿已改變；未採用過期讀取，請重新按查看。";
  }
  const watch = trace?.watch, count = (watch ? watch.events.length : trace?.line_events.length) || 0;
  renderExperimentWatch(watch, view.traceView.index);
  $("experiment-trace-summary").textContent = !trace ? "本次已要求行紀錄，但尚無可綁定到原碼與輸入的回報；不推測已執行路徑。" :
    `${count} 次行事件（依序保留重複）。${trace.truncated ? "已達上限而截斷；不是完整路徑。" : "未回報截斷；仍不是完整路徑證明。"}${trace.hook_intact ? "" : " 掛鉤結束狀態不符，紀錄可能中斷。"}${count ? "" : " 沒有捕捉到符合範圍的事件，不代表沒有執行程式。"}`;
  if (watch) $("experiment-trace-summary").textContent = count + " 個入口函式事件；本次名稱：" + watch.names.join(", ") + "。" +
    (watch.truncated ? "事件或位元組上限已截斷；不是完整路徑。" : "未回報截斷；仍不是完整路徑證明。") +
    (trace.hook_intact ? "" : " 掛鉤結束狀態不符，紀錄可能中斷。") + " 原有本檔行紀錄另保留 " + trace.line_events.length + " 次。";
  const input = typeof job.input_text === "string" && new TextEncoder().encode(job.input_text).length <= 16384 ? job.input_text : null;
  const disclosure = $("experiment-trace-input"); disclosure.hidden = input === null;
  if (disclosure.dataset.job !== job.id) { disclosure.open = false; disclosure.dataset.job = job.id; }
  if ($("experiment-trace-input-text").textContent !== (input || "")) $("experiment-trace-input-text").textContent = input || "";
  $("experiment-trace-controls").hidden = !count;
  const blocked = !count || Boolean(inspector.read) || !view.visible || view.unknown || view.preparing || active() || experimentBusy() || state.refreshing || state.pairPending || modelMutationBlocked();
  $("experiment-trace-step").disabled = $("experiment-trace-view").disabled = blocked;
  $("experiment-trace-prev").disabled = blocked || inspector.index <= 0;
  $("experiment-trace-next").disabled = blocked || inspector.index >= count - 1;
  const stepKey = `${job.id}:${inspector.index}`;
  if ($("experiment-trace-step").dataset.step !== stepKey) { $("experiment-trace-step").value = String(inspector.index + 1); $("experiment-trace-step").dataset.step = stepKey; }
  $("experiment-trace-step").max = String(count);
  const event = watch?.events[inspector.index], line = event?.line ?? trace?.line_events[inspector.index];
  $("experiment-trace-position").textContent = count ? `第 ${inspector.index + 1} / ${count} 次 → ${job.path}:L${line}。片段的前後文不代表也有行事件。` : "";
  if (event) $("experiment-trace-position").textContent += " 呼叫 #" + event.call_id + " · " +
    ({line: "執行這行之前", exception: "exception 事件（不代表未被捕捉）", return: "return 事件（可能是例外展開，不代表成功）"}[event.event]);
  const oversizedLine = Boolean(inspector.source && count && inspector.source.lines[line - 1].length > 4000);
  if (oversizedLine) inspector.note = "本行超過 4,000 顯示字元；未裁切或顯示不完整原碼。";
  const note = inspector.read ? "正在讀取保留原碼；不執行程式或更改模型選段…" : inspector.note;
  if ($("experiment-trace-note").textContent !== note) $("experiment-trace-note").textContent = note;
  const source = $("experiment-trace-source"); source.hidden = !trace || !inspector.source || !count || oversizedLine;
  const sourceKey = source.hidden ? "" : `${key}:${inspector.index}`;
  if (inspector.rendered === sourceKey) return;
  inspector.rendered = sourceKey; source.replaceChildren();
  if (source.hidden) return;
  let start = Math.max(1, line - 3), end = Math.min(inspector.source.lines.length, line + 3);
  if (inspector.source.lines.slice(start - 1, end).join("\n").length > 4000) start = end = line;
  for (let current = start; current <= end; current++) {
    const row = element("span", `${String(current).padStart(4)}│ ${inspector.source.lines[current - 1]}${current < end ? "\n" : ""}`, current === line ? "experiment-trace-current" : "");
    row.dataset.traceLine = current; source.append(row);
  }
}
function renderExperimentWatch(watch, index) {
  const box = $("experiment-watch-values"), event = watch?.events[index], key = JSON.stringify([watch, index]);
  box.hidden = !event;
  if (box.dataset.snapshot === key) return;
  box.dataset.snapshot = key; box.replaceChildren();
  if (!event) return;
  let previous = null;
  for (let at = index - 1; at >= 0; at--) if (watch.events[at].call_id === event.call_id) { previous = watch.events[at]; break; }
  for (const name of watch.names) {
    const value = event.values[name], prior = previous?.values[name], comparable = value.state === "value" && prior?.state === "value";
    const section = element("section", ""), label = name + (comparable ? value.json === prior.json ? " · 與此呼叫上次快照相同" : " · 此呼叫的快照已變動" : " · 無可比較的前值");
    section.append(element("h4", label));
    const text = value.state === "value" ? value.json : {unbound: "尚未綁定", unsupported: "此值型別不支援", limited: "超過值的深度／大小上限"}[value.state];
    const shown = comparable && value.json !== prior.json ? "前次快照：" + prior.json + "\n目前快照：" + text : text;
    const output = element("pre", shown); output.tabIndex = 0; section.append(output); box.append(section);
  }
}
async function viewExperimentTrace(index) {
  const view = state.experiment, job = view?.job, project = state.project, trace = usableExperimentTrace(job), inspector = view?.traceView;
  if (!trace || !inspector || $("experiment-trace-view").disabled) return;
  const count = trace.watch ? trace.watch.events.length : trace.line_events.length;
  if (!Number.isSafeInteger(index) || index < 0 || index >= count) { inspector.note = "請輸入 1–" + count + " 的整數次序。"; renderExperimentTrace(); return; }
  inspector.index = index; inspector.note = "";
  if (inspector.source) { renderExperimentTrace(); return; }
  const input = $("experiment-input").value, inputRevision = view.inputRevision || 0, optionRevision = view.traceRevision || 0, watchRevision = view.watchRevision || 0;
  const question = $("question").value, questionRevision = state.questionRevision, focus = state.focus, focusKey = JSON.stringify(focus);
  const sourceRequest = state.sourceRequest, contextRequest = state.contextRequest, anchor = state.anchor, end = state.end;
  const selectedHistory = state.selectedHistory, historyRequest = state.historyRequest, readingJob = state.job?.id, continuation = state.continuation;
  const request = {current: () => state.experiment === view && view.traceView === inspector && inspector.read === request && view.visible &&
    state.project === project && view.job?.id === job.id && sameExperimentIdentity(view.job, job) && view.job.input_text === job.input_text &&
    JSON.stringify(view.job.reported_trace) === JSON.stringify(trace) && usableExperimentTrace(view.job, project) &&
    $("experiment-input").value === input && (view.inputRevision || 0) === inputRevision && (view.traceRevision || 0) === optionRevision && (view.watchRevision || 0) === watchRevision &&
    $("question").value === question && state.questionRevision === questionRevision && state.focus === focus && JSON.stringify(state.focus) === focusKey &&
    state.sourceRequest === sourceRequest && state.contextRequest === contextRequest && state.anchor === anchor && state.end === end &&
    state.selectedHistory === selectedHistory && state.historyRequest === historyRequest && state.job?.id === readingJob && state.continuation === continuation &&
    !view.unknown && !view.preparing && !active() && !experimentBusy() && !state.refreshing && !state.pairPending && !modelMutationBlocked()};
  inspector.read = request; renderExperimentTrace();
  try {
    const source = await experimentApi(`/api/source?${new URLSearchParams({file: job.file, version: job.version})}`);
    if (!request.current()) {
      if (inspector.read === request) inspector.note = "閱讀狀態或草稿已改變；未採用過期讀取，請重新按查看。";
      return;
    }
    const file = project.files.find(file => file.id === job.file && file.path === job.path);
    if (source?.version !== job.version || source.path !== job.path || !Array.isArray(source.lines) || source.lines.length !== file.lines ||
        source.lines.some(line => typeof line !== "string") || source.lines.reduce((size, line) => size + line.length + 1, 0) > 6 * 65536)
      throw new Error("原碼身分、行數或顯示上限不符；沒有展示其他版本的片段。");
    inspector.source = source;
  } catch (error) {
    if (request.current()) inspector.note = error.name === "AbortError" ? "原碼讀取超過 10 秒；沒有重試，可稍後自行按查看。" : "原碼讀取未完成或身分不符；沒有重試，也未更改原有閱讀狀態。";
  } finally {
    if (inspector.read === request) inspector.read = null;
    if (state.experiment === view && view.traceView === inspector) renderExperimentTrace();
  }
}
function resetExperiment(refreshed = false) {
  clearTimeout(experimentTimer);
  const recover = experimentsEnabled() && !refreshed;
  state.experiment = {target: null, job: null, input: '{"args":[],"kwargs":{}}', visible: recover,
    preparing: false, submitting: false, unknown: recover, checking: false, cancelPending: false,
    error: "", label: "", lastId: null, submission: null, prepareRequest: 0, statusRequest: 0, traceDraft: false, traceRevision: 0,
    searchDraft: false, searchRevision: 0, generatorDraft: 0, generatorRevision: 0, watchDraft: "", watchRevision: 0};
  $("experiment-input").value = state.experiment.input; $("experiment-logs").open = false;
  $("experiment-enabled").hidden = !experimentsEnabled();
  $("experiment-enabled").textContent = `${refreshed ? "已重新讀取原碼；先前試跑已清除。" : ""}本次已明確開放函式試跑。仍須逐次確認，才會在獨立 WASI 環境執行完整模組；不會自動執行 AI 產生的程式碼。`;
  renderExperiment();
  if (recover) pollExperiment();
}
async function openExperiment(item, source) {
  if (!experimentsEnabled() || active() || modelMutationBlocked() || experimentBusy() || experimentBaselineBusy() || state.experiment?.preparing || state.refreshing || state.pairPending ||
      source !== state.source || source?.version !== state.project?.version || item.kind !== "function" || item.name.includes(".") ||
      inChanges() && !source.path.startsWith("after/")) return;
  const view = state.experiment;
  invalidateExperimentSearch(view); view.searchDraft = false; view.searchExecutionPlan = null;
  view.baseTarget = {file: source.file.id, path: source.path, version: state.project.version, entry: item.name, ...(inChanges() ? {mode: "head_current"} : {})};
  view.traceDraft = false; view.traceRevision = (view.traceRevision || 0) + 1;
  view.watchDraft = ""; view.watchRevision = (view.watchRevision || 0) + 1;
  view.generatorDraft = 0; view.generatorRevision = (view.generatorRevision || 0) + 1; view.generatorNotice = "";
  view.moduleDraft = []; view.moduleListKey = ""; $("experiment-modules").open = false;
  view.callInputNote = ""; view.callInputSource = null; view.callInputRequest = null; view.inputPending = false;
  $("experiment-call-input").open = false;
  view.input = '{"args":[],"kwargs":{}}'; $("experiment-input").value = view.input;
  return prepareExperimentTarget(view);
}
async function prepareExperimentTarget(view) {
  invalidateExperimentSearch(view);
  const project = state.project, source = state.source, base = view.baseTarget, request = ++view.prepareRequest;
  const modules = !pairedExperiment(base) && view.moduleDraft?.length ? [base.file, ...view.moduleDraft].sort() : null;
  view.target = view.job = null; view.submission = null; view.preparing = true; view.visible = true; view.error = "";
  view.label = `${base.path} · ${base.entry}`;
  $("experiment-logs").open = false; renderExperiment();
  try {
    const prepared = await experimentApi("/api/experiment/prepare", {file: base.file, version: project.version, entry: base.entry,
      ...(pairedExperiment(base) ? {mode: "head_current"} : {}), ...(modules ? {modules} : {})});
    if (state.experiment !== view || request !== view.prepareRequest || state.project !== project || state.source !== source || state.refreshing) return;
    if (!validExperimentTarget(prepared, project, true) || prepared.file !== base.file || prepared.path !== base.path || prepared.entry !== base.entry || prepared.mode !== base.mode ||
        JSON.stringify(experimentModuleIds(prepared).sort()) !== JSON.stringify(modules || [])) throw new Error("試跑來源或入口不一致；未執行程式碼，請重新核對模組。");
    view.target = prepared; $("outline").open = false; $("experiment-modules").open = false;
  } catch (error) {
    if (state.experiment === view && request === view.prepareRequest && state.project === project && state.source === source && !state.refreshing)
      view.error = `${error.message} ${pairedExperiment(base) ? "尚未執行；可關閉面板，核對 HEAD／目前版本的完整模組與入口。" : "尚未執行；可修正模組選擇後重新核對，或關閉面板重新選擇函式。"}`;
  } finally {
    if (state.experiment === view && request === view.prepareRequest) { view.preparing = false; renderExperiment(); }
  }
}
async function pollExperiment() {
  clearTimeout(experimentTimer);
  const view = state.experiment, project = state.project;
  if (!experimentsEnabled() || !view || view.checking || view.baselinePending || state.refreshing) return;
  const request = ++view.statusRequest, traceRevision = view.traceRevision || 0, searchRevision = view.searchRevision || 0; view.checking = true; experimentControls();
  try {
    const job = await experimentApi("/api/experiment/current");
    if (state.experiment !== view || state.project !== project || request !== view.statusRequest || state.refreshing || view.submitting) return;
    if (job?.id === null && job.status === "idle") {
      view.job = null; view.unknown = Boolean(view.submission?.search || view.submission?.generator || view.submission?.watch);
      if (view.unknown) view.error = "試跑送出狀態尚未確認；空閒回覆不代表未受理。只查詢，不重送。";
      else if (view.submission) { view.submission = null; view.error = `服務目前沒有新試跑；沒有重送執行。${view.error}`; }
      else if (!view.target && !view.preparing) view.visible = false;
    } else {
      if (!validExperimentTarget(job, project) || typeof job.id !== "string" || !job.id ||
          !["running", "cancelling", "completed", "incomplete", "cancelled"].includes(job.status) ||
          typeof job.input_text !== "string" || new TextEncoder().encode(job.input_text).length > 16384 ||
          !Number.isFinite(job.elapsed_seconds) || job.elapsed_seconds < 0 ||
          ["result_text", "stdout", "stderr", "process_status", "host_status", "error"].some(key => job[key] !== undefined && typeof job[key] !== "string") ||
          ["cleanup_unknown", "source_unchanged", "runtime_unchanged"].some(key => job[key] !== undefined && typeof job[key] !== "boolean") ||
          !validPairedExperimentReport(job) || !validExperimentTrace(job, project) || !validExperimentSearch(job)) throw new Error("試跑回報的來源或狀態不完整；未展示其他版本或未確認的結果。");
      if (view.submission && job.id === view.submission.previous && !["running", "cancelling"].includes(job.status)) {
        if (view.submission.search || view.submission.generator || view.submission.watch) {view.unknown = true; view.error = "仍是前一次試跑；這次是否受理尚未確認。只查詢，不重送。";}
        else {view.submission = null; view.unknown = false; view.error = `服務尚未回報新的試跑；沒有重送執行。${view.error}`;}
      } else {
        if (view.job?.id === job.id && (!sameExperimentIdentity(view.job, job) || view.job.input_text !== job.input_text ||
              view.job.reported_trace && JSON.stringify(view.job.reported_trace) !== JSON.stringify(job.reported_trace)) ||
            view.submission?.target && (!sameExperimentIdentity(view.submission.target, job) || view.submission.input !== job.input_text)) throw new Error("同一試跑的來源或輸入發生變動；保留先前狀態，請重新確認。");
        if (view.job?.id === job.id && ["strategy", "plan_sha256", "total"].some(key => view.job.search?.[key] !== job.search?.[key]) ||
            view.submission?.search && (!job.search || job.search.strategy !== view.submission.search.strategy ||
              job.search.plan_sha256 !== view.submission.search.sha256 || job.search.total !== view.submission.search.inputs.length) ||
            job.search && view.searchExecutionPlan?.sha256 === job.search.plan_sha256 &&
              (job.search.total !== view.searchExecutionPlan.inputs.length || job.search.input_text !== view.searchExecutionPlan.inputs[job.search.case_index - 1]?.input_text))
          throw new Error("搜尋清單或實際輸入不一致；未覆蓋先前回報，也未重送執行。");
        try {
          if (!validExperimentInputComparison(job, project)) throw new Error("A／B 回報的身分或格式不一致；未顯示比較。");
          acceptExperimentInputComparison(view, job);
        } catch (error) { view.inputComparison = null; view.baselineUnknown = true; view.baselineError = error.message; }
        const preserveDraft = view.job?.id === job.id && view.input !== view.job.input_text;
        if (view.job?.id !== job.id && (view.traceRevision || 0) === traceRevision) view.traceDraft = job.trace_lines === true;
        if (view.job?.id !== job.id && (view.searchRevision || 0) === searchRevision) view.searchDraft = Boolean(job.search);
        // Recover only an untouched page draft; even an ABA edit must survive late GETs.
        if (view.job?.id !== job.id && !(view.generatorRevision || 0)) view.generatorDraft = job.generator_steps || 0;
        if (view.job?.id !== job.id && !(view.watchRevision || 0)) view.watchDraft = (job.watch_names || []).join(", ");
        if (!view.baseTarget || view.job?.id !== job.id) {
          view.baseTarget = {file: job.file, path: job.path, version: job.version, entry: job.entry, ...(pairedExperiment(job) ? {mode: "head_current"} : {})};
          view.moduleDraft = experimentModuleIds(job).filter(id => id !== job.file); view.moduleListKey = "";
        }
        if (view.callInputSource && (view.callInputSource.input_text !== job.input_text || view.callInputSource.entry !== job.entry)) {
          view.callInputSource = null; view.callInputNote = "";
        }
        view.job = job; view.target = job; view.lastId = job.id;
        if (!preserveDraft) view.input = job.input_text;
        view.visible = true; view.unknown = false; view.error = ""; view.submission = null;
        $("experiment-input").value = view.input;
      }
    }
    renderExperiment();
  } catch (error) {
    if (state.experiment === view && state.project === project && request === view.statusRequest && !state.refreshing) {
      if (view.baselineUnknown && view.job && !["running", "cancelling"].includes(view.job.status) && !view.submission && !view.submitting) view.baselineError = error.message;
      else { view.unknown = true; view.error = `${error.message} 可重新確認狀態；關閉瀏覽器或中斷請求不會取消試跑。`; }
      view.visible = true; renderExperiment();
    }
  } finally {
    view.checking = false;
    if (state.experiment === view) { experimentControls(); if ((experimentBusy() || experimentBaselineBusy()) && view.job?.cleanup_unknown !== true && !state.refreshing) experimentTimer = setTimeout(pollExperiment, 1000); }
  }
}
async function runExperiment() {
  if ($("experiment-run").disabled || !experimentsEnabled() || active() || modelMutationBlocked() || experimentBusy() || experimentBaselineBusy()) return;
  const view = state.experiment, target = view.target, project = state.project, input = $("experiment-input").value, previousJob = view.job;
  if (!validExperimentTarget(target, project, true) || new TextEncoder().encode(input).length > 16384) return;
  const searching = pairedExperiment(target) && view.searchDraft === true, searchPlan = searching ? currentExperimentSearchPlan(view) : null;
  if (searching && !searchPlan) return previewExperimentSearch(view);
  const generator = view.generatorDraft || 0;
  if (generator && (!validExperimentGenerator({...target, generator_steps: generator, trace_lines: view.traceDraft === true}) ||
      experimentModulesDirty() || view.moduleDraft?.length || view.searchDraft)) return;
  const guardedGenerator = Boolean(generator || target.generator_steps !== undefined);
  const watchNames = experimentWatchNames(view), guardedWatch = Boolean(watchNames.length || target.watch_names?.length);
  if (!validExperimentWatchNames(watchNames) || watchNames.length && (!view.traceDraft || generator || pairedExperiment(target) || target.module_set || inChanges() || view.moduleDraft?.length || view.searchDraft)) return;
  const tracing = view.traceDraft === true && !pairedExperiment(target) && !target.module_set;
  // A polled target also carries the old result; copy only source identity into the new job.
  const submittedTarget = Object.fromEntries(["file", "path", "version", "entry", "source_sha256", "source_bytes", "mode", "head", "current_snapshot_sha256", "before", "module_set"]
    .filter(key => target[key] !== undefined).map(key => [key, target[key]]));
  if (tracing) submittedTarget.trace_lines = true;
  if (watchNames.length) submittedTarget.watch_names = [...watchNames];
  if (generator) submittedTarget.generator_steps = generator;
  view.input = input; view.submitting = true; view.unknown = false; view.error = ""; view.generatorNotice = "";
  view.searchExecutionPlan = searchPlan;
  if (searchPlan) $("experiment-search-plan").open = false;
  view.submission = {previous: view.lastId, ...(pairedExperiment(target) || tracing || target.trace_lines === true || guardedGenerator || guardedWatch ? {target: submittedTarget, input} : {}), ...(searchPlan ? {search: searchPlan} : {}), ...(guardedGenerator ? {generator: true} : {}), ...(guardedWatch ? {watch: true} : {})}; view.job = null; view.statusRequest++;
  $("experiment-logs").open = false; $("experiment-call-input").open = false; renderExperiment();
  try {
    const job = await experimentApi("/api/experiment/run", {file: target.file, version: target.version, entry: target.entry,
      source_sha256: target.source_sha256, input_text: input, allow_execution: true,
      ...(tracing ? {trace_lines: true} : {}), ...(generator ? {generator_steps: generator} : {}), ...(watchNames.length ? {watch_names: watchNames} : {}),
      ...(pairedExperiment(target) ? {mode: "head_current", head: target.head, before_sha256: target.before.source_sha256} : {}),
      ...(searchPlan ? {search: searchPlan.strategy, search_plan_sha256: searchPlan.sha256} : {}),
      ...(target.module_set ? {modules: experimentModuleIds(target), module_set_sha256: target.module_set.sha256} : {})});
    if (state.experiment !== view || state.project !== project) return;
    if (typeof job?.id !== "string" || !job.id || (searchPlan || guardedGenerator || guardedWatch) && (!validJobId(job.id) || job.id === view.submission?.previous)) throw new Error("服務未回傳可追蹤的新試跑編號。");
    view.job = {...submittedTarget, id: job.id, status: "running", input_text: input, elapsed_seconds: 0,
      ...(searchPlan ? {search: {strategy: searchPlan.strategy, plan_sha256: searchPlan.sha256, total: searchPlan.inputs.length, completed: 0,
        case_index: 1, input_text: input, stop_reason: null}} : {}),
      ...(pairedExperiment(target) ? {phase: "checking", observations: {}, comparison_result: "unavailable", comparison_rule: "canonical-json-v1", comparison_unchanged: false} : {})};
    view.lastId = job.id; view.submission = null;
  } catch (error) {
    if (state.experiment === view) {
      if (searchPlan && error.httpStatus >= 400 && error.httpStatus < 500) {
        invalidateExperimentSearch(view);
        view.job = previousJob; view.submission = null; view.unknown = false; view.error = `${error.message} 服務拒絕搜尋；未重送執行。`;
        view.searchError = view.error;
      } else if ((guardedGenerator || guardedWatch) && error.httpStatus >= 400 && error.httpStatus < 500) {
        view.job = previousJob; view.submission = null; view.unknown = false; view.generatorNotice = `${error.message} 服務拒絕試跑；未重送執行。`;
      } else { view.unknown = true; view.error = `${error.message} 未重送；正在查詢是否已建立這次試跑。`; }
    }
  } finally {
    if (state.experiment === view) { view.submitting = false; renderExperiment(); await pollExperiment(); }
  }
}
async function cancelExperiment() {
  if ($("experiment-cancel").disabled || !experimentBusy()) return;
  const view = state.experiment, id = view.job?.id;
  if (!id) return;
  view.cancelPending = true; view.statusRequest++; view.job = {...view.job, status: "cancelling"}; renderExperiment();
  try { await experimentApi("/api/experiment/cancel", {id}); }
  catch (error) { if (state.experiment === view) { view.unknown = true; view.error = `${error.message} 取消尚未確認；請重新確認狀態。`; } }
  finally { if (state.experiment === view) { view.cancelPending = false; renderExperiment(); await pollExperiment(); } }
}
async function importExperimentCallInputs() {
  if ($("experiment-from-call").disabled) return;
  const view = state.experiment, source = state.source, target = view.target, project = state.project;
  const request = {view, source, target, project, anchor: state.anchor, end: state.end,
    input: $("experiment-input").value, revision: view.inputRevision || 0};
  const start = Math.min(request.anchor, request.end), end = Math.max(request.anchor, request.end);
  view.callInputRequest = request; view.inputPending = true; view.callInputNote = ""; experimentControls();
  try {
    const result = await experimentApi("/api/experiment/input", {file: source.file.id, version: project.version, start, end, entry: target.entry});
    if (view.callInputRequest !== request || !currentExperimentCallInput(request)) return;
    const keys = ["file", "path", "version", "source_sha256", "entry", "start", "end", "call_start", "call_end", "input_text"];
    if (!result || Object.keys(result).length !== keys.length || keys.some(key => !Object.hasOwn(result, key)) ||
        result.file !== source.file.id || result.path !== source.path || result.version !== project.version || result.entry !== target.entry ||
        result.start !== start || result.end !== end || !Number.isSafeInteger(result.call_start) || !Number.isSafeInteger(result.call_end) ||
        result.call_start < start || result.call_end > end || result.call_start > result.call_end ||
        typeof result.source_sha256 !== "string" || !/^[a-f0-9]{64}$/.test(result.source_sha256) ||
        typeof result.input_text !== "string" || !result.input_text.trim() || new TextEncoder().encode(result.input_text).length > 16384)
      throw new Error("呼叫引數的來源或範圍不一致；原有草稿已保留。");
    // Keep the server's exact JSON text: JSON.parse/stringify would round bigint.
    view.callInputRequest = null; view.inputPending = false; view.callInputSource = result;
    view.input = result.input_text; $("experiment-input").value = result.input_text;
    view.inputRevision = (view.inputRevision || 0) + 1;
    view.callInputNote = `已從 ${result.path} · L${result.call_start}–L${result.call_end} 帶入 ${target.entry} 的字面值引數。未執行；名稱相同不代表實際呼叫同一函式，請確認入口與 JSON 再按執行。`;
    renderExperiment();
  } catch (error) {
    if (view.callInputRequest === request && currentExperimentCallInput(request)) view.callInputNote = `${error.message} 未改寫草稿，也未執行程式。`;
  } finally {
    if (view.callInputRequest === request) { view.callInputRequest = null; view.inputPending = false; }
    if (state.experiment === view) experimentControls();
  }
}
$("experiment-input").addEventListener("input", () => {
  const view = state.experiment; if (!view) return;
  const hadCallSource = Boolean(view.callInputSource);
  if (experimentBusy() || view.preparing) $("experiment-input").value = view.input;
  else {
    view.input = $("experiment-input").value; view.inputRevision = (view.inputRevision || 0) + 1;
    invalidateExperimentSearch(view);
    view.callInputSource = null; view.callInputNote = "";
  }
  if (hadCallSource) renderExperiment(); else experimentControls();
});
$("experiment-search-enabled").addEventListener("change", () => {
  const view = state.experiment; if (!view) return;
  if (!$("experiment-search-enabled").disabled) { invalidateExperimentSearch(view); view.searchDraft = $("experiment-search-enabled").checked === true; }
  experimentControls();
});
$("experiment-generator-steps").addEventListener("change", () => {
  const view = state.experiment, control = $("experiment-generator-steps"); if (!view) return;
  if (!control.disabled && /^(?:[0-9]|1[0-2])$/.test(control.value) && (control.value === "0" ||
      !pairedExperiment(view.target) && !inChanges() && !view.target?.module_set && !view.moduleDraft?.length && !view.traceDraft && !experimentWatchNames(view).length)) {
    view.generatorDraft = Number(control.value); view.generatorRevision = (view.generatorRevision || 0) + 1;
  }
  experimentControls();
});
$("experiment-trace-lines").addEventListener("change", () => {
  const view = state.experiment; if (!view) return;
  if (!$("experiment-trace-lines").disabled) { view.traceDraft = $("experiment-trace-lines").checked; view.traceRevision = (view.traceRevision || 0) + 1; }
  experimentControls();
});
$("experiment-watch-names").addEventListener("input", () => {
  const view = state.experiment; if (!view || $("experiment-watch-names").disabled) return;
  view.watchDraft = $("experiment-watch-names").value; view.watchRevision = (view.watchRevision || 0) + 1; experimentControls();
});
$("experiment-trace-view").addEventListener("click", () => viewExperimentTrace(Number($("experiment-trace-step").value) - 1));
$("experiment-trace-step").addEventListener("change", () => viewExperimentTrace(Number($("experiment-trace-step").value) - 1));
$("experiment-trace-prev").addEventListener("click", () => { if (!$("experiment-trace-prev").disabled) return viewExperimentTrace(state.experiment.traceView.index - 1); });
$("experiment-trace-next").addEventListener("click", () => { if (!$("experiment-trace-next").disabled) return viewExperimentTrace(state.experiment.traceView.index + 1); });
$("experiment-run").addEventListener("click", runExperiment);
$("experiment-baseline-pin").addEventListener("click", () => updateExperimentBaseline());
$("experiment-baseline-clear").addEventListener("click", () => updateExperimentBaseline(true));
$("experiment-from-call").addEventListener("click", importExperimentCallInputs);
$("experiment-module-prepare").addEventListener("click", () => {
  if (!$("experiment-module-prepare").disabled && state.experiment?.baseTarget) return prepareExperimentTarget(state.experiment);
});
$("experiment-module-filter").addEventListener("input", () => {
  const query = $("experiment-module-filter").value.toLocaleLowerCase();
  $("experiment-module-options").querySelectorAll("label").forEach(row => { row.hidden = !row.dataset.path.toLocaleLowerCase().includes(query); });
});
$("experiment-cancel").addEventListener("click", cancelExperiment);
$("experiment-recheck").addEventListener("click", () => { if (!$("experiment-recheck").disabled) return pollExperiment(); });
$("experiment-close").addEventListener("click", () => {
  if (experimentBusy()) return;
  const view = state.experiment; if (!view) return;
  invalidateExperimentSearch(view); view.prepareRequest++; view.preparing = false; view.visible = false; renderExperiment();
});
function renderHistory() {
  $("reading-tools-summary").textContent = state.historyError ? "紀錄讀取失敗 · 開啟查看" : state.selectedHistory ? "正在回看先前問題 · 紀錄與原碼工具" :
    answerJob()?.status === "incomplete" && validReadingScope(answerJob()) ? "補充原碼、再提問 · 紀錄與工具" : `閱讀紀錄${state.history.length ? `（${state.history.length} 題）` : ""}、沿用原碼與匯出`;
  const identity = JSON.stringify([state.job?.question, state.selectedHistory, state.historyEvicted, state.historyError, state.historyBoundary,
    state.history.map(job => [job.id, job.status, job.elapsed_seconds, job.question])]);
  if (historyRendered === identity) return; // Status polling must not rebuild an open native selector.
  historyRendered = identity;
  const labels = {answered: "解讀完成", located: "定位完成", incomplete: "未完成", cancelled: "已取消"};
  const summary = text => [...text].length > 96 ? [...text].slice(0, 96).join("") + "…" : text;
  const current = element("option", `目前工作${state.job?.question ? " · " + summary(state.job.question) : ""}`); current.value = ""; current.title = state.job?.question || "目前工作";
  $("history-select").replaceChildren(current);
  for (const job of [...state.history].reverse()) {
    const label = gatedInsufficient(job) !== null && (job.kind !== "project" || projectReading(job)) ? "模型指出資訊不足" : labels[job.status];
    const option = element("option", `${label} · ${Math.floor(job.elapsed_seconds)} 秒 · ${summary(job.question)}`); option.title = job.question;
    option.value = job.id; $("history-select").append(option);
  }
  $("history-select").value = state.selectedHistory || "";
  $("history-note").textContent = state.historyError || `${state.historyBoundary}${state.selectedHistory ? "正在回看先前問題；上方狀態與取消按鈕仍屬於目前工作。" : "回看不啟動模型，也不會把舊問答加入下一題。"} 本次閱讀桌暫存 ${state.history.length} / 5 題（最多 512 KiB）；關閉服務或重新讀取專案即清除。${state.historyEvicted ? ` 已因容量上限移除 ${state.historyEvicted} 題。` : ""}`;
  $("history-retry").hidden = !state.historyError;
}
function readingScopeRanges(scope) {
  if (!scope || typeof scope !== "object" || Array.isArray(scope) || Object.keys(scope).sort().join(",") !== "focus,origin,supplements" ||
      !["user_focus", "project_candidates"].includes(scope.origin) || !Array.isArray(scope.focus) ||
      !scope.focus.length || scope.focus.length > (scope.origin === "project_candidates" ? 6 : 3) ||
      !Array.isArray(scope.supplements) || scope.supplements.length > 4 || (scope.origin === "user_focus" && scope.supplements.length)) return null;
  const parse = selector => {
    if (typeof selector !== "string" || selector.length > 600) return null;
    const match = selector.match(/^([^:]+):([1-9][0-9]{0,6})-([1-9][0-9]{0,6})$/);
    if (!match) return null;
    const file = state.project?.files.find(file => file.path === match[1]), start = Number(match[2]), end = Number(match[3]);
    return file && start <= end && end <= Math.min(file.lines, 1000000) && end - start < 80 ? {file, start, end} : null;
  };
  const ranges = scope.focus.map(parse), supplements = scope.supplements.map(parse);
  if (ranges.some(span => !span) || supplements.some(span => !span) || new Set(scope.focus.map(value => value.toLowerCase())).size !== ranges.length ||
      new Set(scope.supplements.map(value => value.toLowerCase())).size !== supplements.length ||
      supplements.reduce((sum, span) => sum + span.end - span.start + 1, 0) > 40) return null;
  const lines = new Set(ranges.flatMap(span => Array.from({length: span.end - span.start + 1}, (_, index) => `${span.file.path}\0${span.start + index}`)));
  if (lines.size > 240 || supplements.some(span => Array.from({length: span.end - span.start + 1}, (_, index) => `${span.file.path}\0${span.start + index}`).some(line => !lines.has(line)))) return null;
  return ranges;
}
function validReadingScope(job) {
  const result = job?.result, outcome = result?.outcome;
  if (browseOnly() || inChanges() || !job || typeof job.id !== "string" || !job.id || typeof job.question !== "string" || [...job.question].length > 2000 ||
      job.version !== state.project?.version || job.comparison || job.kind === "locate" || job.error || !requestFinished(job) ||
      !readingResultKind(job, "forge8.explain") || outcome?.source_unchanged !== true || outcome.snapshot_unchanged !== true || outcome.acceptance?.ok !== true ||
      (job.kind === "project" && !projectReading(job)) ||
      !((job.status === "answered" && result.ok === true && outcome.ok === true) || unverifiedProse(job) !== null || gatedInsufficient(job) !== null) ||
      !readingScopeRanges(job.reading_scope)) return null;
  return job.reading_scope;
}
function gatedInsufficient(job) {
  const result = job?.result, outcome = result?.outcome, reason = outcome?.failure_reason;
  return !inChanges() && !job?.comparison && job?.status === "incomplete" && job.kind !== "locate" && !job.error && !result?.error &&
    job.version === state.project?.version && readingResultKind(job, "forge8.explain") && requestFinished(job) &&
    result.status === "insufficient_evidence" && result.ok === false && outcome?.status === "insufficient_evidence" && outcome.ok === false &&
    outcome.answer === null && outcome.source_unchanged === true && outcome.snapshot_unchanged === true && outcome.acceptance?.ok === true &&
    typeof reason === "string" && reason.trim() && [...reason].length <= 1000 && !/[\p{Cf}\p{Zl}\p{Zp}\p{Cs}]/u.test(reason) && !/[\p{Cc}]/u.test(reason.replace(/[\r\n\t]/g, "")) ? reason : null;
}
function readingNotePlan() {
  const job = answerJob(), result = job?.result, outcome = result?.outcome;
  if (browseOnly() || state.refreshing || !state.project || job?.version !== state.project.version || job?.status !== "answered" ||
      result?.status !== "answered" || result.ok !== true || outcome?.status !== "answered" || outcome.ok !== true ||
      job.error || result.error || !requestFinished(job) || !readingResultKind(job, "forge8.explain") ||
      !validComparisonJob(job) || (job.kind === "project" && !projectReading(job))) return null;
  const scope = job.kind === "changes" && inChanges() ?
    {origin: "user_focus", focus: job.comparison?.ranges, supplements: []} : validReadingScope(job);
  const ranges = readingScopeRanges(scope);
  if (!ranges) return null;
  try { return Forge8ReadingNote.prepare(job, ranges); } catch { return null; }
}
function invalidateReadingNote() {
  state.readingExportRevision++;
  if (state.readingExport?.pending) {
    state.readingExport.controller.abort();
    state.readingExportNotice = "閱讀題目或來源狀態已變更，本次未匯出。請確認畫面中的題目後再按一次。";
  } else state.readingExportNotice = "";
  state.readingExport = null;
}
function readingNoteCurrent(view, plan) {
  return state.readingExport === view && view.revision === state.readingExportRevision &&
    state.project === view.project && state.selectedHistory === view.selectedHistory &&
    plan !== null && JSON.stringify(plan) === view.identity;
}
function readingNoteControls() {
  const plan = readingNotePlan(), view = state.readingExport;
  if (view?.pending && !readingNoteCurrent(view, plan)) invalidateReadingNote();
  $("export-reading-note").disabled = !plan || Boolean(state.readingExport?.pending);
  $("export-reading-note").textContent = state.readingExport?.pending ? "正在整理本題閱讀筆記…" : "匯出畫面中這題的閱讀筆記（含原碼）";
  $("reading-note-status").textContent = state.readingExport?.pending ? "只取得這題的暫存原碼；不重新提問、不修改草稿。" :
    state.readingExportNotice || (plan ? "下載 Markdown，可在關閉閱讀桌後回看。不啟動模型。" : "完成且來源檢查通過的解讀才可匯出；資訊不足、預覽與未完成工作不當作答案。");
}
async function exportReadingNote() {
  if ($("export-reading-note").disabled) return;
  const plan = readingNotePlan();
  if (!plan) { readingNoteControls(); return; }
  const view = {pending: true, project: state.project, selectedHistory: state.selectedHistory,
    revision: ++state.readingExportRevision, identity: JSON.stringify(plan), controller: new AbortController()};
  state.readingExport = view; state.readingExportNotice = ""; readingNoteControls();
  const timeout = setTimeout(() => view.controller.abort(), 10000);
  try {
    const sources = await Promise.all(plan.files.map(file =>
      api(`/api/source?file=${encodeURIComponent(file.id)}&version=${encodeURIComponent(plan.version)}`, undefined, view.controller.signal)));
    if (view.controller.signal.aborted || !readingNoteCurrent(view, readingNotePlan())) throw new Error("stale export");
    const note = Forge8ReadingNote.render(plan, sources);
    // No await after the final identity check: a different displayed question cannot slip in.
    const link = document.createElement("a"); link.download = "forge8-reading-note.md"; link.hidden = true;
    const url = URL.createObjectURL(new Blob([note], {type: "text/markdown;charset=utf-8"}));
    try { link.href = url; document.body.append(link); link.click(); }
    finally { link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
    state.readingExportNotice = "本題筆記已交由瀏覽器儲存。檔案含問題與原碼；分享前請自行檢查敏感內容。";
  } catch {
    if (state.readingExport === view) state.readingExportNotice = "未匯出：原碼資料不完整、版本不符、超過容量或等待上限。請確認本機服務與題目後再試；不會自動重送。";
  } finally {
    clearTimeout(timeout); view.controller.abort();
    if (state.readingExport === view) view.pending = false;
    readingNoteControls();
  }
}
$("export-reading-note").addEventListener("click", exportReadingNote);
function sourceRecoveryControls() {
  const job = answerJob(), scope = validReadingScope(job), view = state.sourceRecovery;
  $("replace-source").hidden = !scope;
  $("replace-source").disabled = !scope || scope.focus.length > 3 || Boolean(view?.pending) || active() || experimentBusy() || state.refreshing || state.pairPending || modelMutationBlocked();
  $("replace-source-note").hidden = !scope && !view;
  $("replace-source-note").textContent = view && view.jobId === job?.id && view.selectedHistory === state.selectedHistory ? view.note : !scope ? "" : scope.focus.length > 3 ?
    "本題超過手動 3 段上限；僅能查看並自行選段，不會擷取前三段。" : "明確取代全部手動選段；保留問題草稿與問答紀錄，不送出問題。先以顯示原碼保守檢查來源預算，實際提問仍由服務完整檢查。";
}
function recoveredSourceFits(ranges, sources) {
  const evidence = [];
  for (const [index, span] of ranges.entries()) {
    const source = sources[index];
    if (!source || source.version !== state.project.version || source.path !== span.file.path || !Array.isArray(source.lines) || source.lines.length !== span.file.lines || source.lines.some(line => typeof line !== "string")) throw new Error("原碼版本、路徑或行數不一致；未取代任何選段。");
    const text = source.lines.slice(span.start - 1, span.end).map((line, offset) => `${String(span.start + offset).padStart(6)}|${line}`).join("\n");
    if ([...text].length > 4000) return false;
    // Display escaping can overcount raw source. This is a conservative UI
    // check, not a substitute for the next question's authoritative preflight.
    if (evidence.some(item => item.path === span.file.path && item.start_line <= span.start && item.end_line >= span.end)) continue;
    evidence.push({evidence_id: `E${evidence.length + 1}`, path: span.file.path, start_line: span.start, end_line: span.end, source: text});
  }
  return [...evidence.map(item => JSON.stringify(item)).join("\n")].length <= 9000;
}
async function replaceManualSource() {
  const job = answerJob(), scope = validReadingScope(job), ranges = readingScopeRanges(scope);
  if ($("replace-source").disabled || !ranges || ranges.length > 3) return;
  const view = {jobId: job.id, selectedHistory: state.selectedHistory, pending: true, note: "正在核對本題完整原碼與手動選段預算；尚未取代，也不會提問…"};
  const project = state.project, focus = state.focus, focusKey = JSON.stringify(focus), contextRequest = state.contextRequest;
  const question = $("question").value, questionRevision = state.questionRevision, continuation = state.continuation, sourceRequest = state.sourceRequest;
  const currentJob = state.job?.id, trial = state.experiment?.job?.id, model = state.model?.id, historyRequest = state.historyRequest, scopeKey = JSON.stringify(scope);
  const current = () => state.sourceRecovery === view && state.project === project && state.selectedHistory === view.selectedHistory && answerJob()?.id === job.id &&
    JSON.stringify(validReadingScope(answerJob())) === scopeKey && state.historyRequest === historyRequest && state.focus === focus && JSON.stringify(state.focus) === focusKey && state.contextRequest === contextRequest &&
    $("question").value === question && state.questionRevision === questionRevision && state.continuation === continuation && state.sourceRequest === sourceRequest &&
    state.job?.id === currentJob && state.experiment?.job?.id === trial && state.model?.id === model && !active() && !experimentBusy() && !state.refreshing && !state.pairPending && !modelMutationBlocked();
  state.sourceRecovery = view; showError(""); controls();
  const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 10000);
  try {
    const sources = await Promise.all(ranges.map(span => api(`/api/source?${new URLSearchParams({file: span.file.id, version: project.version})}`, undefined, controller.signal)));
    if (!current()) { view.note = "閱讀狀態或草稿已變動，未採用過期讀取；原有選段全部保留。"; return; }
    if (!recoveredSourceFits(ranges, sources)) throw new Error("顯示原碼的保守預算檢查超過每段 4,000／共用 JSON 9,000 字元；跳脫顯示可能多計，請手動縮小選段。未取代任何選段。");
    state.focus = ranges.map(({file, start, end}) => ({file: file.id, path: file.path, start, end}));
    state.continuation = null; state.continuationNotice = "已切換到本題原碼的手動選段；問題草稿未修改，尚未送出。";
    view.note = "已取代全部手動選段，保留本題完整範圍與順序。可查看名稱／匯入來源後手動補足；問題草稿與問答紀錄未變動，尚未提問。來源預算檢查不含新問題與提示標頭，送出時仍完整檢查。";
    $("question-editor").open = true; renderSelections();
  } catch (error) {
    if (current()) { view.note = "原碼核對未完成；未取代任何選段、修改草稿或自動重試。"; showError(controller.signal.aborted ? "原碼讀取超過 10 秒；原有選段全部保留，可稍後自行重試。" : error.message); }
    else view.note = "閱讀狀態或草稿已變動，未採用過期讀取；原有選段全部保留。";
  }
  finally { clearTimeout(timeout); controller.abort(); if (state.sourceRecovery === view) { view.pending = false; controls(); } }
}
$("replace-source").addEventListener("click", replaceManualSource);
function renderContinuation() {
  const view = state.continuation;
  const identity = JSON.stringify([view, state.continuationNotice]);
  $("selections").dataset.continuation = String(Boolean(view));
  $("selection-count").textContent = view ? `沿用 ${view.scope.focus.length} 段 · 手動 ${state.focus.length} 段未送出` : `${state.focus.length} / 3 段`;
  if (identity === continuationRendered) return;
  continuationRendered = identity;
  $("continuation-panel").hidden = !view;
  $("continuation-note").textContent = state.continuationNotice;
  $("continuation-note").hidden = !state.continuationNotice;
  $("continuation-ranges").replaceChildren();
  $("continuation-parent").textContent = view ? `前題：${view.parent_question}` : "";
  if (!view) return;
  for (const {file, start, end} of readingScopeRanges(view.scope) || []) {
    const button = element("button", `${file.path} · L${start}–L${end}`, "definition-button");
    button.type = "button";
    button.addEventListener("click", () => {
      if (state.continuation !== view || state.project?.version !== view.version || state.pending || state.refreshing) return;
      return openFile(file, view.version, start, end);
    });
    $("continuation-ranges").append(button);
  }
  if (view.scope.supplements.length) $("continuation-ranges").append(element("p", "以上範圍保留前題同檔補充；同名宣告仍不代表已解析綁定。", "muted"));
}
function selectContinuation() {
  const job = answerJob(), scope = validReadingScope(job);
  if ($("continue-source").disabled || active() || experimentBusy() || state.refreshing || state.pairPending || !scope) return;
  state.sourceRecovery = null;
  state.continuation = {parent_id: job.id, parent_question: job.question, version: job.version,
    scope: {focus: [...scope.focus], origin: scope.origin, supplements: [...scope.supplements]}};
  state.continuationNotice = "";
  $("question-editor").open = true;
  controls();
}
function clearContinuation(note = "") {
  state.continuation = null; state.continuationNotice = note; state.sourceRecovery = null; controls();
}
$("continue-source").addEventListener("click", selectContinuation);
$("continuation-clear").addEventListener("click", () => {
  if (!$("continuation-clear").disabled) clearContinuation("已回到原有手動選段；問題草稿未修改。");
});
async function loadHistory() {
  const project = state.project, request = ++state.historyRequest;
  if (browseOnly() || !project || state.refreshing) return;
  try {
    const value = await jobApi("/api/history");
    if (request !== state.historyRequest || state.project !== project || state.refreshing) return;
    if (!value || value.version !== project.version || !Array.isArray(value.entries) || value.entries.length > 5 ||
        !Number.isSafeInteger(value.evicted) || value.evicted < 0 || new Set(value.entries.map(job => job?.id)).size !== value.entries.length ||
        value.entries.some(job => !job || typeof job.id !== "string" || !job.id || !terminal(job) || job.version !== project.version ||
          typeof job.question !== "string" || [...job.question].length > 2000 || !Number.isFinite(job.elapsed_seconds) || job.elapsed_seconds < 0 ||
          Object.hasOwn(job, "preview") || Object.hasOwn(job, "rejected_preview"))) throw new Error("閱讀紀錄版本或內容不符；未載入舊紀錄。");
    state.history = value.entries; state.historyEvicted = value.evicted; state.historyError = "";
    if (state.continuation && !state.history.some(job => job.id === state.continuation.parent_id && validReadingScope(job))) {
      clearContinuation("前題已移出暫存或來源範圍不可沿用；已回到手動選段，問題草稿未修改。");
    }
    if (state.selectedHistory && !state.history.some(job => job.id === state.selectedHistory)) {
      state.selectedHistory = null; state.rendered = ""; draft = null;
      state.historyBoundary = "先前查看的紀錄已移出暫存，已返回目前工作。";
      $("answer").replaceChildren();
    }
    renderHistory(); renderViewedAnswer(); controls();
  } catch (error) {
    if (request !== state.historyRequest || state.project !== project || state.refreshing) return;
    state.historyError = `${error.message} 目前工作不受影響；可重讀紀錄。`; renderHistory();
  }
}
function renderViewedAnswer() {
  const job = answerJob();
  if (!job) return;
  if (state.selectedHistory === null) renderDraft(job);
  // Terminal payload changes must update the displayed answer too, not only export eligibility.
  const identity = terminal(job) ? JSON.stringify([state.selectedHistory, job]) : "";
  if (terminal(job) && state.rendered !== identity) {
    state.rendered = identity;
    const editorOpen = $("question-editor").open;
    if (state.selectedHistory === null) $("question-editor").open = unverifiedProse(job) === null && (job.status !== "answered" || !job.result?.outcome?.answer?.claims?.length);
    renderAnswer(job);
    if (state.selectedHistory !== null) $("question-editor").open = editorOpen;
  }
}
$("history-select").addEventListener("change", () => {
  invalidateReadingNote();
  if (state.refreshing) return;
  const id = $("history-select").value;
  if (id && !state.history.some(job => job.id === id && job.version === state.project?.version)) return;
  state.selectedHistory = id || null; state.sourceRecovery = null; state.rendered = ""; draft = null;
  $("answer").replaceChildren(element("p", id ? "正在開啟先前問題…" : "正在查看目前工作；進度顯示在上方。", "muted"));
  renderHistory(); renderViewedAnswer(); controls();
});
$("history-retry").addEventListener("click", loadHistory);
function renderFiles() {
  const query = $("file-filter").value.toLocaleLowerCase(), project = state.project;
  const files = (state.project?.files || []).filter(file => (!inChanges() || file.path.startsWith("before/") || file.path.startsWith("after/")) && file.path.toLocaleLowerCase().includes(query));
  $("file-count").textContent = `${files.length} 個檔案`;
  const fragment = document.createDocumentFragment();
  for (const file of files) {
    const parts = file.path.split("/");
    const button = element("button", "", `file-button${state.source?.file.id === file.id ? " active" : ""}`);
    button.title = sourceLabel(file.path); button.type = "button";
    button.append(element("strong", parts.pop()), element("small", inChanges() ? sourceLabel(file.path) : parts.join("/") || "專案根目錄"));
    button.addEventListener("click", () => { if (state.project === project && !state.refreshing) return openFile(file, project.version); });
    fragment.append(button);
  }
  if (!files.length) fragment.append(element("p", "沒有符合的檔案。", "muted"));
  $("files").replaceChildren(fragment);
}
function renderCode() {
  const source = state.source;
  if (!source) return;
  const first = state.page * pageSize, last = Math.min(first + pageSize, source.lines.length);
  const fragment = document.createDocumentFragment();
  for (let index = first; index < last; index++) {
    const number = index + 1, row = element("div", "", "code-row");
    row.dataset.line = number;
    const gutter = element("button", String(number), "line-number");
    gutter.type = "button"; gutter.setAttribute("aria-label", `選取第 ${number} 行；按住 Shift 延伸範圍`);
    gutter.addEventListener("click", event => {
      if (active() || state.refreshing) return;
      state.callSelectionRevision++;
      state.rangeHint = "";
      if (!(event.shiftKey || state.extending) || state.anchor === null) state.anchor = number;
      state.extending = false; state.end = number; highlightRange(); controls();
    });
    row.append(gutter, element("code", source.lines[index])); fragment.append(row);
  }
  if (!source.lines.length) fragment.append(element("p", "這是一個空檔案，沒有可選取的行。", "empty-state"));
  $("code").replaceChildren(fragment);
  $("page-info").textContent = source.lines.length ? `${first + 1}–${last} / ${source.lines.length}` : "0 行";
  $("page-prev").disabled = first === 0; $("page-next").disabled = last >= source.lines.length;
  highlightRange();
}
function highlightRange() {
  $("code").querySelectorAll("[data-line]").forEach(row => {
    const line = Number(row.dataset.line);
    row.classList.toggle("selected", state.anchor !== null && line >= Math.min(state.anchor, state.end) && line <= Math.max(state.anchor, state.end));
  });
}
function clearOutline() {
  $("outline").hidden = true; $("outline").open = false; $("outline-filter").value = "";
  $("outline-list").replaceChildren(); $("outline-note").textContent = "";
}
function containingDefinition() {
  const source = state.source, first = Math.min(state.anchor, state.end), last = Math.max(state.anchor, state.end);
  const unavailable = note => ({target: null, note});
  if (!source || source.version !== state.project?.version || !Number.isSafeInteger(state.anchor) || !Number.isSafeInteger(state.end) || first < 1 || last > source.lines.length) return unavailable("先選取原碼行；可擴展到包含整段的完整定義。");
  const outline = source.outline;
  if (outline?.status !== "available" || !Array.isArray(outline.items)) return unavailable("此檔案沒有可用的定義清單；請手動選段。");
  const candidates = []; let exact = 0;
  for (const item of outline.items) {
    if (!item || typeof item.name !== "string" || !Object.hasOwn(definitionKinds, item.kind) || typeof item.stub !== "boolean" ||
        ![item.start_line, item.definition_line, item.end_line].every(Number.isSafeInteger) ||
        !(1 <= item.start_line && item.start_line <= item.definition_line && item.definition_line <= item.end_line && item.end_line <= source.lines.length)) return unavailable("定義座標無效；保留目前選取，請手動確認原碼。");
    if (item.start_line <= first && item.end_line >= last) {
      if (item.start_line === first && item.end_line === last) exact++;
      else candidates.push(item);
    }
  }
  if (exact > 1) return unavailable("相同範圍有多個定義，無法唯一選取；請查看定義清單。");
  if (!candidates.length) return unavailable(exact ? "已選取完整定義，沒有可再擴展的外層定義。" : "沒有包含整段選取的定義；保留目前範圍。");
  const size = Math.min(...candidates.map(item => item.end_line - item.start_line + 1));
  const nearest = candidates.filter(item => item.end_line - item.start_line + 1 === size);
  if (nearest.length !== 1) return unavailable("有多個同樣大小的包含定義，無法唯一選取；請查看定義清單。");
  const target = nearest[0];
  const packing = !inChanges() && !browseOnly() ? packSourceRange(source.lines, target.start_line, target.end_line, 3 - state.focus.length) : null;
  const available = size <= 80 || Boolean(packing?.chunks.length);
  return {target, available, note: `${definitionKinds[target.kind]} ${target.name} · L${target.start_line}–L${target.end_line}（${size} 行）${target.stub ? " · 省略內容" : ""}。${!available ? packing?.note || "超過 80 行，未裁切或更動選取；請手動縮小範圍。" : browseOnly() ? "只擴展反白範圍；要查看名稱來源，請明確加入選段。不代表完整依賴。" : `只擴展選取，不加入提問；不代表完整依賴。${packing?.chunks.length > 1 ? `加入時使用 ${packing.chunks.length} 段，包含全部原碼行。` : ""}`}`};
}
function rememberReadingPosition() {
  const source = state.source;
  if (!source) return;
  const position = { file: { id: source.file.id, path: source.file.path, lines: source.file.lines }, version: source.version, page: state.page, anchor: state.anchor, end: state.end, rangeHint: state.rangeHint, scrollTop: $("code").scrollTop, scrollLeft: $("code").scrollLeft };
  if (JSON.stringify(state.readingTrail.at(-1)) !== JSON.stringify(position)) state.readingTrail.push(position);
  if (state.readingTrail.length > 40) state.readingTrail.shift();
}
function selectDefinition(item, source, remember = true) {
  if (!source || state.source !== source || source.version !== state.project?.version || state.pending || state.refreshing) return;
  state.callSelectionRevision++;
  const size = item.end_line - item.start_line + 1;
  const packing = !inChanges() && !browseOnly() ? packSourceRange(source.lines, item.start_line, item.end_line, 3 - state.focus.length) : null;
  const selectable = size <= 80 || Boolean(packing?.chunks.length);
  const rangeHint = selectable ? "" : `${item.name}：L${item.start_line}–L${item.end_line}，共 ${size} 行；${packing?.note || "請自行選取 80 行內的範圍。"}`;
  if (remember) rememberReadingPosition();
  state.extending = false; state.page = Math.floor((item.start_line - 1) / pageSize);
  state.anchor = selectable ? item.start_line : null; state.end = selectable ? item.end_line : null;
  state.rangeHint = rangeHint;
  $("outline").open = false; renderCode(); controls();
  $("code").querySelector(`[data-line="${item.start_line}"]`)?.scrollIntoView({ block: "center" });
}
function renderOutline() {
  const source = state.source, outline = source?.outline;
  if (!outline || outline.status === "unsupported") { clearOutline(); return; }
  const items = outline.items || [], query = $("outline-filter").value.toLocaleLowerCase();
  const selected = items.filter(item => item.name.toLocaleLowerCase().includes(query));
  $("outline").hidden = false;
  $("outline-summary").textContent = `此檔案的定義（${selected.length}${query ? "/" + items.length : ""}）`;
  $("outline-filter").hidden = $("outline-filter-label").hidden = !items.length;
  $("outline-note").textContent = outline.status === "limited" ? "定義清單超過顯示上限，請改用左側文字搜尋。" : outline.status === "unavailable" ? ({ syntax_or_version: "無法解析此 Python 語法或版本；仍可閱讀與文字搜尋。", line_separators: "換行格式無法精確對應定義行號，請使用文字搜尋。", parse_depth: "定義結構過深，請改用文字搜尋。" }[outline.reason] || "無法建立定義清單；仍可閱讀與文字搜尋。") : items.length ? "原碼中的定義位置，不代表實際呼叫關係或執行時的名稱綁定。" : "沒有可列出的定義；可使用左側文字搜尋。";
  const fragment = document.createDocumentFragment();
  for (const item of selected) {
    const button = element("button", "", "definition-button"); button.type = "button";
    button.dataset.definitionLine = item.definition_line; button.dataset.startLine = item.start_line; button.dataset.endLine = item.end_line;
    button.append(element("strong", item.name), element("small", `${definitionKinds[item.kind] || item.kind} · 定義 L${item.definition_line} · L${item.start_line}–L${item.end_line}（${item.end_line - item.start_line + 1} 行）${item.stub ? " · 省略內容" : ""}`));
    button.addEventListener("click", () => selectDefinition(item, source));
    const row = element("div", "", "definition-row"), name = item.name.split(".").at(-1);
    row.append(button);
    if (source.version === state.project?.version && /\.pyi?$/i.test(source.path) && Object.hasOwn(definitionKinds, item.kind) && validCallQuery(name)) {
      const calls = element("button", "找同名呼叫", "quiet definition-experiment definition-calls"); calls.type = "button"; calls.dataset.callQuery = name;
      calls.setAttribute("aria-label", `找同名呼叫：${name}（專案內相同拼寫，不解析實際綁定）`);
      calls.addEventListener("click", () => {
        if (calls.disabled || state.source !== source || source.version !== state.project?.version || state.pending || state.refreshing) return;
        $("search-mode").value = "calls"; searchModeChanged(); $("search-query").value = name; return searchSource(true);
      });
      row.append(calls);
    }
    if (experimentsEnabled() && source.version === state.project.version && source.path.endsWith(".py") && (!inChanges() || source.path.startsWith("after/")) &&
        item.kind === "function" && !item.name.startsWith("__") && /^[_\p{XID_Start}][_\p{XID_Continue}]*$/u.test(item.name)) {
      const experiment = element("button", inChanges() ? "同一輸入，比較兩版…" : "試一組輸入…", "quiet definition-experiment");
      experiment.type = "button"; experiment.dataset.experimentEntry = item.name;
      experiment.setAttribute("aria-label", `${inChanges() ? "同一輸入，比較 HEAD／目前兩版" : "試一組輸入"}：${item.name}（先確認完整模組，不會立即執行）`);
      experiment.addEventListener("click", () => { if (!experiment.disabled) return openExperiment(item, source); });
      row.append(experiment);
    }
    fragment.append(row);
  }
  if (items.length && !selected.length) fragment.append(element("p", "沒有符合的定義名稱。", "muted"));
  $("outline-list").replaceChildren(fragment);
  experimentControls();
}
async function openFile(file, version, start = null, end = start, restore = null, prepared = null, rangeHint = "") {
  if (state.experiment?.preparing) {
    state.experiment.prepareRequest++; state.experiment.preparing = false; state.experiment.visible = false;
    renderExperiment();
  }
  const request = ++state.sourceRequest;
  if (!restore) rememberReadingPosition();
  state.source = null; state.anchor = state.end = null; state.extending = false; state.rangeHint = ""; clearOutline();
  $("source-title").textContent = file.path;
  $("source-version").textContent = "讀取中…";
  $("code").replaceChildren(element("p", "正在讀取唯讀快照…", "empty-state")); controls();
  try {
    const source = prepared || await api(`/api/source?${new URLSearchParams({ file: file.id, version })}`);
    if (request !== state.sourceRequest) return;
    if (source.version !== version || source.path !== file.path || !Array.isArray(source.lines)) throw new Error("原碼版本或路徑不一致，請重新讀取專案。");
    state.source = { ...source, file }; state.page = restore ? restore.page : start ? Math.floor((start - 1) / pageSize) : 0;
    state.anchor = restore ? restore.anchor : rangeHint ? null : start; state.end = restore ? restore.end : rangeHint ? null : end; state.rangeHint = restore ? restore.rangeHint : rangeHint;
    $("source-version").textContent = inChanges() ? sourceLabel(file.path) : version === state.project.version ? "唯讀快照" : "本題歷史快照";
    renderCode(); renderFiles(); renderOutline(); controls();
    if (restore) {
      $("source-title").scrollIntoView({ block: "nearest" });
      $("code").scrollTop = restore.scrollTop; $("code").scrollLeft = restore.scrollLeft;
    } else if (start) $("code").querySelector(`[data-line="${Number(start)}"]`)?.scrollIntoView({ block: "center" });
    return state.source;
  } catch (error) { if (request === state.sourceRequest) { showError(error.message); $("source-version").textContent = "讀取失敗"; controls(); } }
}
function clearSourceContext() {
  state.contextRequest++; state.context = null;
  $("source-context").hidden = true; $("context-status").textContent = ""; $("context-results").replaceChildren();
}
function currentContext(view) {
  return state.context === view && state.contextRequest === view.request && state.project === view.project &&
    state.project.version === view.version && state.focus.includes(view.selection) && !state.refreshing;
}
function validSourceContext(result, view) {
  const {selection, file, version} = view;
  const shape = (value, keys) => value && typeof value === "object" && !Array.isArray(value) &&
    Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
  const text = value => typeof value === "string" && value.length > 0 && value.length <= 65536;
  const reason = value => value === null || typeof value === "string" && value.length <= 65536;
  const uses = lines => Array.isArray(lines) && lines.length > 0 && lines.length <= 80 && lines.every((line, index) =>
    Number.isSafeInteger(line) && line >= selection.start && line <= selection.end && (index === 0 || line > lines[index - 1]));
  if (!shape(result, ["file", "path", "version", "start", "end", "status", "reason", "scope", "semantics_verified", "bindings", "candidates"]) ||
      result.file !== selection.file || result.path !== selection.path || result.version !== version || result.start !== selection.start || result.end !== selection.end ||
      result.scope !== "same-file lexical scopes" || result.semantics_verified !== false || !["available", "limited", "unavailable", "unsupported"].includes(result.status) ||
      !reason(result.reason) || !Array.isArray(result.bindings) || result.bindings.length > 64 || !Array.isArray(result.candidates) || result.candidates.length > 64 ||
      (result.status !== "available" && (result.bindings.length || result.candidates.length))) return false;
  const bindings = new Map();
  for (const binding of result.bindings) {
    if (!shape(binding, ["id", "name", "classification", "scope", "scope_line", "use_lines", "reason"]) ||
        typeof binding.id !== "string" || !/^B(?:[1-9]|[1-5][0-9]|6[0-4])$/.test(binding.id) || bindings.has(binding.id) || !text(binding.name) ||
        typeof binding.classification !== "string" || !Object.hasOwn(contextClassifications, binding.classification) || !text(binding.scope) ||
        !Number.isSafeInteger(binding.scope_line) || binding.scope_line < 1 || binding.scope_line > file.lines || !uses(binding.use_lines) || !reason(binding.reason)) return false;
    bindings.set(binding.id, binding);
  }
  for (const candidate of result.candidates) {
    if (!shape(candidate, ["binding_id", "name", "kind", "start_line", "end_line", "use_lines", "conditional"])) return false;
    const binding = bindings.get(candidate.binding_id);
    if (!binding || binding.classification === "unresolved" || candidate.name !== binding.name || typeof candidate.kind !== "string" ||
        !Object.hasOwn(contextKinds, candidate.kind) || typeof candidate.conditional !== "boolean" ||
        !Number.isSafeInteger(candidate.start_line) || !Number.isSafeInteger(candidate.end_line) || candidate.start_line < 1 ||
        candidate.end_line < candidate.start_line || candidate.end_line > file.lines || !uses(candidate.use_lines) ||
        JSON.stringify(candidate.use_lines) !== JSON.stringify(binding.use_lines)) return false;
  }
  const metadata = {status: result.status, reason: result.reason, scope: result.scope, semantics_verified: false, bindings: result.bindings, candidates: result.candidates};
  return new TextEncoder().encode(JSON.stringify(metadata)).length <= 65536;
}
async function inspectSourceContext(selection) {
  if (active() || experimentBusy() || state.refreshing || state.pairPending || !state.focus.includes(selection)) return;
  clearSourceContext(); showError("");
  const project = state.project, file = project.files.find(file => file.id === selection.file && file.path === selection.path);
  if (!file) { showError("選段不在目前快照內；原有選段已保留，請重新讀取專案。"); return; }
  const view = {project, version: project.version, file, selection, request: state.contextRequest, pending: true};
  const label = `${inChanges() ? changeRoleLabels[selection.role] + " · " : ""}${sourceLabel(selection.path, project.comparison)} · L${selection.start}–L${selection.end}`;
  state.context = view; $("source-context").hidden = false;
  $("context-status").textContent = `${label}：正在依作用域檢查同檔名稱來源，不啟動模型…`; controls();
  try {
    const result = await api("/api/context", {file: selection.file, version: view.version, start: selection.start, end: selection.end});
    if (!currentContext(view)) return;
    if (!validSourceContext(result, view)) throw new Error("名稱來源的版本或座標不一致；未顯示候選，原有選段已保留。");
    const explanations = {language: "目前只支援 Python 的靜態宣告導航。", utf8: "原碼無法作為 UTF-8 解析。", line_separators: "換行格式無法精確對應宣告行號。", parse_depth: "語法結構過深，無法列出宣告。", syntax_or_version: "無法解析此 Python 語法或版本。"};
    const unknown = result.bindings.filter(binding => binding.classification === "unresolved").length;
    const summary = result.status === "available" ? result.bindings.length ? `依使用作用域列出 ${result.bindings.length} 組名稱、${result.candidates.length} 項宣告${unknown ? `；${unknown} 組來源未確定` : ""}。` : "選段中沒有可列出的名稱讀取；不代表上下文完整或沒有外部依賴。" :
      result.status === "limited" ? "清單超過靜態檢查上限，未提供部分清單；不代表沒有其他宣告。" : Object.hasOwn(explanations, result.reason) ? explanations[result.reason] : "無法列出同檔宣告；仍可手動查看原碼。";
    view.note = `${label}。${summary}`;
    view.candidates = result.candidates;
    $("context-status").textContent = view.note;
    for (const binding of result.bindings) {
      const group = element("section", "", "context-binding"), candidates = result.candidates.filter(candidate => candidate.binding_id === binding.id);
      group.dataset.bindingId = binding.id;
      group.append(element("h4", `${binding.name} · ${contextClassifications[binding.classification]}`),
        element("p", `讀取作用域：${binding.scope === "<module>" ? "模組" : binding.scope} · L${binding.scope_line} · 選段讀取位置 ${binding.use_lines.map(line => `L${line}`).join("、")}`, "muted"));
      if (binding.classification === "annotation") group.append(element("p", "這是註記中的潛在名稱來源，求值時機與實際綁定未確認。", "muted"));
      if (binding.reason !== null) group.append(element("p", Object.hasOwn(contextReasons, binding.reason) ? contextReasons[binding.reason] : "此名稱仍有靜態分析限制；不推測實際值或生效的宣告。", "muted"));
      if (!candidates.length) group.append(element("p", "沒有可安全導航的同檔宣告；不代表名稱不存在，也不會改用其他作用域的同名宣告。", "muted"));
      if (candidates.length > 1) group.append(element("p", "此讀取作用域有多個同名宣告，全部保留；不判定先後賦值或哪個宣告會生效。", "muted"));
      for (const candidate of candidates) {
        const row = element("div", "", "context-candidate"), inside = candidate.start_line >= selection.start && candidate.end_line <= selection.end;
        let importRoutes = null;
        row.append(element("p", `${contextKinds[candidate.kind]} · L${candidate.start_line}–L${candidate.end_line}${inside ? " · 已在選段內" : ""}${candidate.conditional ? " · 條件／控制流程中的宣告，未證明會執行" : ""}${["import", "from import"].includes(candidate.kind) ? " · 僅為匯入宣告，不是目標實作" : ""}`, "muted"));
        const start = Math.min(selection.start, candidate.start_line), end = Math.max(selection.end, candidate.end_line);
        const browse = element("button", "查看", "quiet"); browse.type = "button"; browse.dataset.available = "true";
        browse.setAttribute("aria-label", `查看 ${candidate.name} · ${sourceLabel(selection.path, project.comparison)} L${candidate.start_line}–L${candidate.end_line}`);
        browse.addEventListener("click", () => { if (!browse.disabled) return contextSourceAction(view, candidate, false); }); row.append(browse);
        if (["import", "from import"].includes(candidate.kind)) {
          const trace = element("button", "追蹤匯入來源", "quiet"), routes = element("div", "", "import-routes");
          trace.type = "button"; trace.dataset.available = "true"; routes.hidden = true;
          trace.setAttribute("aria-label", `追蹤 ${candidate.name} 的匯入來源 · ${sourceLabel(selection.path, project.comparison)} L${candidate.start_line}–L${candidate.end_line}（不啟動模型）`);
          trace.addEventListener("click", () => { if (!trace.disabled) return inspectImportSource(view, candidate, routes); });
          row.append(trace); importRoutes = routes;
        }
        if (!inside) {
          const expand = element("button", `擴展此選段至 L${start}–L${end}`, "quiet"); expand.type = "button"; expand.dataset.available = String(end - start < 80);
          expand.title = browseOnly() ? "只擴展這一段並保留其他選段，包含中間所有行；不執行原碼。" : "只擴展這一段並保留其他選段；包括中間所有行，不自動啟動模型。共用預算仍在載入模型前檢查。";
          expand.addEventListener("click", () => { if (!expand.disabled) return contextSourceAction(view, candidate, true); }); row.append(expand);
          if (end - start >= 80) row.append(element("p", "與原選段合併後超過 80 行；只能查看，不裁切或自動加入。", "muted"));
        }
        if (importRoutes) row.append(importRoutes);
        group.append(row);
      }
      $("context-results").append(group);
    }
  } catch (error) {
    if (currentContext(view)) { $("context-results").replaceChildren(); $("context-status").textContent = `${label}：檢查未完成；選段、問題與問答紀錄未變動，沒有啟動模型。`; showError(error.message); }
  } finally { if (currentContext(view)) { view.pending = false; controls(); } }
}
async function contextSourceAction(view, candidate, expand) {
  if (!currentContext(view) || view.pending || active() || experimentBusy() || state.pairPending) return;
  const {selection, file, version} = view, start = Math.min(selection.start, candidate.start_line), end = Math.max(selection.end, candidate.end_line);
  if (expand && start === selection.start && end === selection.end) return;
  if (expand && end - start >= 80) { showError("合併後超過 80 行；原有選段全部保留。"); return; }
  const sourceRequest = state.sourceRequest;
  view.pending = true; showError(""); controls();
  $("context-status").textContent = `${view.note} 正在確認原碼${expand ? "與合併範圍" : ""}，尚未更動選段…`;
  try {
    const source = await api(`/api/source?${new URLSearchParams({file: file.id, version})}`);
    if (!currentContext(view) || active() || experimentBusy() || state.pairPending || (!expand && sourceRequest !== state.sourceRequest)) return;
    if (source.version !== version || source.path !== file.path || !Array.isArray(source.lines) || source.lines.length !== file.lines || source.lines.some(line => typeof line !== "string") ||
        candidate.end_line > source.lines.length || end > source.lines.length) throw new Error("宣告原碼版本、路徑或範圍不一致；原有選段全部保留。");
    if (expand) {
      if (selectedCharacters(source, start, end) > 4000) throw new Error("合併後超過每段 4,000 字元；原有選段全部保留，請手動調整。");
      if (state.focus.some(item => item !== selection && item.file === selection.file && item.start === start && item.end === end)) throw new Error("擴展後會與另一選段完全重複；原有選段全部保留，請手動調整。");
      state.focus = state.focus.map(item => item === selection ? {...selection, start, end} : item);
      renderSelections(); $("source-context").hidden = false;
      $("context-status").textContent = `已將 ${sourceLabel(selection.path, view.project.comparison)} 的${selection.role ? changeRoleLabels[selection.role] : "這一選段"}擴展為 L${start}–L${end}，包含中間所有行；${browseOnly() ? "其他選段與 traceback 原稿未變動，不執行原碼。可重新查看名稱來源；不代表完整依賴。" : "其他選段與問題未變動，未啟動模型。共用預算仍在模型載入前檢查；需要時可重新查看名稱來源，這不代表上下文已完整。"}`;
    } else await openFile(file, version, candidate.start_line, candidate.end_line, null, source);
  } catch (error) { if (currentContext(view)) showError(error.message); }
  finally { if (currentContext(view)) { view.pending = false; $("context-status").textContent = view.note; controls(); } }
}
function importContextReady(view, candidate) {
  return currentContext(view) && view.candidates?.includes(candidate) && !active() && !experimentBusy() && !state.pairPending;
}
function validImportSource(result, view, candidate, request) {
  const shape = (value, keys) => value && typeof value === "object" && !Array.isArray(value) &&
    Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
  const text = value => typeof value === "string" && value.length > 0 && value.length <= 65536;
  const reason = value => value === null || typeof value === "string" && value.length <= 65536;
  if (!shape(result, [...Object.keys(request), "path", "scope", "semantics_verified", "status", "reason", "routes"]) ||
      Object.keys(request).some(key => result[key] !== request[key]) || result.path !== view.file.path ||
      result.scope !== "snapshot import candidates" || result.semantics_verified !== false ||
      !["available", "unavailable", "limited", "unsupported"].includes(result.status) || !reason(result.reason) ||
      !Array.isArray(result.routes) || result.routes.length > 32 || (result.status !== "available" && result.routes.length)) return false;
  const side = path => path.startsWith("before/") ? "before" : path.startsWith("after/") ? "after" : null;
  for (const route of result.routes) {
    if (!shape(route, ["outcome", "reason", "steps"]) || !["declaration", "module", "unresolved", "cycle"].includes(route.outcome) ||
        !reason(route.reason) || !Array.isArray(route.steps) || !route.steps.length || route.steps.length > 8) return false;
    for (const step of route.steps) {
      if (!shape(step, ["file", "path", "name", "kind", "start_line", "end_line", "conditional"])) return false;
      const file = view.project.files.find(file => file.id === step.file && file.path === step.path);
      if (!file || !text(step.name) || typeof step.kind !== "string" || !(step.kind === "module" || Object.hasOwn(contextKinds, step.kind)) ||
          typeof step.conditional !== "boolean" || (view.project.comparison && (!side(step.path) || side(step.path) !== side(view.file.path)))) return false;
      if (step.kind === "module") { if (step.start_line !== null || step.end_line !== null) return false; }
      else if (!Number.isSafeInteger(step.start_line) || !Number.isSafeInteger(step.end_line) || step.start_line < 1 || step.end_line < step.start_line || step.end_line > file.lines) return false;
    }
    const origin = route.steps[0], last = route.steps.at(-1);
    if (origin.file !== view.file.id || origin.path !== view.file.path || ["name", "kind", "start_line", "end_line", "conditional"].some(key => origin[key] !== candidate[key]) ||
        (route.outcome === "module" && last.kind !== "module") || (route.outcome === "declaration" && ["module", "import", "from import"].includes(last.kind))) return false;
  }
  return new TextEncoder().encode(JSON.stringify(result)).length <= 131072;
}
async function inspectImportSource(view, candidate, box) {
  if (!importContextReady(view, candidate) || view.pending || !["import", "from import"].includes(candidate.kind)) return;
  const {selection} = view, request = {file: selection.file, version: view.version, start: selection.start, end: selection.end,
    binding_id: candidate.binding_id, declaration_start: candidate.start_line, declaration_end: candidate.end_line};
  const sourceRequest = state.sourceRequest;
  view.pending = true; box.hidden = false; box.replaceChildren(element("p", "正在追蹤快照內的匯入候選，不啟動模型…", "muted")); controls(); showError("");
  let finished = false;
  try {
    const result = await api("/api/import-source", request);
    if (!importContextReady(view, candidate) || sourceRequest !== state.sourceRequest) return;
    if (!validImportSource(result, view, candidate, request)) throw new Error("匯入來源的版本、路徑或座標不一致；未顯示候選，原有選段已保留。");
    box.replaceChildren(element("p", "只追蹤快照根目錄／src 常見實體檔案布局的候選；不證明 Python 匯入成功、搜尋順序或執行時綁定。不求值 __all__，不在主機匯入程式碼。只瀏覽，不自動加入選段或啟動模型。", "muted"));
    if (result.reason !== null) box.append(element("p", importSourceReason(result.reason), "muted"));
    if (!result.routes.length) box.append(element("p", result.status === "limited" ? "超過追蹤上限，未提供部分路徑；可手動查看宣告。" : "沒有可列出的匯入路徑；不代表模組不存在，仍可手動搜尋原碼。", "muted"));
    if (result.routes.length > 1) box.append(element("p", `共有 ${result.routes.length} 條候選路徑，全部保留；不挑選生效者。`, "muted"));
    const labels = {declaration: "宣告候選", module: "模組檔案候選", unresolved: "來源未確定", cycle: "循環匯入候選，已停止追蹤"};
    for (const [index, route] of result.routes.entries()) {
      const group = element("section", "", "import-route"); group.append(element("h5", `${index + 1} · ${labels[route.outcome]}`));
      if (route.reason !== null) group.append(element("p", importSourceReason(route.reason), "muted"));
      for (const [position, step] of route.steps.entries()) {
        const row = element("div", "", "import-step"), label = `${position + 1}. ${step.name} · ${sourceLabel(step.path, view.project.comparison)} · ${step.kind === "module" ? "整個模組檔案" : `${contextKinds[step.kind]} L${step.start_line}–L${step.end_line}`}`;
        row.append(element("p", `${label}${step.conditional ? " · 條件宣告，未證明會執行" : ""}`, "muted"));
        const button = element("button", "查看來源", "quiet"); button.type = "button"; button.dataset.available = "true";
        button.setAttribute("aria-label", `查看來源：${label}`);
        button.addEventListener("click", () => { if (!button.disabled) return importSourceAction(view, candidate, step); }); row.append(button); group.append(row);
      }
      box.append(group);
    }
    finished = true;
  } catch (error) {
    if (importContextReady(view, candidate) && sourceRequest === state.sourceRequest) { box.replaceChildren(element("p", "追蹤未完成；選段、問題與問答紀錄未變動，沒有啟動模型。", "muted")); showError(error.message); finished = true; }
  } finally {
    if (currentContext(view)) {
      if (!finished) box.replaceChildren(element("p", "閱讀狀態已變動；未採用過期回應，需要時請再次追蹤。", "muted"));
      view.pending = false; controls();
    }
  }
}
function importSourceReason(reason) {
  const explanations = {
    not_in_snapshot: "目標未包含在這一側的快照，無法導航；不代表 Python 環境沒有此模組。",
    name_not_declared: "目標檔案未找到可列出的名稱宣告；不推測執行時匯出的值。",
    namespace_package: "可能是命名空間套件，沒有對應的實體模組檔案可開啟。",
    relative_escape: "相對匯入超出這次快照可對應的套件範圍。",
    wildcard_import: "不展開星號匯入，也不求值 __all__；缺失的名稱保持未確定。",
    dynamic_exports: "有動態匯出，無法只靠靜態宣告確定名稱來源。",
    expression_alias_not_followed: "這裡是運算式賦值；不把它推測成另一個符號或實作。",
    submodule_candidate: "此路徑是子模組候選；不代表 Python 一定選用它。",
    imported_submodule_not_attribute_resolution: "此匯入綁定的是頂層名稱（例如 import pkg.tools 綁定 pkg）；只列出該名稱的模組候選，不解析使用時的屬性查找。",
    cycle: "遇到重複的匯入路徑，已停止追蹤；不推測執行時是否成功。",
    language: "目前只支援 Python 的靜態匯入導航。", syntax_or_version: "無法解析此 Python 語法或版本。",
    line_separators: "換行格式無法精確對應行號。", utf8: "原碼無法作為 UTF-8 解析。",
    source_unavailable: "無法取得完整的保留原碼，未使用其他來源代替。"
  };
  if (Object.hasOwn(explanations, reason)) return explanations[reason];
  if (["source_limit", "node_limit", "file_limit", "byte_limit", "route_limit", "step_limit", "metadata_limit"].includes(reason)) return "超過靜態追蹤的容量或路徑上限；未提供部分清單，也不推測省略的來源。";
  return `靜態追蹤仍有未確定或不支援的部分（${reason}）；不推測缺失的來源，也不以同名宣告代替。`;
}
async function importSourceAction(view, candidate, step) {
  if (!importContextReady(view, candidate) || view.pending) return;
  const file = view.project.files.find(file => file.id === step.file && file.path === step.path), sourceRequest = state.sourceRequest;
  if (!file) return;
  view.pending = true; controls(); showError("");
  try {
    const source = await api(`/api/source?${new URLSearchParams({file: file.id, version: view.version})}`);
    if (!importContextReady(view, candidate) || sourceRequest !== state.sourceRequest) return;
    if (source.version !== view.version || source.path !== file.path || !Array.isArray(source.lines) || source.lines.length !== file.lines || source.lines.some(line => typeof line !== "string") ||
        (step.end_line !== null && step.end_line > source.lines.length)) throw new Error("匯入候選的原碼版本、路徑或範圍不一致；原有選段全部保留。");
    const hint = step.end_line !== null && step.end_line - step.start_line >= 80 ? `${step.name}：L${step.start_line}–L${step.end_line}，超過 80 行；僅瀏覽，請自行選取較小範圍。` : "";
    await openFile(file, view.version, step.start_line, step.end_line, null, source, hint);
  } catch (error) { if (importContextReady(view, candidate) && sourceRequest === state.sourceRequest) showError(error.message); }
  finally { if (currentContext(view)) { view.pending = false; controls(); } }
}
function renderSelections() {
  clearSourceContext();
  $("selection-count").textContent = `${state.focus.length} / 3 段`;
  $("selections").replaceChildren();
  state.focus.forEach((selection, index) => {
    const chip = element("div", "", "selection-chip"), remove = element("button", "×");
    remove.type = "button"; remove.setAttribute("aria-label", `移除選段 ${selection.path} 第 ${selection.start} 至 ${selection.end} 行`);
    remove.addEventListener("click", () => { if (active() || state.refreshing || state.pairPending) return; state.focus.splice(index, 1); renderSelections(); });
    chip.append(element("span", `${inChanges() ? changeRoleLabels[selection.role] + " · " : ""}${selection.path}:${selection.start}–${selection.end}`));
    const inspect = element("button", "查看名稱來源（不啟動模型）", "selection-edit"); inspect.type = "button";
    inspect.addEventListener("click", () => { if (!inspect.disabled) return inspectSourceContext(selection); }); chip.append(inspect);
    if (inChanges()) {
      const project = state.project, edit = element("button", "調整", "selection-edit"); edit.type = "button";
      edit.setAttribute("aria-label", `調整${changeRoleLabels[selection.role]}，可包含周邊常數或設定`);
      edit.addEventListener("click", () => {
        if (active() || state.refreshing || state.pairPending || state.project !== project || !state.focus.includes(selection)) return;
        const file = project.files.find(item => item.id === selection.file && item.path === selection.path);
        $("change-slot").value = selection.role;
        if (file) return openFile(file, project.version, selection.start, selection.end);
      });
      chip.append(edit);
    }
    chip.append(remove); $("selections").append(chip);
  });
  if (!state.focus.length) $("selections").append(element("p", "先在原碼中選行，再加入選段。", "muted"));
  controls();
}
function observedPosition(project, call, line, label = `L${line}`) {
  const button = element("button", label, "quiet"); button.type = "button";
  button.dataset.callId = call.id; button.dataset.observedLine = line;
  button.title = `#${call.id} · ${call.path}:${line}`;
  button.addEventListener("click", () => {
    if (state.project !== project || state.refreshing) return;
    const file = project.files.find(item => item.id === call.file && item.path === call.path);
    if (file) return openFile(file, project.version, line);
  });
  return button;
}
function orderedPathDifference(left, right) {
  let shared = 0;
  while (shared < left.length && shared < right.length && left[shared] === right[shared]) shared++;
  return { shared, last: shared ? left[shared - 1] : null, left: left[shared] ?? null, right: right[shared] ?? null };
}
function renderObservedDifference() {
  const project = state.project, calls = project?.observation?.calls || [];
  const left = calls.find(call => String(call.id) === $("observed-call").value);
  const right = calls.find(call => String(call.id) === $("observed-compare").value);
  const box = $("observed-difference"); box.replaceChildren();
  if (!left || !right || left.id === right.id || left.path !== right.path || left.first_line !== right.first_line || left.function !== right.function) return;
  const result = orderedPathDifference(left.visits, right.visits);
  box.append(element("p", result.left === null && result.right === null ? `兩次行號序列相同（${result.shared} 次）；不代表輸入、結果或副作用相同。` : `前 ${result.shared} 次行號相同，接下來的位置不同；這不是分支原因的判定。`));
  for (const [label, call, line] of [["最後共同位置", left, result.last], [`#${left.id} 下一位置`, left, result.left], [`#${right.id} 下一位置`, right, result.right]]) {
    const row = element("div"); row.append(element("span", label));
    row.append(line === null ? element("span", label === "最後共同位置" ? "無" : "行號序列結束", "muted") : observedPosition(project, call, line)); box.append(row);
  }
}
function renderObservedCall() {
  const project = state.project, calls = project?.observation?.calls || [];
  const call = calls.find(item => String(item.id) === $("observed-call").value);
  $("observed-visits").replaceChildren(); $("observed-compare").replaceChildren(); $("observed-difference").replaceChildren();
  $("observed-parent").replaceChildren();
  $("observed-location").textContent = $("observed-result").textContent = "";
  if (!call) return;
  $("observed-location").textContent = `${call.path} · 定義 L${call.first_line} · ${call.parent === null ? "無選定父呼叫" : `父呼叫 #${call.parent}`}`;
  const parent = calls.find(item => item.id === call.parent);
  if (parent && Number.isInteger(call.parent_line) && call.parent_line > 0) {
    $("observed-parent").append(element("span", "選定父層的最近位置： ", "muted"), observedPosition(project, parent, call.parent_line, `${parent.path}:L${call.parent_line}`));
  }
  $("observed-result").textContent = `${call.visits.length} 次行號事件（依序，保留重複） · ${call.exceptions} 次例外事件 · ${call.returned ? "已離開函式（不代表成功）" : "未見離開事件"}`;
  $("observed-visits").append(...call.visits.map(line => observedPosition(project, call, line)));
  if (!call.visits.length) $("observed-visits").append(element("p", "本次沒有可定位的行號事件。", "muted"));
  const peers = calls.filter(item => item.id !== call.id && item.path === call.path && item.first_line === call.first_line && item.function === call.function);
  const none = element("option", peers.length ? "選擇另一次呼叫…" : "沒有同一函式的其他呼叫"); none.value = "";
  $("observed-compare").append(none, ...peers.map(item => { const option = element("option", `#${item.id} · ${item.visits.length} 次行號 · 父 ${item.parent ?? "無"}`); option.value = String(item.id); return option; }));
  $("observed-compare").value = ""; $("observed-compare").disabled = !peers.length;
}
function renderObservation() {
  if (inChanges()) { $("observation").hidden = true; return; } // A same-version trace cannot describe a two-version comparison.
  const observation = state.project?.observation, error = state.project?.observation_error;
  $("observation").hidden = !observation && !error; $("observation").open = Boolean(observation || error);
  $("observation-body").hidden = !observation; $("observed-call").replaceChildren();
  $("observation-status").textContent = observation ? `${observation.test} · Python ${observation.python} · 捕捉完整 · ${observation.test_passed ? "測試通過" : "測試未通過／跳過"} · ${observation.calls.length} 次呼叫` : error || "";
  if (observation && !observation.test_passed) {
    const labels = {failed: "失敗", errors: "錯誤", skipped: "跳過", expected_failures: "預期失敗", unexpected_successes: "非預期成功", subtests_failed: "子測試失敗", subtests_errors: "子測試錯誤"};
    $("observation-status").textContent += Object.entries(labels).filter(([key]) => observation.counts[key]).map(([key, label]) => ` · ${label} ${observation.counts[key]}`).join("");
  }
  if (observation) {
    for (const call of observation.calls) { const option = element("option", `#${call.id} · ${call.function}`); option.value = String(call.id); $("observed-call").append(option); }
    $("observed-call").value = observation.calls.length ? String(observation.calls[0].id) : "";
    $("observed-call").disabled = !observation.calls.length;
  }
  renderObservedCall();
}
$("observed-call").addEventListener("change", renderObservedCall);
$("observed-compare").addEventListener("change", renderObservedDifference);
function renderReadingMode() {
  const browsing = browseOnly();
  $("browse-only-notice").hidden = !browsing;
  $("reading-notice").hidden = browsing;
  $("ai-reading-results").hidden = $("ask").hidden = $("change-mode").hidden = browsing;
  $("ai-discovery-actions").hidden = browsing || inChanges();
  $("traceback-tools").hidden = $("selection-budget").hidden = inChanges();
  $("reader-title").textContent = browsing ? "閱讀工具" : "探索與理解程式碼";
  $("question-editor-summary").textContent = browsing ? "選段與 traceback 導航" : "調整選段與問題";
  $("question-label").textContent = browsing ? "貼上 Python traceback（僅對應原碼，不執行）" : "你想釐清什麼？";
  if (browsing) $("question").placeholder = '例如：File "exporter/rows.py", line 7, in render_row\n最多 2,000 字元；按下方按鈕才會對應，不是 AI 提問。';
  $("selection-limits").textContent = browsing ?
    "最多保留 3 段，每段 80 行／4,000 字元；加入後可查看名稱來源。不送給模型，也不代表完整依賴。" :
    "手動最多 3 段，自動選材最多 6 段；每段仍限 80 行，實際讀取合計最多 240 行，兩者字元預算相同。手動選段在載入模型前檢查；自動找原碼會在定位後、回答請求前檢查，放不下就停下，不裁掉候選。";
  $("reading-footer").textContent = browsing ? "純原碼閱讀 · 本機唯讀快照 · 靜態導航不等於執行時驗證" : "離線閱讀 · 唯讀快照 · 手動選段或明確授權模型尋找原碼";
}
function showProject(project, refreshed = false) {
  invalidateReadingNote();
  if (project.comparison) {
    const view = project.comparison, catalogue = view.catalogue;
    if (!/^(?:[0-9a-f]{40}|[0-9a-f]{64})$/.test(view.head) || !/^[0-9a-f]{64}$/.test(view.original_snapshot_sha256) ||
        catalogue?.schema_version !== 1 || catalogue.kind !== "source_change_catalogue" || catalogue.semantics_verified !== false || !Array.isArray(catalogue.files) || catalogue.files.length > 1000 ||
        catalogue.files.some(item => !item || typeof item.path !== "string" || !Array.isArray(item.units) || !Array.isArray(item.hunks) || !item.detail ||
          item.units.some(unit => !unit || typeof unit.name !== "string" || typeof unit.kind !== "string" || [unit.before, unit.after].some(span => span !== null && (!span || !Number.isSafeInteger(span.start_line) || !Number.isSafeInteger(span.end_line)))) ||
          item.hunks.some(hunk => [hunk?.before, hunk?.after].some(span => !span || !Number.isSafeInteger(span.start) || span.start < 0 || !Number.isSafeInteger(span.count) || span.count < 0)))) throw new Error("比較來源或目錄資料不完整；未切換畫面，請重新讀取專案。");
  }
  if (Boolean(project.comparison) !== inChanges()) $("file-filter").value = "";
  clearTraceback();
  state.pairRequest++; state.pairPending = false; $("change-slot").value = "before";
  state.project = project; state.source = null; state.focus = []; state.anchor = state.end = null; state.extending = false; state.rangeHint = ""; state.readingTrail = []; state.job = state.submission = null; state.rendered = ""; draft = null; clearOutline();
  state.history = []; state.historyEvicted = 0; state.selectedHistory = null; state.historyLoadedJob = null; state.historyError = ""; state.historyRequest++;
  state.continuation = null; state.continuationNotice = "";
  state.sourceRecovery = null;
  state.modelRequest++; state.model = null; state.modelUnknown = false; state.modelError = ""; state.modelReleasePending = false;
  clearTimeout(modelTimer); clearTimeout(modelCountdownTimer);
  state.historyBoundary = refreshed ? "已重新讀取專案；先前問答紀錄已清除。" : "";
  $("question-editor").open = true;
  $("ai-discovery-actions").open = $("reading-tools").open = $("selection-budget").open = false;
  $("traceback-tools").open = browseOnly();
  state.sourceRequest++; state.searchRequest++; clearTimeout(pollTimer);
  $("project-name").textContent = project.name; $("project-version").textContent = `${project.comparison ? "比較快照" : "快照"} ${project.version.slice(0, 12)}`;
  $("reader-badge").textContent = browseOnly() ? "純原碼閱讀 · 無 AI" : Object.hasOwn(readerLabels, project.reader) ? readerLabels[project.reader] : "本機模型 · 試用";
  $("ask-project-note").textContent = residentEnabled() ? "不知道檔案也能提問：先定位，再完整讀取候選並回答。最多 3 次模型請求，共用常駐模型；仍可能需要數分鐘。結果會揭露模型選材，不修改你的手動選段，仍可能漏掉相關實作。" : "不知道檔案也能提問：先定位，再完整讀取候選並回答。最多 3 次模型請求、分兩次載入與釋放；可能需要數分鐘。結果會揭露模型選材，不修改你的手動選段，仍可能漏掉相關實作。";
  $("source-title").textContent = "選一個檔案，開始閱讀"; $("source-version").textContent = "";
  $("code").replaceChildren(element("p", "從左側開啟檔案，點行號選取想理解的範圍。", "empty-state"));
  $("page-info").textContent = ""; $("page-prev").disabled = $("page-next").disabled = true;
  $("search-results").replaceChildren(); $("search-summary").textContent = "";
  if (project.comparison) {
    const omitted = project.comparison.excluded;
    const available = Array.isArray(omitted?.before) && Array.isArray(omitted?.after);
    $("excluded-summary").textContent = available ? `原始來源未納入比較：${omitted.before.length + omitted.after.length} 項（依版本計）` : "原始來源排除資訊未提供";
    $("excluded-list").replaceChildren(...(available ? [...omitted.before.map(path => `HEAD · ${path}`), ...omitted.after.map(path => `目前已儲存 · ${path}`)] : ["比較快照的空排除清單，不代表原始專案沒有排除項目。"]).map(path => element("li", path)));
  } else {
    $("excluded-summary").textContent = `未納入閱讀：${project.excluded.length} 項`;
    $("excluded-list").replaceChildren(...project.excluded.map(path => element("li", path)));
  }
  $("answer").replaceChildren(element("p", "選取相關原碼並提問；答案中的引用可以點擊。", "muted"));
  resetExperiment(refreshed);
  renderChanges(); renderReadingMode(); renderFiles(); renderSelections(); renderObservation(); renderJob({ id: null, status: "idle" });
  if (residentEnabled()) { pollModel(); updateModelCountdown(); }
}
function citationButton(reference, job, label, className = "citation") {
  const project = state.project;
  const wrapper = element("span", "", "citation-wrap"), button = element("button", label, className), preview = element("span", "", "citation-preview");
  button.title = `${sourceLabel(reference.path, job.comparison)}:${reference.start_line}–${reference.end_line}`;
  button.type = "button"; button.setAttribute("aria-expanded", "false"); preview.hidden = true;
  wrapper.append(button, preview); let loaded = false;
  button.addEventListener("click", async () => {
    if (state.refreshing || state.project !== project) return;
    const table = button.closest(".answer-table");
    if (table && preview.parentElement !== table.parentElement) table.after(preview);
    preview.hidden = !preview.hidden; button.setAttribute("aria-expanded", String(!preview.hidden));
    if (preview.hidden || loaded) return;
    const version = job.version || job.result?.version;
    const files = job.files || (version === state.project.version ? state.project.files : []);
    const file = files.find(item => item.path === reference.path);
    preview.replaceChildren(element("span", "正在讀取本題引用的原碼…", "muted"));
    try {
      if (!version || !file) throw new Error("本題缺少可定位的來源版本，無法安全開啟此引用。");
      const source = state.source?.version === version && state.source.file.id === file.id ? state.source : await api(`/api/source?${new URLSearchParams({ file: file.id, version })}`);
      if (state.refreshing || state.project !== project) return;
      if (source.version !== version || source.path !== reference.path || reference.end_line > source.lines.length) throw new Error("引用的原碼版本或範圍不一致；未顯示其他版本的內容。");
      const text = source.lines.slice(reference.start_line - 1, reference.end_line).map((line, index) => `${String(reference.start_line + index).padStart(4)}│ ${line}`).join("\n");
      const code = element("pre", text), open = element("button", "開啟完整原碼", "quiet source-open");
      const surrounding = element("button", "展開周邊原碼", "quiet source-context"), note = element("span", "", "context-note");
      const first = Math.max(1, reference.start_line - 16), last = Math.min(source.lines.length, reference.end_line + 16);
      let expanded = false;
      code.tabIndex = 0; open.type = "button";
      surrounding.type = "button"; surrounding.setAttribute("aria-pressed", "false"); note.hidden = true;
      surrounding.disabled = first === reference.start_line && last === reference.end_line;
      surrounding.addEventListener("click", () => {
        expanded = !expanded; surrounding.setAttribute("aria-pressed", String(expanded));
        surrounding.textContent = expanded ? "只看引用行" : "展開周邊原碼"; note.hidden = !expanded;
        if (!expanded) { code.textContent = text; return; }
        note.textContent = `周邊原碼 L${first}–L${last} · ▸ 標示原引用行。供核對，不擴大引用或選段，也不代表回答已驗證。`;
        code.replaceChildren();
        for (let number = first; number <= last; number++) {
          const cited = number >= reference.start_line && number <= reference.end_line;
          const line = element("span", `${cited ? "▸" : " "} ${String(number).padStart(4)}│ ${source.lines[number - 1]}${number < last ? "\n" : ""}`, `context-line${cited ? " cited-line" : ""}`);
          line.dataset.contextLine = number; code.append(line);
        }
      });
      open.addEventListener("click", () => { if (!state.refreshing && state.project === project) return openFile(file, version, reference.start_line, reference.end_line); });
      preview.replaceChildren(element("strong", `${sourceLabel(reference.path, job.comparison)}:${reference.start_line}–${reference.end_line} · 本題快照`), note, code, surrounding, open);
      loaded = true;
    } catch (error) { preview.replaceChildren(element("span", `${error.message} 可收合後重試，或重新讀取專案。`, "citation-error")); }
  });
  return wrapper;
}
function appendAnswerQuestion(job) {
  const question = job.question || job.result?.question;
  if (question) {
    const details = element("details", "", "answer-question");
    details.append(element("summary", `本題問題：${question}`), element("p", question)); $("answer").append(details);
  }
  if (residentCompletion(job)) $("answer").append(element("p", "這是常駐模型的請求層級紀錄，不是已釋放模型的最終收據。完成本題時工作階段清理尚待完成；來源與引用的檢查不代表解讀正確。目前模型狀態另見上方模型列。", "request-completion-note muted"));
  if (job.kind === "continue" && job.continuation && typeof job.continuation.parent_question === "string") $("answer").append(element("p", `本題沿用前題原碼：${job.continuation.parent_question}。未重新定位，也未帶入前題問答。`, "comparison-answer-scope muted"));
  if (job.kind === "continue" && validReadingScope(job)) {
    const box = element("details", "", "project-reading-scope");
    box.append(element("summary", `本題沿用的完整原碼 · ${job.reading_scope.focus.length} 段`),
      element("p", "沒有重新定位或擴充原碼；可能仍缺少新問題需要的上下文。以下是來源範圍，不代表答案引用或推論已驗證。", "muted"));
    for (const {file, start, end} of readingScopeRanges(job.reading_scope)) {
      const button = element("button", `${file.path} · L${start}–L${end}`, "definition-button retained-source");
      button.type = "button"; button.dataset.retained = "true";
      button.addEventListener("click", () => {
        if (state.pending || state.refreshing || answerJob()?.id !== job.id || !validReadingScope(answerJob())) return;
        return openFile(file, job.version, start, end);
      });
      box.append(button);
    }
    $("answer").append(box);
  }
  if (job.comparison && validComparisonJob(job)) $("answer").append(element("p", `本題：HEAD ${job.comparison.head} → 目前已儲存的原碼。第三段呼叫端固定使用目前版本；這不是完整歷史影響或測試覆蓋判定。`, "comparison-answer-scope muted"));
}
function renderDraft(job) {
  const preview = validComparisonJob(job) && job.kind !== "locate" && job.status === "running" && typeof job.preview === "string" ? job.preview : "";
  if (!preview) {
    if (draft) { draft = null; $("answer").replaceChildren(element("p", job.status === "cancelling" ? "草稿已清除，正在等待取消與清理。" : "生成草稿已清除，請查看工作狀態。", "muted")); }
    return;
  }
  if (!draft || draft.id !== job.id) {
    const body = element("div", "", "draft-text"), text = document.createTextNode("");
    body.append(text); draft = { id: job.id, text };
    $("answer").replaceChildren(); appendAnswerQuestion(job);
    $("answer").append(element("p", "生成中草稿 · 尚未完成／引用未檢查（最多顯示 12,000 字元）", "draft-label"), body);
  }
  const previous = draft.text.data;
  if (preview !== previous) {
    if (preview.startsWith(previous)) draft.text.appendData(preview.slice(previous.length));
    else draft.text.data = preview;
  }
}
function appendAnswerInline(parent, text, citations, job, following = "") {
  let offset = 0;
  // Only paired inline code/bold and exact host-accepted references. No HTML,
  // links, images or recursive Markdown; bold may contain code and citations.
  for (const match of (text + following).matchAll(/(?<!`)`([^`\n]+)`(?!`)|(?<!\*)\*\*([^*\n]+)\*\*(?!\*)|\[(E[1-9][0-9]{0,3}):(L?)([1-9][0-9]{0,6})(?:-\4([1-9][0-9]{0,6}))?\]|(?<![A-Za-z0-9_\[])(E[1-9][0-9]{0,3}|\[E[1-9][0-9]{0,3}\])[ \t]+L([1-9][0-9]{0,6})(?:-L([1-9][0-9]{0,6}))?(?![A-Za-z0-9_:\-\u2013\u2014])(?!\.[0-9])(?![ \t\r\n]*[-\u2013\u2014][ \t\r\n]*(?:L?[0-9]|$))/g)) {
    if (match.index >= text.length) break; // The suffix checks boundaries across paragraph/list splits, never renders them.
    parent.append(element("span", text.slice(offset, match.index)));
    if (match[1]) parent.append(element("code", match[1]));
    else if (match[2]) {
      const bold = element("strong", ""); appendAnswerInline(bold, match[2], citations, job); parent.append(bold);
    } else {
      const token = match[3] || match[7], id = token.startsWith("[") ? token.slice(1, -1) : token;
      const start = Number(match[5] || match[8]), end = Number(match[6] || match[9] || start);
      // Text inside a structured answer is still model-written. Neither a
      // retained evidence ID nor a contained range replaces this exact match.
      const reference = citations.find(item => item.evidence_id === id && item.start_line === start && item.end_line === end);
      parent.append(reference ? citationButton(reference, job, match[0]) : element("span", match[0]));
    }
    offset = match.index + match[0].length;
  }
  parent.append(element("span", text.slice(offset)));
}
function formatAnswer(text, citations, job) {
  const body = element("div", "", "answer-prose"), lines = text.split("\n");
  const fence = /^ {0,3}(`{3,}|~{3,})([\w+-]*)\s*$/, heading = /^ {0,3}(#{1,6}) (.+)$/;
  const bullet = /^([ \t]*)(?:([-*+]) |([1-9][0-9]{0,5})([.)]) )(.+)$/, pipe = /^\s*\|.*\|\s*$/;
  const block = (tag, value, following = "") => { const node = element(tag, ""); appendAnswerInline(node, value, citations, job, following); return node; };
  const alignedListProse = raw => {
    const levels = [];
    for (const line of raw) {
      if (/\t|`{3}|~{3}/.test(line)) return false;
      if (!line.trim()) continue;
      const entry = line.match(bullet), indent = line.match(/^ */)[0].length;
      const content = entry ? entry[5].trimStart() : line.slice(indent);
      if (/^(?:#{1,6}(?:\s|$)|>|\|)/.test(content)) return false;
      if (entry) {
        const extra = entry[5].match(/^ */)[0].length;
        if (extra >= 4) return false; // Five spaces after a marker may introduce code.
        while (levels.length && indent < levels.at(-1).marker) levels.pop();
        const parent = levels.at(-1);
        if (parent && indent === parent.marker) levels.pop();
        else if (parent ? indent !== parent.content : indent !== 0) return false;
        levels.push({marker: indent, content: line.length - entry[5].length + extra});
      } else {
        while (levels.length && indent < levels.at(-1).content) levels.pop();
        if (!levels.length || indent !== levels.at(-1).content) return false;
      }
    }
    return true;
  };
  for (let i = 0; i < lines.length;) {
    if (!lines[i].trim()) { i++; continue; }
    const marker = lines[i].match(fence), title = lines[i].match(heading), item = lines[i].match(bullet);
    if (marker) {
      let end = i + 1; while (end < lines.length && lines[end].trim() !== marker[1]) end++;
      const code = element("pre", ""); code.tabIndex = 0;
      code.append(element("code", lines.slice(end < lines.length ? i + 1 : i, end).join("\n")));
      body.append(code); i = end + 1; continue;
    }
    if (title) { body.append(block(title[1].length <= 2 ? "h4" : "h5", title[2])); i++; continue; }
    if (item) {
      let end = i + 1;
      while (end < lines.length) {
        if (!lines[end].trim()) {
          let next = end + 1; while (next < lines.length && !lines[next].trim()) next++;
          const resumed = lines[next]?.match(bullet);
          if (next >= lines.length || (!/^[ \t]+\S/.test(lines[next]) && (!resumed || (resumed[2] || resumed[4]) !== (item[2] || item[4])))) break;
          end = next; // Keep separated continuations/items together without guessing hierarchy.
        }
        if (!/^[ \t]/.test(lines[end]) && (fence.test(lines[end]) || heading.test(lines[end]) || pipe.test(lines[end]))) break;
        end++;
      }
      const raw = lines.slice(i, end), entries = raw.map(line => line.match(bullet));
      // Do not promote nested items or erase continuation indentation. Only
      // unindented, single-marker flat lists are formatted; no hierarchy guessing.
      if (entries.every(entry => entry && !entry[1] && (entry[2] || entry[4]) === (item[2] || item[4]))) {
        const list = element(item[3] ? "ol" : "ul", "");
        for (const entry of entries) { const node = block("li", entry[5]); if (entry[3]) node.value = Number(entry[3]); list.append(node); }
        body.append(list);
      } else if ((entries.every((entry, index) => entry || !raw[index].trim()) && !raw.some(line => /`{3}|~{3}/.test(line))) || alignedListProse(raw)) {
        // Explicit list markers retain their indentation/order; only inline
        // prose and exactly aligned continuations are formatted, never a tree.
        const prose = block("div", raw.join("\n"), end < lines.length ? "\n" + lines.slice(end).join("\n") : "");
        prose.className = "answer-list-prose"; body.append(prose);
      } else { const literal = element("pre", raw.join("\n")); literal.tabIndex = 0; body.append(literal); }
      i = end; continue;
    }
    if (pipe.test(lines[i])) {
      let end = i + 1; while (end < lines.length && pipe.test(lines[end])) end++;
      const raw = lines.slice(i, end), rows = raw.map(line => line.trim().slice(1, -1).split("|").map(cell => cell.trim()));
      // Flat tables only: no escaped pipes or pipes inside code, no inferred
      // columns. Ambiguous/malformed tables retain their complete literal text.
      const valid = rows.length >= 2 && rows[0].length >= 2 && rows[0].length <= 8 && raw.every(line => !line.includes("\\|") && !line.includes("``")) && rows.every(row => row.length === rows[0].length && row.every(cell => (cell.match(/`/g) || []).length % 2 === 0)) && rows[1].every(cell => /^:?-{3,}:?$/.test(cell));
      if (valid) {
        const wrap = element("div", "", "answer-table"), table = element("table", ""), head = element("thead", ""), contents = element("tbody", ""); wrap.tabIndex = 0;
        rows.forEach((row, index) => {
          if (index === 1) return;
          const tr = element("tr", ""); row.forEach(cell => tr.append(block(index ? "td" : "th", cell))); (index ? contents : head).append(tr);
        });
        table.append(head, contents); wrap.append(table); body.append(wrap);
      } else { const literal = element("pre", raw.join("\n")); literal.tabIndex = 0; body.append(literal); }
      i = end; continue;
    }
    let end = i + 1;
    while (end < lines.length && lines[end].trim() && !fence.test(lines[end]) && !heading.test(lines[end]) && !bullet.test(lines[end]) && !pipe.test(lines[end])) end++;
    body.append(block("p", lines.slice(i, end).join("\n"), end < lines.length ? "\n" + lines.slice(end).join("\n") : "")); i = end;
  }
  return body;
}
function validDiscovery(outcome, version) {
  if (outcome?.ok !== true || outcome.status !== "located" || outcome.acceptance?.ok !== true ||
      outcome.source_unchanged !== true || outcome.snapshot_unchanged !== true || outcome.ingress_unchanged !== true || outcome.snapshot_sha256 !== version || version !== state.project?.version) {
    return false;
  }
  const candidates = outcome.candidates, scope = outcome.scope;
  if (!Array.isArray(candidates) || candidates.length > 6 || candidates.some(item =>
    !item || typeof item.name !== "string" || !Number.isInteger(item.start_line) || !Number.isInteger(item.end_line) ||
    item.start_line < 1 || item.end_line < item.start_line || !state.project.files.some(file => file.path === item.path && item.end_line <= file.lines))) {
    return false;
  }
  if (scope !== undefined && (scope?.mode !== "files_then_definitions" || !Array.isArray(scope.files) || scope.files.length > 3 ||
      new Set(scope.files).size !== scope.files.length || scope.files.some(path => !state.project.files.some(file => file.path === path)) ||
      scope.total_files !== state.project.files.length || !Number.isInteger(scope.total_functions) || scope.total_functions < 1 ||
      !Number.isInteger(scope.selected_functions) || scope.selected_functions < candidates.length || scope.selected_functions > scope.total_functions ||
      (scope.files.length === 0 && scope.selected_functions !== 0) || candidates.some(item => !scope.files.includes(item.path)))) {
    return false;
  }
  if (Object.hasOwn(outcome, "unindexed")) {
    const rows = outcome.unindexed, reasons = {
      unavailable: ["line_separators", "parse_depth", "syntax_or_version"],
      limited: ["source_limit", "node_limit", "item_limit", "metadata_limit"]};
    if (!Array.isArray(rows) || !rows.length || rows.length > Math.min(1000, state.project.files.length) ||
        rows.some(row => !row || typeof row !== "object" || Array.isArray(row) || Object.keys(row).length !== 4 ||
          !["file", "path", "status", "reason"].every(key => Object.hasOwn(row, key) && typeof row[key] === "string") ||
          !/\.pyi?$/i.test(row.path) || !Object.hasOwn(reasons, row.status) || !reasons[row.status].includes(row.reason) ||
          !state.project.files.some(file => file.id === row.file && file.path === row.path) ||
          candidates.some(item => item.path === row.path) || scope?.files.includes(row.path)) ||
        new Set(rows.map(row => row.file)).size !== rows.length || new Set(rows.map(row => row.path)).size !== rows.length) return false;
  }
  return true;
}
function appendDiscoveryCoverage(outcome, job) {
  if (!outcome.unindexed) return;
  const rows = outcome.unindexed, box = element("section", "", "discovery-index-warning");
  box.append(element("p", `Python 函式目錄不完整：${rows.length} 個檔案未建立索引，定位不會挑選其中的函式，相關實作可能被漏掉。`, "answer-prose"));
  const detail = element("details"), list = element("ul", "", "discovery-scope");
  detail.append(element("summary", `查看未索引檔案（${rows.length}）`),
    element("p", "檔案仍保留在唯讀快照；點開可自行閱讀，不會加入選段或重新提問。", "muted"));
  const reasons = {line_separators: "換行格式無法對應行號", parse_depth: "語法結構過深", syntax_or_version: "語法或 Python 版本無法解析",
    source_limit: "原碼大小超過上限", node_limit: "語法節點超過上限", item_limit: "定義數量超過上限", metadata_limit: "索引資料超過上限"};
  for (const row of rows) {
    const item = element("li"), file = state.project.files.find(file => file.id === row.file && file.path === row.path);
    const button = element("button", `${row.path} · ${reasons[row.reason]}（${row.reason}）`, "definition-button");
    button.type = "button"; button.dataset.discovery = "true";
    button.addEventListener("click", () => {
      const current = answerJob();
      if (state.pending || state.refreshing || current?.id !== job.id || state.project?.version !== job.version) return;
      const live = current.kind === "project" ? projectReading(current)?.discovery :
        current.status === "located" && readingResultKind(current, "forge8.locate") && requestFinished(current) ? current.result?.outcome : null;
      if (!validDiscovery(live, job.version) || !live.unindexed?.some(item =>
          ["file", "path", "status", "reason"].every(key => item[key] === row[key]))) return;
      return openFile(file, job.version);
    });
    item.append(button); list.append(item);
  }
  detail.append(list); box.append(detail); $("answer").append(box);
}
function appendDiscoveryCandidates(candidates, job, parent = $("answer")) {
  const version = job.version;
  for (const item of candidates) {
    const file = state.project.files.find(file => file.path === item.path), button = element("button", "", "definition-button discovery-hit");
    button.type = "button"; button.dataset.discovery = "true";
    button.append(element("strong", item.name), element("small", `${item.path} · L${item.start_line}–${item.end_line}${item.end_line - item.start_line >= 80 ? " · 完整選取需多段空位" : ""}${item.stub ? " · 省略內容" : ""}`));
    button.addEventListener("click", async () => {
      if (state.pending || state.refreshing || answerJob()?.id !== job.id || state.project?.version !== version) return;
      if (job.kind === "project" && !projectReading(answerJob())) return;
      const sameFile = state.source?.file.id === file.id && state.source.version === version;
      const source = sameFile ? state.source : await openFile(file, version);
      if (answerJob()?.id === job.id && state.project?.version === version && (job.kind !== "project" || projectReading(answerJob()))) selectDefinition(item, source, sameFile);
    });
    parent.append(button);
  }
}
function renderDiscovery(job) {
  const outcome = job.result?.outcome;
  $("answer").replaceChildren(); appendAnswerQuestion(job);
  if (job.status !== "located" || !readingResultKind(job, "forge8.locate") || job.result.ok !== true || !requestFinished(job) || !validDiscovery(outcome, job.version)) {
    $("answer").append(element("p", job.error || job.result?.error || outcome?.failure_reason || "定位未完成，或來源／候選範圍資料不符；未顯示候選。", "answer-prose")); return;
  }
  const {candidates, scope} = outcome;
  if (candidates.length) $("question-editor").open = false;
  $("answer").append(element("h3", "AI 建議閱讀位置 · 尚未解釋程式"),
    element("p", "模型只看目錄與 README 節錄，沒有讀取函式實作。候選可能有遺漏或不相關，也不代表實際呼叫關係。點開核對，再自行加入選段。", "muted"));
  appendDiscoveryCoverage(outcome, job);
  if (scope) {
    $("answer").append(element("p", `兩階段定位 · 先從 ${scope.total_files} 個檔案挑選，再檢視所選檔案的完整函式目錄（${scope.selected_functions} / ${scope.total_functions} 個）。未選中的檔案仍可能有相關實作。`, "muted"));
    const list = element("ul", "", "discovery-scope");
    for (const path of scope.files) list.append(element("li", path));
    $("answer").append(list);
  }
  if (!candidates.length) $("answer").append(element("p", "沒有找到可建議的函式；可補充用途，或使用左側的檔案與字面搜尋。", "answer-prose"));
  appendDiscoveryCandidates(candidates, job);
}
function projectReading(job) {
  const result = job?.result, meta = result?.project_reading, outcome = result?.outcome;
  if (job?.kind !== "project" || !["answered", "incomplete"].includes(job.status) || job.error || !requestFinished(job) ||
      !readingResultKind(job, "forge8.explain") || !meta || !validDiscovery(meta.discovery, job.version) || !Array.isArray(meta.focus) ||
      (residentCompletion(job) && !residentDetailCompletion(meta.discovery))) return null;
  const supplement = meta.context;
  if (Object.hasOwn(meta, "context") && (!supplement || typeof supplement !== "object" || Array.isArray(supplement) ||
      Object.keys(supplement).length !== 2 || !Object.hasOwn(supplement, "added") || !Object.hasOwn(supplement, "skipped") ||
      !Array.isArray(supplement.added) || supplement.added.length > 4 || !supplement.skipped || typeof supplement.skipped !== "object" || Array.isArray(supplement.skipped) ||
      Object.entries(supplement.skipped).some(([reason, count]) => !Object.hasOwn(projectContextReasons, reason) || !Number.isSafeInteger(count) || count < 1 || count > 384) ||
      supplement.added.some(span => !span || typeof span !== "object" || Array.isArray(span) || Object.keys(span).length !== 3 ||
        !Object.hasOwn(span, "path") || !Object.hasOwn(span, "start_line") || !Object.hasOwn(span, "end_line") || typeof span.path !== "string" || !span.path ||
        !Number.isSafeInteger(span.start_line) || !Number.isSafeInteger(span.end_line) || span.start_line < 1 || span.end_line < span.start_line || span.end_line - span.start_line >= 40) ||
      supplement.added.reduce((total, span) => total + span.end_line - span.start_line + 1, 0) > 40 ||
      new Set(supplement.added.map(span => JSON.stringify([span.path, span.start_line, span.end_line]))).size !== supplement.added.length)) return null;
  if (meta.answer_attempted === false) return job.status === "incomplete" && result.status === "selection_required" && result.ok === false && meta.focus.length === 0 && !supplement?.added.length ? meta : null;
  if (meta.answer_attempted !== true || !meta.focus.length || meta.focus.length > 6 || !meta.discovery.candidates.length ||
      (job.status === "answered" ? result.ok !== true || result.status !== "answered" || outcome?.ok !== true || outcome.status !== "answered" :
        gatedInsufficient(job) === null && (result.ok !== false || result.status !== "stalled" || outcome?.ok !== false || outcome.status !== "stalled")) ||
      outcome?.source_unchanged !== true || outcome.snapshot_unchanged !== true || outcome.acceptance?.ok !== true) return null;
  const lines = (ranges, limit) => {
    const keys = new Set();
    if (!Array.isArray(ranges) || ranges.length > 6) return null;
    for (const span of ranges) {
      if (!span || !Number.isInteger(span.start_line) || !Number.isInteger(span.end_line) || span.start_line < 1 ||
          span.end_line < span.start_line || span.end_line - span.start_line >= limit ||
          !state.project.files.some(file => file.path === span.path && span.end_line <= file.lines)) return null;
      for (let line = span.start_line; line <= span.end_line; line++) keys.add(`${span.path}\0${line}`);
    }
    return keys.size <= 240 ? keys : null;
  };
  const planned = lines(meta.focus, 80), rows = outcome.coverage?.observed?.ranges;
  if (!planned || !Array.isArray(rows) || rows.length > 6 || rows.some(row => !Array.isArray(row?.ranges) || row.ranges.length > 6)) return null;
  const observed = lines(rows.flatMap(row => row.ranges.map(span => ({...span, path: row.path}))), 240);
  if (!observed || observed.size !== planned.size || [...planned].some(key => !observed.has(key)) ||
      meta.discovery.candidates.some(item => item.end_line - item.start_line >= 240 ||
        Array.from({length: item.end_line - item.start_line + 1}, (_, index) => `${item.path}\0${item.start_line + index}`).some(key => !planned.has(key)))) return null;
  if (supplement?.added.some(span => Array.from({length: span.end_line - span.start_line + 1}, (_, index) => `${span.path}\0${span.start_line + index}`).some(key => !planned.has(key)))) return null;
  return meta;
}
function renderProjectScope(job, meta) {
  const box = element("details", "", "project-reading-scope"), {discovery, focus, answer_attempted: attempted} = meta;
  appendDiscoveryCoverage(discovery, job);
  const added = meta.context?.added || [], skipped = Object.entries(meta.context?.skipped || {});
  const appendRange = (span, supplemental = false) => {
    const file = state.project.files.find(file => file.path === span.path), button = element("button", `${span.path} · L${span.start_line}–L${span.end_line}`, "definition-button retained-source");
    button.type = "button"; button.dataset.retained = "true";
    if (supplemental) button.dataset.projectContext = "true";
    button.addEventListener("click", () => {
      if (state.pending || state.refreshing || answerJob()?.id !== job.id || !projectReading(answerJob())) return;
      return openFile(file, job.version, span.start_line, span.end_line);
    });
    box.append(button);
  };
  box.open = !attempted;
  box.append(element("summary", `模型選材 · ${focus.length} 段回答原碼${added.length ? ` · 自動補充 ${added.length} 項同檔宣告` : ""} · 非整個專案`), element("p", attempted ?
    "本題完整保留模型挑選的候選，分組或分段後交給模型；可能包含段間原碼。這不是你的手動選段，仍可能漏掉相關實作。" :
    "本次只完成定位，尚未進行回答。請查看原因，點候選檢查實作，再自行加入合適的選段；沒有刪掉候選或自動重試。", "muted"));
  if (discovery.scope) {
    box.append(element("p", `定位僅檢視所選 ${discovery.scope.files.length} / ${discovery.scope.total_files} 個檔案的函式目錄（${discovery.scope.selected_functions} / ${discovery.scope.total_functions} 個）；其他檔案仍可能相關。`, "muted"));
    const files = element("ul", "", "discovery-scope");
    for (const path of discovery.scope.files) files.append(element("li", path));
    box.append(files);
  }
  if (!attempted) appendDiscoveryCandidates(discovery.candidates, job, box);
  else {
    const names = element("ul", "", "discovery-scope");
    for (const item of discovery.candidates) names.append(element("li", `${item.name} · ${item.path}:${item.start_line}–${item.end_line}`));
    box.append(names);
    box.append(element("p", "回答實際讀取範圍 · 點擊核對，不會加入手動選段", "muted"));
    for (const span of focus) appendRange(span);
  }
  if (added.length || skipped.length) {
    box.append(element("p", "自動補充只找同檔案中名稱相符的一層宣告；不是已解析的名稱綁定、必要依賴或完整上下文。", "muted"));
    if (added.length) {
      box.append(element("p", "補充宣告的完整範圍 · 已包含於上方實際讀取範圍；點擊只查看原碼", "muted"));
      for (const span of added) appendRange(span, true);
    }
    if (skipped.length) box.append(element("p", `未補入原因計數：${skipped.map(([reason, count]) => `${projectContextReasons[reason]} ${count}`).join("、")}。不代表已找出所有遺漏。`, "muted"));
  }
  $("answer").append(box);
}
function unverifiedProse(job) {
  const result = job?.result, outcome = result?.outcome, text = outcome?.unverified_prose;
  return job?.status === "incomplete" && validComparisonJob(job) && job.kind !== "locate" && !job.error && requestFinished(job) &&
    typeof job.version === "string" && job.version === state.project?.version && readingResultKind(job, "forge8.explain") && result.status === "stalled" && result.ok === false &&
    outcome?.status === "stalled" && outcome.ok === false && outcome.answer === null && outcome.acceptance?.ok === true &&
    outcome.source_unchanged === true && outcome.snapshot_unchanged === true &&
    typeof text === "string" && text.trim() && [...text].length <= 12_000 && (job.kind !== "project" || projectReading(job)) ? text : null;
}
function renderUnverified(job, text) {
  const box = element("div", "", "unverified-output"), outcome = job.result.outcome;
  box.append(element("h3", "待核對的模型解讀"),
    element("p", "文字生成已結束，但引用未通過檢查，內容也可能有錯誤或遺漏。以下不是已驗證答案；請對照本題讀取的原碼。", "muted"));
  const rows = outcome.coverage?.observed?.ranges, ranges = [];
  let valid = Array.isArray(rows) && rows.length > 0 && rows.length <= 3;
  for (const row of valid ? rows : []) {
    const file = job.files?.find(file => file.path === row?.path);
    if (!file || !Array.isArray(row.ranges) || !row.ranges.length || row.ranges.length > 3 ||
        !state.project.files.some(item => item.id === file.id && item.path === file.path)) { valid = false; break; }
    for (const span of row.ranges) {
      if (!Number.isInteger(span?.start_line) || !Number.isInteger(span.end_line) || span.start_line < 1 ||
          span.end_line < span.start_line || span.end_line > file.lines) { valid = false; break; }
      ranges.push({file, start: span.start_line, end: span.end_line});
    }
  }
  if (valid && ranges.length > 0 && ranges.length <= 3 && job.kind !== "project") {
    box.append(element("p", "本題保留讀取的原碼 · 不是回答引用", "muted"));
    for (const {file, start, end} of ranges) {
      const button = element("button", `${sourceLabel(file.path, job.comparison)} · L${start}–L${end}`, "definition-button retained-source");
      button.type = "button"; button.dataset.retained = "true";
      button.addEventListener("click", () => {
        if (state.pending || state.refreshing || answerJob()?.id !== job.id || !unverifiedProse(answerJob()) || state.project?.version !== job.version) return;
        return openFile(file, job.version, start, end);
      });
      box.append(button);
    }
  } else if (job.kind !== "project") box.append(element("p", "本題原碼範圍無法顯示；可從檔案清單自行核對。", "muted"));
  box.append(formatAnswer(text, [], job)); // No model-written reference becomes clickable.
  const detail = element("details", "", "reference-failure");
  detail.append(element("summary", "引用檢查資訊"), element("p", outcome.failure_reason || "引用未通過檢查。", "muted"));
  box.append(detail); $("answer").append(box);
}
function renderAnswer(job) {
  if (job.kind === "locate" || ["forge8.locate", "forge8.locate.request"].includes(job.result?.kind)) { renderDiscovery(job); return; }
  const result = job.result, outcome = result?.outcome, claims = outcome?.answer?.claims || [];
  $("answer").replaceChildren(); appendAnswerQuestion(job);
  const insufficient = gatedInsufficient(job);
  if (!inChanges() && !job.comparison && (result?.status === "insufficient_evidence" || outcome?.status === "insufficient_evidence") && insufficient === null) {
    $("answer").append(element("p", "資訊不足回報的來源、內容或請求收尾未通過檢查；未提供可恢復的原碼或答案。", "answer-prose")); return;
  }
  const selectionOnly = job.kind === "project" && result?.status === "selection_required" && result.project_reading?.answer_attempted === false && outcome === null;
  const residentResult = result?.kind === "forge8.explain.request" || job.gpu === "resident" || job.request_completion !== undefined || result?.request_completion !== undefined;
  if (residentResult && (!residentCompletion(job) || job.version !== state.project?.version || (!selectionOnly && (outcome?.source_unchanged !== true || outcome.snapshot_unchanged !== true)) || job.error ||
      (job.status === "answered" && (result.ok !== true || result.status !== "answered" || outcome.ok !== true || outcome.status !== "answered")))) {
    $("answer").append(element("p", job.error || result?.error || outcome?.failure_reason || "本題的來源或請求收尾尚未通過檢查；未將常駐狀態當作完成答案。", "answer-prose")); return;
  }
  if (!validComparisonJob(job) && (claims.length || typeof outcome?.unverified_prose === "string")) { $("answer").append(element("p", "本題比較版本資訊不符；未顯示可能屬於其他版本的解讀。請重新讀取專案。", "answer-prose")); return; }
  if (job.kind === "project") {
    const meta = projectReading(job);
    if (!meta) { $("answer").append(element("p", job.error || result?.error || outcome?.failure_reason || "本題的來源範圍或清理狀態無法確認，沒有可顯示的專案解讀。", "answer-prose")); return; }
    if (!meta.answer_attempted) {
      if (meta.discovery.candidates.length) $("question-editor").open = false;
      $("answer").append(element("p", result.error || "候選尚未進入回答；請調整問題或改用手動選段。", "answer-prose"));
      renderProjectScope(job, meta); return;
    }
    $("question-editor").open = insufficient !== null;
    renderProjectScope(job, meta);
  }
  if (insufficient !== null) {
    const box = element("section", "", "insufficient-output");
    box.append(element("h3", "模型指出資訊不足"), element("p", "以下是模型對缺失資訊的說明，尚未證明真的缺少這些內容；這不是服務故障，也不是完成的解讀。", "muted"), element("p", insufficient, "insufficient-reason"));
    const scope = validReadingScope(job);
    if (scope && job.kind !== "project" && job.kind !== "continue") {
      box.append(element("p", "本題實際讀取的原碼 · 不是答案引用", "muted"));
      for (const {file, start, end} of readingScopeRanges(scope)) {
        const button = element("button", `${file.path} · L${start}–L${end}`, "definition-button retained-source"); button.type = "button"; button.dataset.retained = "true";
        button.addEventListener("click", () => { if (!state.pending && !state.refreshing && answerJob()?.id === job.id && validReadingScope(answerJob())) return openFile(file, job.version, start, end); }); box.append(button);
      }
    }
    box.append(element("p", scope ? "可用上方「取代手動選段」回到可編輯的原碼；超過手動上限時請自行選段。也可沿用相同原碼，寫一個較窄的新問題。不帶入舊模型說明，必須自行送出。" : "本題沒有可恢復的完整範圍；請從檔案清單核對並手動選段，問題草稿未修改，不會自動重試。", "muted"));
    $("answer").append(box); return;
  }
  const unverified = unverifiedProse(job);
  if (unverified !== null) { renderUnverified(job, unverified); return; }
  if (job.status !== "answered" || !claims.length) {
    $("answer").append(element("p", job.error || result?.error || outcome?.failure_reason || (job.status === "cancelled" ? "本題已取消，沒有完成的解讀。" : "本題未能完成。請查看上方狀態，保留問題並調整選段後再試。"), "answer-prose"));
    const excerpt = job.rejected_preview;
    if (job.status === "incomplete" && requestFinished(job) && !job.error &&
        result?.status === "stalled" && result.ok === false && outcome?.status === "stalled" && outcome.ok === false && outcome.answer === null &&
        typeof excerpt === "string" && excerpt.trim() && [...excerpt].length <= 12_000) {
      const details = element("details", "", "rejected-preview");
      details.append(element("summary", "查看未採用的輸出節錄"),
        element("p", "節錄可能不完整或不正確，引用未通過檢查；最多顯示當時收到的前 12,000 字元，不代表完整回答。", "muted"),
        element("div", excerpt, "rejected-preview-text"));
      $("answer").append(details);
    }
    return;
  }
  $("answer").append(element("h3", outcome.answer.format === "cited_prose" ? "模型解讀 · 來源可核對，內容未驗證" : "模型解讀"));
  const references = new Map();
  for (const claim of claims) {
    const citations = claim.citations || [];
    $("answer").append(formatAnswer(String(claim.text || ""), citations, job));
    for (const reference of citations) references.set(`${reference.path}:${reference.start_line}:${reference.end_line}`, reference);
  }
  $("answer").append(element("p", "引用出處 · 點擊就地展開本題原碼", "muted"));
  for (const reference of references.values()) $("answer").append(citationButton(reference, job, `${sourceLabel(reference.path, job.comparison)}:${reference.start_line}–${reference.end_line}`, "citation reference"));
}
function renderJob(job) {
  state.job = job;
  if (state.selectedHistory === null && job.status !== "incomplete") {
    for (const diagnostic of [...$("answer").children].filter(node => ["rejected-preview", "unverified-output", "insufficient-output", ...(!["answered", "incomplete"].includes(job.status) ? ["project-reading-scope"] : [])].includes(node.className))) {
      if (diagnostic.className === "project-reading-scope") $("answer").replaceChildren(element("p", "正在確認本題的來源範圍與工作狀態…", "muted"));
      else diagnostic.remove();
      state.rendered = "";
    }
  }
  const labels = { idle: "尚未提問", running: "本機模型處理中", cancelling: "正在取消與清理", unknown: "等待確認本機狀態", answered: "解讀已完成", located: "定位完成 · 請檢查候選", incomplete: "本題未完成", cancelled: "本題已取消" };
  const phases = {
    "checking selected source": "正在檢查選取的原碼。",
    "checking source catalogue": "正在檢查專案目錄。",
    "Project question: locating source before answering.": "專案提問：先從目錄尋找相關原碼，尚未進行回答。",
    "Project question: preparing complete candidate source.": "專案提問：定位模型已釋放，正在檢查全部候選原碼能否完整放入回答。",
    "Project question: answering from the automatically selected source.": "專案提問：重新載入模型，依自動選中的完整原碼回答；原有手動選段保持不變。",
    "building a complete bounded Python definition catalogue": "正在建立 Python 函式目錄並檢查定位範圍。",
    "selecting source candidates from the catalogue; not answering": "模型正在依目錄挑選閱讀位置，尚未解釋實作。",
    "Finding source definitions (one local request; no answer generation).": "本機模型正在挑選閱讀位置；這次只定位，不產生解釋。",
    "Large catalogue: at most 2 requests; choose up to 3 files, then definitions only within those files; relevant files may be missed.": "專案目錄較大，最多進行兩次定位：先選檔案，再找其中的實作。可能漏掉相關檔案。",
    "Large catalogue: selecting up to 3 files (request 1/2; paths only).": "定位 1 / 2：依完整檔案目錄挑選最多 3 個檔案，尚未讀取實作。",
    "Selecting from every Python definition in the chosen files (request 2/2; no answer generation).": "定位 2 / 2：從所選檔案的完整函式目錄挑選閱讀位置，不產生解釋。",
    "sanitizing and fingerprinting a read-only source snapshot": "正在建立並檢查唯讀原碼快照。",
    "hashing the pinned local runtime and model": "正在完整驗證本機模型與執行環境（SHA-256）；本階段尚未啟動模型，可取消本題。",
    "Qwen3.5 preview reader: one bounded thinking pass; answer may take about 1-2 minutes": "正在準備 Qwen3.5 試用模型；每題可能需要 1–2 分鐘。",
    "Gemma4 12B preview reader: one bounded thinking pass; larger model may take longer": "正在準備 Gemma 4 12B 實驗模型；需要較多顯示記憶體，等待時間可能較長。",
    "starting the verified local model": "正在載入已驗證的本機模型。",
    "reading only the sanitized snapshot and building source citations": "模型正在解讀選段並整理引用，請稍候。",
    "reading user-selected source ranges before final synthesis": "正在讀取選段，準備整理解讀。",
    "reading automatically selected source ranges before final synthesis": "正在讀取自動選中的完整原碼，準備回答；不變動你的手動選段。",
    "grounded answer accepted; preparing safe shutdown": "引用範圍檢查完成，正在準備停止本機模型。",
    "stopping the local model and proving GPU/server release": "正在停止本機模型並確認服務退出。",
    "GPU/server release proven; sealing the evidence bundle": "已確認本機模型退出，正在保存本題紀錄。"
  };
  const synthesis = typeof job.phase === "string" && job.phase.match(/^local model final synthesis; (\d+) citable source span\(s\) retained$/);
  if (residentEnabled()) {
    phases["Project question: preparing complete candidate source."] = "專案提問：定位請求已完成，正在檢查全部候選原碼能否完整放入回答。";
    // Candidate preparation may outlast the resident idle deadline. The live
    // model row, not this workflow phase, establishes reuse or a new load.
    phases["Project question: answering from the automatically selected source."] = "專案提問：依自動選中的完整原碼回答；原有手動選段保持不變。模型狀態請見上方。";
    phases["grounded answer accepted; preparing safe shutdown"] = "引用範圍已檢查，正在核對本題請求收尾；模型可能保留常駐。";
  }
  const finished = { answered: "本題已完成。可保留選段修改問題，或點引用對照原碼。", located: "定位不等於解釋。先點候選檢查實作，再加入相關選段；不會自動啟動下一次模型。", incomplete: "本題未產生完整解讀，請查看下方原因後調整問題或選段。", cancelled: "取消流程已結束。可修改問題或選段後重新提問。" };
  if (job.kind === "locate") finished.incomplete = "定位未完成；請查看下方原因。問題與原有選段已保留。";
  if (job.kind === "project") {
    finished.answered = "已根據模型選中的原碼完成解讀；請核對顯示的範圍，這不代表整個專案已被理解。";
    finished.incomplete = job.result?.status === "selection_required" ? "候選尚未進入回答；請查看原因，改用手動選段。原有問題與選段已保留。" : "專案提問未完成；請查看原因。原有問題與手動選段已保留。";
  }
  if (job.result?.status === "integrity_failed") finished.incomplete = "模型或執行環境檔案未通過檢查。請依下方原因修復部署，再重新提問。";
  const reviewable = unverifiedProse(job) !== null;
  if (reviewable) {
    labels.incomplete = "文字已生成 · 引用未通過";
    finished.incomplete = "可閱讀待核對內容，並開啟本題實際讀取的原碼；不會自動重試模型。";
  }
  if (gatedInsufficient(job) !== null && (job.kind !== "project" || projectReading(job))) {
    labels.incomplete = "模型指出資訊不足";
    finished.incomplete = "本題已停止生成，沒有完整解讀。請核對缺失說明與實際讀取原碼，明確調整選段或問題；不會自動重試。";
  }
  $("status-label").textContent = labels[job.status] || "檢查工作狀態中";
  $("status-dot").className = `status-dot ${["running", "cancelling"].includes(job.status) ? "running" : job.status === "answered" ? "answered" : job.status === "incomplete" ? "incomplete" : ""}`;
  const elapsed = Math.max(0, Math.floor(Number(job.elapsed_seconds) || 0));
  $("elapsed").textContent = job.id ? `${Math.floor(elapsed / 60)}:${String(elapsed % 60).padStart(2, "0")}` : "";
  $("phase").textContent = finished[job.status] || phases[job.phase] || (synthesis ? `本機模型正在整理解讀；使用 ${synthesis[1]} 段已保留原碼。` : job.phase) || (job.status === "idle" ? "僅瀏覽與搜尋，不啟動模型。提問可能需要 1–2 分鐘。" : "等待本機服務回報狀態…");
  renderHistory(); renderViewedAnswer();
  controls();
}
async function poll() {
  clearTimeout(pollTimer);
  if (browseOnly() || polling) return;
  polling = true; const request = state.statusRequest;
  try {
    let job = await jobApi("/api/jobs/current");
    if (request === state.statusRequest && !state.pending && !browseOnly()) {
      if (!job || !["idle", "running", "cancelling", "answered", "located", "incomplete", "cancelled"].includes(job.status) ||
          (job.status === "idle" ? job.id !== null : !validJobId(job.id))) throw new Error("本機工作狀態不完整；無法確認是否正在執行，未開放新問題。");
      const submission = state.submission;
      if (submission?.uncertain && (!job.id || job.id === submission.previous)) {
        renderJob({id: null, kind: submission.kind, status: "unknown",
          phase: "送出結果尚未確認；正在查詢目前工作，不會重送問題。若持續無法確認，請在原終端機以 Ctrl+C 關閉閱讀桌並等待清理；不要另開一題。"});
        return;
      }
      if (submission?.error && (!job.id || job.id === submission.previous) && !["running", "cancelling"].includes(job.status)) {
        job = { id: job.id, kind: submission.kind, status: "incomplete", error: `本題未能送出：${submission.error}` };
      } else if (submission?.error && job.id && job.id !== submission.previous) showError("");
      state.submission = null; renderJob(job);
      if (terminal(job) && job.version === state.project?.version && state.historyLoadedJob !== job.id) {
        if (residentEnabled()) pollModel();
        await loadHistory();
        if (request === state.statusRequest && !state.historyError) state.historyLoadedJob = job.id;
      }
    }
  }
  catch (error) { if (request === state.statusRequest && !browseOnly()) renderJob({ ...state.job, status: "unknown", phase: `${error.message} 暫時無法確認工作狀態，不代表模型已停止；正在重新連線。` }); }
  finally { polling = false; if (active() && !browseOnly()) pollTimer = setTimeout(poll, 1000); }
}
$("file-filter").addEventListener("input", renderFiles);
$("outline-filter").addEventListener("input", renderOutline);
function searchModeChanged() {
  state.searchRequest++; $("search-results").replaceChildren(); $("search-summary").textContent = "";
  const definitions = $("search-mode").value === "definitions", keywords = $("search-mode").value === "keywords", calls = $("search-mode").value === "calls";
  $("search-keyword-hint").hidden = !keywords;
  $("search-call-hint").hidden = !calls;
  $("search-query-label").textContent = calls ? "找同名呼叫（單一名稱、區分大小寫）" : keywords ? "以全部關鍵字找函式（最多 8 詞）" : definitions ? "同名定義（區分大小寫）" : "在原碼中搜尋文字";
  $("search-query").placeholder = calls ? "例如 merge_setting；不含物件前綴" : keywords ? "例如 merge headers 或 user config unix" : definitions ? "例如 merge_setting 或 Context.invoke" : "例如 getint 或 release";
}
$("search-mode").addEventListener("change", searchModeChanged);
$("search-query").addEventListener("input", () => { state.searchRequest++; $("search-results").replaceChildren(); $("search-summary").textContent = ""; });
function clearTraceback() {
  state.tracebackRequest++; state.tracebackPending = false;
  $("traceback-results").replaceChildren(); $("traceback-summary").textContent = "";
}
async function locateTraceback() {
  if (inChanges() || $("traceback-locate").disabled) return;
  clearTraceback(); showError("");
  const text = $("question").value, project = state.project, request = state.tracebackRequest;
  if (text.length > 2000) { showError("紀錄超過 2,000 字元；請先縮短內容，未送出或裁切。"); return; }
  const current = () => request === state.tracebackRequest && state.project === project && $("question").value === text;
  const reasons = {outside_project: "不在目前專案內", unknown_source: "未納入目前快照", unsupported_path: "不支援此路徑格式", line_out_of_range: "行號超出目前檔案", line_separators: "換行格式無法精確對應行號"};
  state.tracebackPending = true; controls(); $("traceback-summary").textContent = "正在對應目前快照，不啟動模型…";
  try {
    const result = await api("/api/traceback", {text, version: project.version});
    if (!current()) return;
    if (result.version !== project.version || result.scope !== "unverified_user_traceback" || !Array.isArray(result.frames) || !result.frames.length || result.frames.length > 32) throw new Error("紀錄對應結果或版本不一致；請重新解析。");
    const files = result.frames.map(frame => {
      if (!frame || typeof frame.reported_path !== "string" || typeof frame.function !== "string" || !Number.isSafeInteger(frame.line) || frame.line < 0) throw new Error("紀錄對應座標無效；未開啟任何路徑。");
      if (frame.reason !== null) {
        if (!Object.hasOwn(reasons, frame.reason) || frame.file !== null || frame.path !== null) throw new Error("紀錄對應狀態無效；請重新解析。");
        return null;
      }
      const file = project.files.find(file => file.id === frame.file && file.path === frame.path && 1 <= frame.line && frame.line <= file.lines);
      if (!file) throw new Error("紀錄座標不在目前快照內；未開啟任何路徑。");
      return file;
    });
    if (result.matched !== files.filter(Boolean).length) throw new Error("紀錄對應數量不一致；請重新解析。");
    $("traceback-summary").textContent = `貼上紀錄未驗證，只對應目前快照：${result.matched} / ${result.frames.length} 個位置。${browseOnly() ? "點擊查看原碼，不執行紀錄中的呼叫。" : "點擊後可自行選段解釋。"}`;
    result.frames.forEach((frame, index) => {
      const file = files[index], row = element(file ? "button" : "div", "", "search-hit definition-hit");
      row.dataset.tracebackFrame = index + 1;
      row.append(element("span", `${index + 1}. ${frame.reported_path}:${frame.line}${frame.function ? ` · ${frame.function}` : ""}`), element("small", file ? `目前快照：${file.path}:${frame.line}` : reasons[frame.reason]));
      if (file) {
        row.type = "button";
        row.addEventListener("click", () => { if (current() && !state.pending && !state.refreshing) return openFile(file, project.version, frame.line); });
      }
      $("traceback-results").append(row);
    });
  } catch (error) {
    if (current()) { $("traceback-results").replaceChildren(); $("traceback-summary").textContent = "紀錄未完成對應；未開啟路徑或啟動模型。"; showError(error.message); }
  } finally { if (request === state.tracebackRequest) { state.tracebackPending = false; controls(); } }
}
$("traceback-locate").addEventListener("click", locateTraceback);
$("question").addEventListener("input", () => { state.questionRevision++; clearTraceback(); controls(); });
$("question").addEventListener("paste", event => {
  const text = event.clipboardData?.getData("text/plain"), input = $("question");
  if (typeof text === "string" && input.value.length - (input.selectionEnd - input.selectionStart) + text.length > questionLimit()) {
    event.preventDefault(); showError(`貼上後會超過 ${inChanges() ? "1,300" : "2,000"} 字元，已保留原稿；未貼上或裁切，請先縮短紀錄。`);
  }
});
$("change-slot").addEventListener("change", controls);
$("clear-change-selections").addEventListener("click", () => {
  if (!inChanges() || active() || state.refreshing || state.pairPending) return;
  state.focus = []; $("change-slot").value = "before"; renderSelections();
  $("changes-feedback").textContent = "已明確清除全部選段。可依序手動加入 HEAD、目前對應原碼與目前呼叫端；原碼與問題未修改。";
});
$("extend-selection").addEventListener("click", () => { if (!$("extend-selection").disabled) { state.extending = !state.extending; controls(); } });
$("expand-definition").addEventListener("click", () => {
  const selected = definitionTarget;
  if ($("expand-definition").disabled || active() || state.refreshing || !selected || selected.source !== state.source || selected.version !== state.project?.version || selected.source.version !== selected.version || selected.anchor !== state.anchor || selected.end !== state.end) return;
  selectDefinition(selected.target, selected.source);
});
$("source-back").addEventListener("click", async () => {
  if ($("source-back").disabled) return;
  const position = state.readingTrail.at(-1);
  const source = await openFile(position.file, position.version, null, null, position);
  if (source && state.source === source && state.readingTrail.at(-1) === position) { state.readingTrail.pop(); controls(); }
});
$("page-prev").addEventListener("click", () => { if (state.source && state.page > 0) { state.page--; renderCode(); $("code").scrollTop = 0; } });
$("page-next").addEventListener("click", () => { if (state.source && (state.page + 1) * pageSize < state.source.lines.length) { state.page++; renderCode(); $("code").scrollTop = 0; } });
$("add-selection").addEventListener("click", () => {
  const target = selectionTarget;
  if ($("add-selection").disabled || active() || experimentBusy() || state.refreshing || state.pairPending || !target || target.project !== state.project || target.source !== state.source ||
      target.version !== state.project?.version || state.source.version !== target.version || target.anchor !== state.anchor || target.end !== state.end || target.role !== $("change-slot").value ||
      target.focus.length !== state.focus.length || target.focus.some((item, index) => item !== state.focus[index])) return;
  const selection = { file: state.source.file.id, path: state.source.path, start: Math.min(state.anchor, state.end), end: Math.max(state.anchor, state.end) };
  if (inChanges()) {
    selection.role = $("change-slot").value;
    state.focus = [...state.focus.filter(item => item.role !== selection.role), selection].sort((left, right) => changeRoles.indexOf(left.role) - changeRoles.indexOf(right.role));
    const missing = changeRoles.find(role => !state.focus.some(item => item.role === role));
    if (missing) $("change-slot").value = missing;
    showError(""); renderSelections(); return;
  }
  const selections = target.chunks.map(chunk => ({...selection, ...chunk}));
  if (selections.some(chunk => state.focus.some(item => item.file === chunk.file && item.start === chunk.start && item.end === chunk.end))) { showError("部分範圍已經加入選段；請移除重複選段再加入，原有選段未變動。"); return; }
  state.focus = [...state.focus, ...selections]; showError(""); renderSelections();
});
function validKeywordDefinitions(result, project) {
  const shape = (value, keys) => value && typeof value === "object" && !Array.isArray(value) && Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
  const text = (value, limit) => typeof value === "string" && value.trim().length > 0 && [...value].length <= limit && !/[\r\n\0]/.test(value);
  if (!shape(result, ["version", "mode", "terms", "matches", "truncated", "total_matches", "unavailable_files"]) ||
      result.version !== project.version || result.mode !== "keywords" || new TextEncoder().encode(JSON.stringify(result)).length > 65536 ||
      !Array.isArray(result.terms) || !result.terms.length || result.terms.length > 8 || result.terms.some(term => !text(term, 384)) || new Set(result.terms).size !== result.terms.length ||
      !Array.isArray(result.matches) || result.matches.length > 40 || typeof result.truncated !== "boolean" ||
      !Number.isSafeInteger(result.total_matches) || result.total_matches < 0 || result.matches.length !== Math.min(result.total_matches, 40) || result.truncated !== (result.total_matches > 40) ||
      !Number.isSafeInteger(result.unavailable_files) || result.unavailable_files < 0 || result.unavailable_files > project.files.length) return false;
  const identities = new Set();
  return result.matches.every(match => {
    if (!shape(match, ["file", "path", "name", "kind", "start_line", "definition_line", "end_line", "stub", "matched_terms", "preview"])) return false;
    const file = project.files.find(file => file.id === match.file && file.path === match.path);
    if (!file || !/\.pyi?$/i.test(file.path) || !text(match.name, 65536) || !["function", "async function"].includes(match.kind) || typeof match.stub !== "boolean" ||
        ![match.start_line, match.definition_line, match.end_line, file.lines].every(Number.isSafeInteger) ||
        !(1 <= match.start_line && match.start_line <= match.definition_line && match.definition_line <= match.end_line && match.end_line <= file.lines) ||
        !Array.isArray(match.matched_terms) || match.matched_terms.length !== result.terms.length ||
        !shape(match.preview, ["line", "text"]) || !Number.isSafeInteger(match.preview.line) || match.preview.line < match.start_line || match.preview.line > match.end_line ||
        typeof match.preview.text !== "string" || [...match.preview.text].length > 240 || /[\r\n\0]/.test(match.preview.text)) return false;
    const key = JSON.stringify([match.file, match.start_line, match.definition_line, match.end_line, match.name]);
    if (identities.has(key)) return false;
    identities.add(key);
    return match.matched_terms.every((hit, index) => shape(hit, ["term", "field", "line"]) && hit.term === result.terms[index] &&
      (hit.field === "source" ? Number.isSafeInteger(hit.line) && hit.line >= match.start_line && hit.line <= match.end_line :
        ["name", "path"].includes(hit.field) && hit.line === null));
  });
}
function currentKeywordSearch(view) {
  return state.project === view.project && state.project.version === view.version && state.searchRequest === view.request &&
    $("search-mode").value === "keywords" && $("search-query").value.trim() === view.query && !state.refreshing;
}
async function keywordSourceAction(view, match, button) {
  if (!currentKeywordSearch(view) || button.disabled || state.pending || state.experiment?.preparing) return;
  const opening = view.opening = (view.opening || 0) + 1, sourceRequest = state.sourceRequest, anchor = state.anchor, end = state.end;
  const questionRevision = state.questionRevision, focus = state.focus, focusKey = JSON.stringify(focus);
  const trial = state.experiment, trialId = trial?.job?.id, prepareRequest = trial?.prepareRequest;
  const current = () => currentKeywordSearch(view) && view.opening === opening && state.sourceRequest === sourceRequest &&
    state.anchor === anchor && state.end === end && state.focus === focus && JSON.stringify(state.focus) === focusKey && state.questionRevision === questionRevision &&
    state.experiment === trial && trial?.job?.id === trialId && trial?.prepareRequest === prepareRequest && !state.experiment?.preparing && !state.pending;
  const file = view.project.files.find(file => file.id === match.file && file.path === match.path);
  const sameFile = state.source?.file.id === file.id && state.source.version === view.version;
  const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 10000);
  button.disabled = true;
  try {
    // Fetch before navigating so an obsolete search cannot replace the active source.
    const source = sameFile ? state.source : await api(`/api/source?${new URLSearchParams({file: file.id, version: view.version})}`, undefined, controller.signal);
    if (!current()) return;
    if (source?.version !== view.version || source.path !== file.path || !Array.isArray(source.lines) || source.lines.length !== file.lines ||
        source.lines.some(line => typeof line !== "string") || source.lines.reduce((sum, line) => sum + line.length + 1, 0) > 6 * 262144 ||
        source.outline?.status !== "available" || !Array.isArray(source.outline.items) || !source.outline.items.some(item => item && ["name", "kind", "start_line", "definition_line", "end_line", "stub"].every(key => item[key] === match[key])))
      throw new Error("原碼版本或定義座標不符；未更動目前原碼與選取。");
    if (sameFile) selectDefinition(match, source);
    else {
      const ready = openFile(file, view.version, null, null, null, source), openedRequest = state.sourceRequest;
      const opened = await ready;
      if (currentKeywordSearch(view) && view.opening === opening && state.sourceRequest === openedRequest && !state.pending && !state.experiment?.preparing)
        selectDefinition(match, opened, false);
    }
  } catch (error) {
    if (current()) showError(controller.signal.aborted ? "原碼讀取超過 10 秒；原有閱讀狀態保留，沒有自動重試。" : error.message);
  } finally { clearTimeout(timeout); if (currentKeywordSearch(view)) controls(); }
}
async function searchKeywordDefinitions(reveal) {
  const view = {project: state.project, version: state.project?.version, query: $("search-query").value.trim(), request: ++state.searchRequest};
  $("search-results").replaceChildren(); $("search-summary").textContent = "";
  if (!view.project || !view.query || state.refreshing) return;
  if ([...view.query].length > 128) { showError("搜尋最多 128 字元；原稿保留，請縮短後再搜尋。"); return; }
  showError(""); $("search-summary").textContent = "以全部關鍵字搜尋定義中；不使用模型…";
  const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 10000);
  try {
    const result = await api(`/api/definitions?${new URLSearchParams({q: view.query, version: view.version, mode: "keywords"})}`, undefined, controller.signal);
    if (!currentKeywordSearch(view)) return;
    if (!validKeywordDefinitions(result, view.project)) throw new Error("關鍵字回報的版本、來源座標或顯示上限不符；未展示部分或其他版本的結果。");
    $("search-summary").textContent = `全部詞：${result.terms.join(" · ")}。顯示 ${result.matches.length} / ${result.total_matches} 個函式${result.truncated ? "（已達顯示上限，請縮小搜尋）" : ""}。純字面命中，不證明用途或實際呼叫目標。${result.unavailable_files ? ` ${result.unavailable_files} 個 Python 檔案無可用定義清單，可改用文字搜尋。` : ""}`;
    for (const match of result.matches) {
      const button = element("button", "", "search-hit definition-hit keyword-hit"); button.type = "button"; button.dataset.definitionLine = match.definition_line;
      button.dataset.definitionName = match.name; button.dataset.definitionPath = match.path; button.title = `${match.name} · ${match.path}`;
      button.append(element("span", `${match.name} · ${match.path}`), element("small", `${definitionKinds[match.kind]} · 定義 L${match.definition_line} · L${match.start_line}–L${match.end_line}${match.stub ? " · 省略內容" : ""}`),
        element("small", "命中：" + match.matched_terms.map(hit => `${hit.term}（${hit.field === "source" ? `原碼 L${hit.line}` : hit.field === "name" ? "名稱" : "路徑"}）`).join(" · ")),
        element("small", `原碼 L${match.preview.line} 預覽（非完整定義）：${match.preview.text}`));
      button.addEventListener("click", () => keywordSourceAction(view, match, button)); $("search-results").append(button);
    }
    controls(); if (reveal) $("search-summary").scrollIntoView({block: "center"});
  } catch (error) {
    if (currentKeywordSearch(view)) { showError(controller.signal.aborted ? "關鍵字搜尋超過 10 秒；沒有自動重試，請縮小範圍後再試。" : error.message); $("search-summary").textContent = "搜尋未完成"; }
  } finally { clearTimeout(timeout); }
}
const callSkipReasons = {not_python: "非 Python", source_size: "檔案大小", nonphysical_lines: "換行格式", syntax: "語法／版本", node_limit: "語法節點上限", encoding: "編碼", coordinates: "座標無法確認"};
function validCallQuery(query) {
  return typeof query === "string" && [...query].length <= 128 && /^[_\p{XID_Start}][_\p{XID_Continue}]*$/u.test(query);
}
function validCallSearch(result, view) {
  const shape = (value, keys) => value && typeof value === "object" && !Array.isArray(value) && Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
  const count = value => Number.isSafeInteger(value) && value >= 0;
  if (!shape(result, ["version", "mode", "query", "column_unit", "semantics_verified", "matches", "total_matches", "truncated", "inspected_files", "skipped_files", "uninspected_files", "total_files", "stop_reason"]) ||
      result.version !== view.version || result.mode !== "calls" || result.query !== view.query || result.column_unit !== "utf8_bytes" || result.semantics_verified !== false ||
      new TextEncoder().encode(JSON.stringify(result)).length > 65536 || !Array.isArray(result.matches) || result.matches.length > 40 ||
      !count(result.total_matches) || result.total_matches > 500000 || result.matches.length !== Math.min(40, result.total_matches) || result.truncated !== (result.total_matches > result.matches.length) ||
      ![result.inspected_files, result.uninspected_files, result.total_files].every(count) || result.total_files !== view.project.files.length ||
      !result.skipped_files || typeof result.skipped_files !== "object" || Array.isArray(result.skipped_files) ||
      Object.entries(result.skipped_files).some(([reason, number]) => !Object.hasOwn(callSkipReasons, reason) || !count(number) || number === 0) ||
      result.inspected_files + Object.values(result.skipped_files).reduce((sum, number) => sum + number, 0) + result.uninspected_files !== result.total_files ||
      ![null, "time_limit", "node_budget"].includes(result.stop_reason) || (result.stop_reason === null) !== (result.uninspected_files === 0) ||
      result.inspected_files === 0 && result.total_matches !== 0) return false;
  const identities = new Set(), hashes = new Map(), nameBytes = new TextEncoder().encode(view.query).length;
  const before = (line, column, lastLine, lastColumn) => line < lastLine || line === lastLine && column <= lastColumn;
  return result.matches.every(match => {
    if (!shape(match, ["file", "path", "source_sha256", "kind", "name", "start_line", "start_column", "end_line", "end_column", "name_line", "name_column", "preview"])) return false;
    const file = view.project.files.find(file => file.id === match.file && file.path === match.path);
    if (!file || !/\.pyi?$/i.test(file.path) || !["name", "attribute"].includes(match.kind) || match.name !== view.query ||
        typeof match.source_sha256 !== "string" || !/^[0-9a-f]{64}$/.test(match.source_sha256) ||
        ![match.start_line, match.end_line, match.name_line, file.lines].every(Number.isSafeInteger) ||
        !(1 <= match.start_line && match.start_line <= match.name_line && match.name_line <= match.end_line && match.end_line <= file.lines) ||
        ![match.start_column, match.end_column, match.name_column].every(column => count(column) && column <= 262144) ||
        match.name_column + nameBytes > 262144 || !before(match.start_line, match.start_column, match.name_line, match.name_column) ||
        !before(match.name_line, match.name_column + nameBytes, match.end_line, match.end_column) ||
        typeof match.preview !== "string" || [...match.preview].length > 240 || [...match.preview].some(char => char !== " " && /[\p{C}\p{Z}]/u.test(char))) return false;
    const identity = JSON.stringify([match.file, match.start_line, match.start_column, match.end_line, match.end_column]);
    if (identities.has(identity) || hashes.has(match.file) && hashes.get(match.file) !== match.source_sha256) return false;
    identities.add(identity); hashes.set(match.file, match.source_sha256); return true;
  }) && hashes.size <= result.inspected_files;
}
function currentCallSearch(view) {
  return state.project === view.project && state.project.version === view.version && state.searchRequest === view.request &&
    $("search-mode").value === "calls" && $("search-query").value.trim() === view.query && !state.refreshing;
}
async function callSourceAction(view, match, button) {
  if (!currentCallSearch(view) || button.disabled || state.pending || state.experiment?.preparing) return;
  const opening = view.opening = (view.opening || 0) + 1, source = state.source, trial = state.experiment;
  // Explicit source/focus/draft/target changes invalidate this GET, including ordinary selection/input ABA.
  const stamp = () => JSON.stringify([state.sourceRequest, state.statusRequest, state.historyRequest, state.readingExportRevision, state.contextRequest,
    state.callSelectionRevision, state.questionRevision, state.pairRequest, state.anchor, state.end, state.page, state.focus, $("question").value, $("change-slot").value,
    trial?.prepareRequest, trial?.statusRequest, trial?.inputRevision, trial?.traceRevision, trial?.generatorRevision, trial?.input, $("experiment-input").value, trial?.visible, trial?.moduleDraft,
    trial?.job?.id, trial?.job?.status, trial?.job?.result_text, trial?.baseline?.id, trial?.inputComparison?.revision]);
  const captured = stamp(), target = trial?.target, job = trial?.job, baseline = trial?.baseline;
  const current = () => currentCallSearch(view) && view.opening === opening && state.source === source && state.experiment === trial &&
    trial?.target === target && trial?.job === job && trial?.baseline === baseline && stamp() === captured && !state.pending && !trial?.preparing;
  const file = view.project.files.find(file => file.id === match.file && file.path === match.path);
  const sameFile = source?.file.id === file.id && source.version === view.version;
  const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 10000);
  button.dataset.opening = "true"; button.disabled = true;
  try {
    const retained = sameFile ? source : await api(`/api/source?${new URLSearchParams({file: file.id, version: view.version})}`, undefined, controller.signal);
    if (!current()) return;
    // GET serves the admitted display cache, not raw bytes. UTF-8 columns are metadata, never JS string offsets.
    if (retained?.version !== view.version || retained.path !== file.path || !Array.isArray(retained.lines) || retained.lines.length !== file.lines ||
        retained.lines.some(line => typeof line !== "string") || retained.lines.reduce((sum, line) => sum + line.length + 1, 0) > 6 * 262144)
      throw new Error("呼叫原碼的版本、路徑或行數不符；原有閱讀與試跑狀態保留。");
    const hint = match.end_line - match.start_line >= 80 ? `呼叫 L${match.start_line}–L${match.end_line} 超過 80 行；僅定位起點，請自行選取較小範圍。` : "";
    // Prepared data makes openFile's mutation synchronous; no second fetch can race a newer user action.
    await openFile(file, view.version, match.start_line, match.end_line, null, retained, hint);
  } catch (error) {
    if (current()) showError(controller.signal.aborted ? "呼叫原碼讀取超過 10 秒；閱讀與試跑草稿保留，沒有自動重試。" : error.message);
  } finally {
    clearTimeout(timeout); button.dataset.opening = "false";
    if (currentCallSearch(view)) controls();
  }
}
async function searchCalls(reveal) {
  const view = {project: state.project, version: state.project?.version, query: $("search-query").value.trim(), request: ++state.searchRequest};
  $("search-results").replaceChildren(); $("search-summary").textContent = "";
  if (!view.project || !view.query || state.refreshing) return;
  if (!validCallQuery(view.query)) { showError("請輸入單一 Python 名稱（最多 128 字元），例如 merge_setting，不含物件前綴；未送出搜尋。"); return; }
  showError(""); $("search-summary").textContent = "正在檢查專案內的同名呼叫；不使用模型、不執行原碼…";
  const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 10000);
  try {
    const result = await api(`/api/definitions?${new URLSearchParams({q: view.query, version: view.version, mode: "calls"})}`, undefined, controller.signal);
    if (!currentCallSearch(view)) return;
    if (!validCallSearch(result, view)) throw new Error("呼叫搜尋回報的版本、座標或涵蓋數量不符；未展示部分或其他版本的結果。");
    const skipped = Object.entries(result.skipped_files).filter(([, number]) => number).map(([reason, number]) => `${callSkipReasons[reason]} ${number}`).join("、");
    $("search-summary").textContent = `顯示 ${result.matches.length} / ${result.total_matches} 個位置${result.truncated ? "（顯示受限）" : ""}。已檢查 ${result.inspected_files} / ${result.total_files} 檔；未檢查 ${result.uninspected_files}${skipped ? `；略過：${skipped}` : ""}。${result.stop_reason ? `${result.stop_reason === "time_limit" ? "時間" : "總節點"}上限停止。` : ""}未涵蓋不代表沒有呼叫。原始檔 UTF-8 位元組欄：從 0 起、終點不含。`;
    for (const match of result.matches) {
      const button = element("button", "", "search-hit definition-hit call-hit"); button.type = "button";
      button.dataset.callPath = match.path; button.dataset.callKind = match.kind;
      for (const [key, value] of Object.entries({callStartLine: match.start_line, callStartColumn: match.start_column, callEndLine: match.end_line, callEndColumn: match.end_column})) button.dataset[key] = value;
      button.title = `${match.name} · ${match.path} · L${match.start_line}:${match.start_column}–L${match.end_line}:${match.end_column}`;
      button.append(element("span", `${match.name} · ${sourceLabel(match.path)}`),
        element("small", `${match.kind === "name" ? "直接名稱呼叫" : "屬性名稱呼叫（接收物件未解析）"} · L${match.start_line}:${match.start_column}–L${match.end_line}:${match.end_column}`),
        element("small", `呼叫預覽（非完整原碼）：${match.preview}`));
      button.addEventListener("click", () => callSourceAction(view, match, button)); $("search-results").append(button);
    }
    controls(); if (reveal) $("search-summary").scrollIntoView({block: "center"});
  } catch (error) {
    if (currentCallSearch(view)) { showError(controller.signal.aborted ? "呼叫搜尋超過 10 秒；沒有自動重試，可縮小專案範圍後再搜尋。" : error.message); $("search-summary").textContent = "搜尋未完成；不能據此判定沒有呼叫。"; }
  } finally { clearTimeout(timeout); }
}
async function searchSource(reveal = false) {
  if ($("search-mode").value === "calls") return searchCalls(reveal);
  if ($("search-mode").value === "keywords") return searchKeywordDefinitions(reveal);
  const query = $("search-query").value.trim(), request = ++state.searchRequest, definitions = $("search-mode").value === "definitions";
  $("search-results").replaceChildren(); $("search-summary").textContent = "";
  if (!query || !state.project) return;
  showError("");
  const version = state.project.version; $("search-summary").textContent = "搜尋中…";
  try {
    const result = await api(definitions ? `/api/definitions?${new URLSearchParams({ q: query, version })}` : `/api/search?${new URLSearchParams({ q: query })}`);
    if (request !== state.searchRequest || version !== state.project.version) return;
    if (definitions && result.version !== version) throw new Error("定義清單版本不一致；請重新整理快照後再搜尋。");
    $("search-summary").textContent = `${result.matches.length} 個${definitions ? "同名定義" : "結果"}${result.truncated ? "（已達上限，請縮小關鍵字）" : ""}` + (definitions ? `。僅搜尋可解析的 Python 定義；名稱相同不代表實際呼叫目標。${result.unavailable_files ? `${result.unavailable_files} 個 Python 檔案未建立清單，可改用文字搜尋。` : ""}` : "");
    for (const match of result.matches) {
      const file = state.project.files.find(item => item.id === match.file); if (!file) continue;
      const button = element("button", "", definitions ? "search-hit definition-hit" : "search-hit"); button.type = "button";
      if (definitions) {
        button.dataset.definitionLine = match.definition_line;
        button.append(element("span", `${match.name} · ${match.path}`), element("small", `${definitionKinds[match.kind] || match.kind} · 定義 L${match.definition_line} · L${match.start_line}–L${match.end_line}${match.stub ? " · 省略內容" : ""}`));
        button.addEventListener("click", async () => {
          if (request !== state.searchRequest || version !== state.project.version || state.pending || state.refreshing) return;
          const sameFile = state.source?.file.id === file.id && state.source.version === version;
          const source = sameFile ? state.source : await openFile(file, version);
          if (request === state.searchRequest && version === state.project.version) selectDefinition(match, source, sameFile);
        });
      } else {
        button.append(element("span", `${match.path}:${match.line}`), element("small", match.text));
        button.addEventListener("click", () => openFile(file, version, match.line));
      }
      $("search-results").append(button);
    }
    controls();
    if (reveal) $("search-summary").scrollIntoView({block: "center"});
  } catch (error) { if (request === state.searchRequest) { showError(error.message); $("search-summary").textContent = "搜尋未完成"; } }
}
$("search-form").addEventListener("submit", event => { event.preventDefault(); return searchSource(); });
$("find-selection").addEventListener("pointerdown", event => event.preventDefault());
$("find-selection").addEventListener("click", () => {
  if ($("find-selection").disabled) return;
  const selection = document.getSelection(), range = selection?.rangeCount === 1 ? selection.getRangeAt(0) : null;
  const insideCode = node => $("code").contains(node) && (node.nodeType === 3 ? node.parentElement : node).closest("code");
  const name = selection?.toString().trim() || "";
  if (!range || !insideCode(range.startContainer) || !insideCode(range.endContainer) || name.length > 128 || !/^[_\p{XID_Start}][_\p{XID_Continue}]*(?:\.[_\p{XID_Start}][_\p{XID_Continue}]*)*$/u.test(name)) {
    showError("請先在目前原碼內反白一個名稱，例如 merge_setting；也可在左側選「同名定義」並輸入名稱。未使用原碼外的選取文字。"); return;
  }
  showError(""); $("search-mode").value = "definitions"; searchModeChanged(); $("search-query").value = name; return searchSource(true);
});
async function submitQuestion(kind = "explain") {
  if (browseOnly()) return;
  const locate = kind === "locate", project = kind === "project";
  if (state.continuation && kind !== "explain") return;
  const continuation = state.continuation;
  if (continuation && (continuation.version !== state.project?.version || !readingScopeRanges(continuation.scope))) { clearContinuation("沿用原碼的版本或範圍不符；已回到手動選段，未送出問題。"); return; }
  if (inChanges() && (kind !== "explain" || comparisonIssue())) return;
  if ($("question").value.length > questionLimit()) { showError(`問題超過 ${questionLimit()} 字元；原稿已保留，未送出或裁切。`); return; }
  if ($(project ? "ask-project" : locate ? "locate" : "ask").disabled) return;
  const submittedKind = continuation ? "continue" : kind;
  const submission = { previous: state.job?.id || null, kind: submittedKind, error: null };
  state.submission = submission;
  clearSourceContext();
  state.pending = true; state.statusRequest++; state.selectedHistory = null; state.rendered = ""; draft = null; showError("");
  const request = state.statusRequest;
  $("answer").replaceChildren(element("p", continuation ? "正在送出新問題，沿用指定前題的完整原碼，不重新定位…" : project ? "正在送出專案問題；將先定位，再讀取完整候選回答…" : locate ? "正在送出定位問題；不需先選取程式碼…" : "正在送出你選取的程式碼與問題…", "muted"));
  renderJob({ id: null, kind: submittedKind, status: "unknown", phase: "正在送出問題，等待本機服務確認。" });
  try {
    const payload = { question: $("question").value, version: state.project.version };
    if (continuation) payload.parent = continuation.parent_id;
    else if (!locate && !project) payload.focus = state.focus.map(({ file, start, end }) => ({ file, start, end }));
    const job = await jobApi(continuation ? "/api/continue-question" : project ? "/api/project-question" : locate ? "/api/locate" : "/api/jobs", payload);
    if (state.submission !== submission) return;
    if (!job || !validJobId(job.id) || job.id === submission.previous) throw new Error("本機未回傳可辨識的新工作；正在查詢狀態，不會重送問題。");
    state.submission = null;
    if (state.selectedHistory === null) $("answer").replaceChildren(element("p", continuation ? "模型正在閱讀前題的完整原碼範圍，回答這次的新問題…" : project ? "專案問題已送出；定位、準備原碼與回答是同一題，可以隨時取消。" : locate ? "模型正在從目錄挑選閱讀候選；尚未讀取函式實作…" : "模型正在閱讀你選取的程式碼…", "muted"));
    renderJob({ id: job.id, kind: submittedKind, status: "running", phase: "工作已送出，等待本機處理。" });
  } catch (error) {
    if (state.submission === submission) {
      submission.error = error.message;
      submission.uncertain = !Number.isInteger(error.httpStatus) || error.httpStatus >= 500;
      showError(error.message);
    }
  }
  finally { if (request === state.statusRequest) { state.pending = false; controls(); poll(); } }
}
$("question-form").addEventListener("submit", event => { event.preventDefault(); return submitQuestion(); });
$("locate").addEventListener("click", () => submitQuestion("locate"));
$("ask-project").addEventListener("click", () => submitQuestion("project"));
$("cancel").addEventListener("click", async () => {
  if (browseOnly() || $("cancel").disabled) return;
  $("cancel").disabled = true; const request = ++state.statusRequest;
  const identifier = state.job.id;
  try {
    await jobApi(`/api/jobs/${encodeURIComponent(identifier)}/cancel`, {});
    if (request === state.statusRequest && state.job.id === identifier && active()) renderJob({ ...state.job, status: "cancelling", phase: "取消已送出，等待本機模型停止與清理。" });
  }
  catch (error) { if (request === state.statusRequest) { showError(error.message); controls(); } }
  poll();
});
async function refreshProject(mode) {
  if ($("refresh").disabled || browseOnly() && mode === "changes") return;
  invalidateReadingNote();
  const previousVersion = $("project-version").textContent;
  if (state.experiment) { state.experiment.prepareRequest++; state.experiment.preparing = false; state.experiment.statusRequest++; }
  clearSourceContext();
  state.refreshing = true; state.statusRequest++; state.historyRequest++; showError(""); controls();
  $("project-version").textContent = mode === "changes" || (mode === undefined && inChanges()) ? "正在建立 HEAD／目前比較，不啟動模型…" : "正在重新讀取已儲存原碼，不啟動模型…";
  try { showProject(await api("/api/refresh", mode === undefined ? {} : {mode}), true); }
  catch (error) { $("project-version").textContent = previousVersion; showError(error.message.includes("comparison_preparation_unavailable") ?
    "無法建立比較，原有閱讀狀態已保留。請確認是已有本機 HEAD 的一般 Git checkout、使用原生 Git 2.43 以上，且不是 linked worktree 或 partial clone；一般閱讀仍可使用。若狀態目錄設在模型資產內，請先將 Forge8 state 設定在資產目錄之外。" : error.message); }
  finally { state.refreshing = false; controls(); }
}
$("refresh").addEventListener("click", () => refreshProject());
$("change-mode").addEventListener("click", () => { if (!$("change-mode").disabled) return refreshProject(inChanges() ? "source" : "changes"); });
(async () => {
  try { showProject(await api("/api/project")); if (browseOnly()) return; const historyRequest = state.historyRequest; await poll(); if (state.historyRequest === historyRequest) await loadHistory(); }
  catch (error) { showError(error.message); $("project-name").textContent = "無法開啟閱讀桌"; }
})();
