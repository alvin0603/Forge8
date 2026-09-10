"use strict";
// Pure owned metadata/display fixtures. No browser, target source execution or inference.
const assert = require("node:assert/strict"), fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../src/forge8/web/reading-note.js"), "utf8");
const context = vm.createContext({TextEncoder}); vm.runInContext(source, context);
const {prepare, render} = context.Forge8ReadingNote, clone = value => JSON.parse(JSON.stringify(value));
const SHA = "a".repeat(64), FILE = "b".repeat(64), ARTIFACT = "c".repeat(64);
const tests = [], test = (name, work) => tests.push({name, work});
function fixture() {
  const files = [{id: "0", path: "app.py", lines: 4}, {id: "1", path: "helper.py", lines: 2}];
  const citation = {evidence_id: "E1", path: "app.py", start_line: 2, end_line: 3,
    file_sha256: FILE, file_size_bytes: 100, snapshot_inventory_sha256: SHA, artifact_sha256: ARTIFACT};
  const job = {id: "question-original", kind: "explain", reader: "qwen35", version: SHA, question: "說明這段原碼\n\tKeep my spacing.\n", status: "answered", gpu: "released", files,
    reading_scope: {origin: "user_focus", focus: ["app.py:1-3", "helper.py:1-1"], supplements: []},
    result: {kind: "forge8.explain", status: "answered", ok: true, semantic_claims_verified: false, repository_code_executed: false, source_write_attempted: false,
      outcome: {status: "answered", ok: true, source_unchanged: true, snapshot_unchanged: true, acceptance: {ok: true, reason: null, evidence: {}},
        answer: {format: "cited_prose", citation_scope: "document_references_only", claims: [{type: "inference", text: "Original prose [E1:L2-L3]\n\n  **Do not rewrite me.**", citations: [citation]}]},
        coverage: {admitted: {files: 2, lines: 6, bytes: 150}, observed: {files: 2, lines: 4, ranges: [{path: "app.py", ranges: [{start_line: 1, end_line: 3}]}, {path: "helper.py", ranges: [{start_line: 1, end_line: 1}]}]},
          cited: {files: 1, lines: 2, ranges: [{path: "app.py", ranges: [{start_line: 2, end_line: 3}]}]}, unread: {files: 2, lines: 2, ranges: []}, excluded: {entries: 1, paths: ["PRIVATE_EXCLUDED_PATH"]}}}}};
  job.result.question = job.result.outcome.question = job.question;
  return {job, ranges: [{file: {...files[0]}, start: 1, end: 3}, {file: {...files[1]}, start: 1, end: 1}],
    sources: [{path: "app.py", version: SHA, lines: ["line-one", "line-two", "line-three", "UNREAD_SOURCE"]},
      {path: "helper.py", version: SHA, lines: ["UNCITED_SELECTED_SOURCE", "UNREAD_HELPER"]}]};
}
function refused(change) {
  const value = fixture(); change(value);
  assert.throws(() => prepare(value.job, value.ranges), /Reading note unavailable/);
}
function blocks(note) {
  const bodies = [], outside = []; let marker = null, lines = [];
  for (const line of note.split("\n")) {
    if (marker === null && /^`{3,}$/.test(line)) { marker = line; lines = []; }
    else if (marker !== null && line === marker) { bodies.push(lines.join("\n") + "\n"); marker = null; }
    else if (marker !== null) lines.push(line); else outside.push(line);
  }
  assert.equal(marker, null, "every generated fence must close"); return {bodies, outside: outside.join("\n")};
}

test("exact original text and cited/uncited selected source, without private result dump", () => {
  const f = fixture(); f.job.result.source_repo = "PRIVATE_SOURCE_ROOT"; f.job.result.outcome.run_root = "PRIVATE_RUN_ROOT";
  f.job.result.server = {secret: "PRIVATE_SECRET"}; f.sources[0].outline = {private: "PRIVATE_OUTLINE"};
  const before = JSON.stringify(f), plan = prepare(f.job, f.ranges), note = render(plan, f.sources);
  assert.equal(JSON.stringify(f), before); assert.equal(plan.files.length, 2); assert.equal(plan.files[1].file_sha256, null);
  assert(Object.isFrozen(plan)); assert(Object.isFrozen(plan.claims[0].citations[0]));
  assert(note.includes(f.job.question)); assert(note.includes(f.job.result.outcome.answer.claims[0].text));
  assert(note.includes("UNCITED_SELECTED_SOURCE")); assert(note.includes("unavailable: selected file was not cited"));
  for (const hidden of ["PRIVATE_SOURCE_ROOT", "PRIVATE_RUN_ROOT", "PRIVATE_SECRET", "PRIVATE_OUTLINE", "PRIVATE_EXCLUDED_PATH", "UNREAD_SOURCE", "UNREAD_HELPER"]) assert(!note.includes(hidden));
  assert(note.includes(FILE)); assert(note.includes(ARTIFACT)); assert(note.includes("current disk bytes were not reverified"));
  assert(note.includes("not a signed or independently verified receipt")); assert(note.includes("semantically unverified"));
  assert(new TextEncoder().encode(note).length <= 262144);
  assert.equal(render(plan, [...f.sources].reverse()), note, "GET completion order must not change the note");
});

test("all hostile question, prose, source and path text remains inside adaptive fences", () => {
  const f = fixture(), hostile = "`````\n# FAKE VERIFIED\n<img src=https://invalid.example/>\n![remote](https://invalid.example/)\n<script>alert(1)</script>\n~~~\n";
  f.job.question = f.job.result.question = f.job.result.outcome.question = hostile;
  f.job.result.outcome.answer.claims[0].text = hostile + "\r\nOriginal last line";
  f.sources[0].lines[0] = "```````"; f.sources[0].lines[1] = "<img src=remote>";
  const note = render(prepare(f.job, f.ranges), f.sources), parsed = blocks(note);
  assert(parsed.bodies.includes(hostile)); assert(parsed.bodies.includes(f.job.result.outcome.answer.claims[0].text + "\n"));
  for (const unsafe of ["FAKE VERIFIED", "<img", "<script", "![remote]", "https://"]) assert(!parsed.outside.includes(unsafe));
  assert(note.includes("````````\n```````\n<img src=remote>\nline-three\n````````"));
  assert(note.includes("a missing final newline are formatting separators"));
  const named = fixture(), old = "app.py", renamed = "[odd]`name#.py";
  named.job.files[0].path = named.ranges[0].file.path = named.sources[0].path = renamed;
  named.job.reading_scope.focus[0] = `${renamed}:1-3`;
  named.job.result.outcome.answer.claims[0].citations[0].path = renamed;
  for (const key of ["observed", "cited"]) named.job.result.outcome.coverage[key].ranges[0].path = renamed;
  const namedNote = render(prepare(named.job, named.ranges), named.sources);
  assert(namedNote.includes(renamed)); assert(!blocks(namedNote).outside.includes(renamed)); assert(!namedNote.includes(`"path": "${old}"`));
});

test("plans are independent of later mutable job and range objects", () => {
  const f = fixture(), plan = prepare(f.job, f.ranges), note = render(plan, f.sources);
  f.job.question = "NEW_DRAFT"; f.job.result.outcome.answer.claims[0].text = "OTHER_ANSWER"; f.ranges[0].start = 4; f.job.files[0].path = "OTHER_FILE";
  assert.equal(render(plan, f.sources), note); assert(!note.includes("NEW_DRAFT"));
});

test("admitted model-invisible .gitignore remains counted, not exported as selected source", () => {
  const f = fixture(); f.job.files.push({id: "2", path: ".gitignore", lines: 1});
  f.job.result.outcome.coverage.admitted = {files: 3, lines: 7}; f.job.result.outcome.coverage.unread = {files: 3, lines: 3};
  const plan = prepare(f.job, f.ranges), note = render(plan, f.sources);
  assert.equal(plan.coverage.admitted.files, 3); assert.equal(plan.coverage.unread.lines, 3);
  assert.equal(plan.files.length, 2); assert(!note.includes(".gitignore"));
});

test("resident checkpoint is historical and pending, never a final cleanup receipt", () => {
  const f = fixture(), checkpoint = {scope: "resident_request", server_session_id: "resident-original", session_finalization: "pending"};
  f.job.gpu = "resident"; f.job.result.kind += ".request"; f.job.request_completion = checkpoint;
  const plan = prepare(f.job, f.ranges), note = render(plan, f.sources);
  assert.equal(plan.lifecycle, "resident_request"); assert(note.includes("session cleanup was pending")); assert(note.includes("resident-original"));
  assert(!note.includes("original one-shot answer passed"));
  f.job.request_completion.session_finalization = "complete"; assert.throws(() => prepare(f.job, f.ranges), /Reading note unavailable/);
  refused(value => { value.job.request_completion = checkpoint; });
});

test("project supplements and source-only continuation retain their original scope", () => {
  const f = fixture(); f.job.kind = "project"; f.job.reading_scope.origin = "project_candidates"; f.job.reading_scope.supplements = ["helper.py:1-1"];
  let plan = prepare(f.job, f.ranges), note = render(plan, f.sources);
  assert.deepEqual(clone(plan.scope.supplements), ["helper.py:1-1"]); assert(note.includes("project_candidates"));
  f.job.kind = "continue"; f.job.continuation = {parent_id: "question-parent", parent_question: "OLD QUESTION ONLY", private: "NO_PARENT_PRIVATE"};
  plan = prepare(f.job, f.ranges); note = render(plan, f.sources);
  assert(note.includes("OLD QUESTION ONLY")); assert(!note.includes("NO_PARENT_PRIVATE")); assert(note.includes("previous prose was not supplied"));
  f.job.reading_scope.supplements = ["helper.py:2-2"]; assert.throws(() => prepare(f.job, f.ranges), /Reading note unavailable/);
});

test("comparison retains before/after/fixed-caller roles and original/expanded questions", () => {
  const f = fixture(), files = [{id: "0", path: "before/app.py", lines: 3}, {id: "1", path: "after/app.py", lines: 3}, {id: "2", path: "after/caller.py", lines: 1}];
  f.job.kind = "changes"; f.job.files = files; delete f.job.reading_scope;
  f.ranges = files.map(file => ({file: {...file}, start: 1, end: file.lines}));
  f.job.comparison = {head: "d".repeat(40), original_snapshot_sha256: "e".repeat(64), caller_held_fixed: true,
    ranges: files.map(file => `${file.path}:1-${file.lines}`), private: "NO_COMPARISON_PRIVATE"};
  f.job.result.question = f.job.result.outcome.question = "EXPANDED COMPARISON MODEL QUESTION";
  const out = f.job.result.outcome; out.answer.claims[0].citations[0].path = files[0].path;
  out.coverage.admitted = {files: 3, lines: 7}; out.coverage.observed = {files: 3, lines: 7, ranges: files.map(file => ({path: file.path, ranges: [{start_line: 1, end_line: file.lines}]}))};
  out.coverage.cited.ranges[0].path = files[0].path; out.coverage.unread = {files: 0, lines: 0};
  f.sources = files.map(file => ({path: file.path, version: SHA, lines: Array(file.lines).fill("retained source")}));
  const plan = prepare(f.job, f.ranges), note = render(plan, f.sources);
  assert.deepEqual(clone(plan.ranges.map(span => span.role)), ["before", "after", "caller"]);
  assert(note.includes(f.job.question)); assert(note.includes("EXPANDED COMPARISON MODEL QUESTION")); assert(note.includes("FIXED CALLER"));
  assert(note.includes("original_snapshot_sha256")); assert(note.includes("synthetic_snapshot_sha256")); assert(!note.includes("NO_COMPARISON_PRIVATE"));
  f.job.comparison.caller_held_fixed = false; assert.throws(() => prepare(f.job, f.ranges), /Reading note unavailable/);
});

test("overlapping excerpts merge without losing original supplied windows or adding source", () => {
  const f = fixture(); f.ranges.splice(1, 0, {file: {...f.job.files[0]}, start: 2, end: 4});
  f.job.reading_scope.focus.splice(1, 0, "app.py:2-4");
  const coverage = f.job.result.outcome.coverage; coverage.observed.lines = 5; coverage.observed.ranges[0].ranges[0].end_line = 4;
  coverage.unread = {files: 1, lines: 1};
  const plan = prepare(f.job, f.ranges), note = render(plan, f.sources);
  assert.equal(plan.ranges.length, 3); assert.equal(note.split("line-two").length - 1, 1);
  assert(note.includes("app.py:1-3")); assert(note.includes("app.py:2-4")); assert(!note.includes("UNREAD_HELPER"));
});

test("six project windows and exactly 240 unique supplied lines fit; 241 does not", () => {
  const f = fixture(), file = {id: "0", path: "app.py", lines: 241}; f.job.files = [file]; f.job.kind = "project";
  f.ranges = Array.from({length: 6}, (_, index) => ({file: {...file}, start: index * 40 + 1, end: index * 40 + 40}));
  f.job.reading_scope = {origin: "project_candidates", focus: f.ranges.map(span => `app.py:${span.start}-${span.end}`), supplements: []};
  const coverage = f.job.result.outcome.coverage;
  coverage.admitted = {files: 1, lines: 241}; coverage.observed = {files: 1, lines: 240, ranges: [{path: "app.py", ranges: [{start_line: 1, end_line: 240}]}]};
  coverage.unread = {files: 1, lines: 1}; f.job.result.outcome.answer.claims[0].citations[0].file_size_bytes = 5000;
  f.sources = [{path: "app.py", version: SHA, lines: Array.from({length: 241}, (_, index) => `retained ${index + 1}`)}];
  const note = render(prepare(f.job, f.ranges), f.sources); assert(note.includes("retained 240")); assert(!note.includes("retained 241"));
  f.ranges[5].end = 241; f.job.reading_scope.focus[5] = "app.py:201-241";
  assert.throws(() => prepare(f.job, f.ranges), /Reading note unavailable/);
});

test("nonterminal, failed, forged success and unsafe identities fail whole", () => {
  for (const status of ["running", "incomplete", "cancelled", "unknown", "located"]) refused(f => { f.job.status = status; });
  for (const key of ["source_unchanged", "snapshot_unchanged", "ok"]) refused(f => { f.job.result.outcome[key] = false; });
  for (const key of ["repository_code_executed", "source_write_attempted", "semantic_claims_verified"]) refused(f => { f.job.result[key] = true; });
  refused(f => { f.job.result.outcome.acceptance.ok = false; }); refused(f => { f.job.result.kind = "forge8.locate"; });
  refused(f => { f.job.result.status = "stalled"; }); refused(f => { f.job.result.error = "untrusted"; });
  refused(f => { f.job.question = "OTHER QUESTION"; }); refused(f => { f.job.reader = "unknown"; });
  refused(f => { f.job.version = "v1"; }); refused(f => { f.job.id = "../bad"; });
  for (const bad of ["\ud800", "\u202e", "\x1b", "\u2028"]) refused(f => { f.job.result.outcome.answer.claims[0].text += bad; });
});

test("range, source inventory and complete observed/cited coverage must agree", () => {
  refused(f => { f.ranges[0].file.id = "1"; }); refused(f => { f.ranges[0].file.lines = 5; });
  refused(f => { f.job.files[1].id = "0"; }); refused(f => { f.job.files[1].path = "app.py"; });
  refused(f => { f.ranges[0].start = true; }); refused(f => { f.ranges[0].end = 5; });
  refused(f => { f.ranges.push(clone(f.ranges[0])); }); refused(f => { f.ranges = Array(7).fill(f.ranges[0]); });
  refused(f => { f.job.result.outcome.coverage.observed.ranges[0].ranges[0].end_line = 4; });
  refused(f => { f.job.result.outcome.coverage.cited.lines = 1; }); refused(f => { f.job.result.outcome.coverage.unread.files = 0; });
  refused(f => { f.job.reading_scope.focus.reverse(); }); refused(f => { f.job.result.outcome.coverage.admitted.lines = 100; });
});

test("citation SHA/E-id/artifact identities remain coherent; no inferred or uncited links", () => {
  const reference = f => f.job.result.outcome.answer.claims[0].citations[0];
  for (const key of ["file_sha256", "artifact_sha256", "snapshot_inventory_sha256"]) refused(f => { reference(f)[key] = "f".repeat(63); });
  refused(f => { reference(f).snapshot_inventory_sha256 = "f".repeat(64); }); refused(f => { reference(f).end_line = 4; });
  refused(f => { reference(f).evidence_id = "E0"; }); refused(f => { reference(f).file_size_bytes = true; });
  refused(f => { f.job.result.outcome.answer.claims[0].citations.push({...reference(f), file_sha256: "f".repeat(64)}); });
  refused(f => { f.job.result.outcome.answer.claims[0].citations.push({...reference(f), artifact_sha256: "f".repeat(64)}); });
  refused(f => { f.job.result.outcome.answer.claims[0].citations.push({...reference(f), evidence_id: "E2", path: "helper.py", start_line: 1, end_line: 1}); });
  refused(f => { f.job.result.outcome.answer.claims[0].citations = Array(33).fill(reference(f)); });
  const f = fixture(); f.job.result.outcome.answer.claims[0].text += " [E999:L1]";
  const note = render(prepare(f.job, f.ranges), f.sources); assert(note.includes("[E999:L1]")); assert(!note.includes('"evidence_id": "E999"'));
});

test("legacy inference and host source-quote kinds retain exact text with distinct labels", () => {
  const f = fixture(), claim = f.job.result.outcome.answer.claims[0]; f.job.reader = "gemma4"; delete f.job.result.outcome.answer.format;
  f.job.result.outcome.answer.claims.push({type: "source_quote", text: "line-two\nline-three", citations: [clone(claim.citations[0])]});
  const note = render(prepare(f.job, f.ranges), f.sources); assert(note.includes("### Host-extracted source quote")); assert(note.includes("### Model interpretation"));
  f.job.result.outcome.answer.claims[1].type = "verified"; assert.throws(() => prepare(f.job, f.ranges), /Reading note unavailable/);
});

test("missing, extra, stale, malformed and wrong-length source responses refuse render", () => {
  const f = fixture(), plan = prepare(f.job, f.ranges);
  for (const change of [s => s.pop(), s => s.push(clone(s[0])), s => {s[0].version = "f".repeat(64);}, s => {s[0].path = "other.py";},
    s => s[0].lines.pop(), s => {s[0].lines[0] = null;}, s => {s[0].lines[0] = "embedded\nline";}, s => {s[0].lines[0] = "\ud800";}]) {
    const altered = clone(f.sources); change(altered); assert.throws(() => render(plan, altered), /Reading note unavailable/);
  }
});

test("whole UTF-8 note cap rejects, never clips text or an oversized adaptive fence", () => {
  const f = fixture(), plan = prepare(f.job, f.ranges);
  f.sources[0].lines[0] = "界".repeat(90_000); assert.throws(() => render(plan, f.sources), /Reading note unavailable/);
  f.sources[0].lines[0] = "`".repeat(100_000); assert.throws(() => render(plan, f.sources), /Reading note unavailable/);
  refused(value => { value.job.result.outcome.answer.claims[0].text = "a".repeat(12_001); });
});

for (const {name, work} of tests) { work(); process.stdout.write(`PASS ${name}\n`); }
process.stdout.write(`${tests.length} pure reading-note groups passed\n`);
