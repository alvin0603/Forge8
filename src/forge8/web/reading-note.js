"use strict";
// Pure archival display-note formatter. Caller owns live UI/lifecycle gates.
(() => {
  const MAX_BYTES = 256 * 1024, encoder = new TextEncoder();
  const fail = () => { throw new Error("Reading note unavailable: incomplete, inconsistent or over-limit retained data."); };
  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const integer = (value, maximum, minimum = 0) => Number.isSafeInteger(value) && value >= minimum && value <= maximum;
  const text = (value, maximum, blank = false) => {
    if (typeof value !== "string" || value.length > maximum * 2 || [...value].length > maximum || (!blank && !value.trim()) ||
        /[\p{Cf}\p{Zl}\p{Zp}\p{Cs}]/u.test(value) || /[\p{Cc}]/u.test(value.replace(/[\r\n\t]/g, ""))) fail();
    return value;
  };
  const identifier = value => { if (typeof value !== "string" || !/^[A-Za-z0-9_-]{1,200}$/.test(value)) fail(); return value; };
  const hash = value => { if (typeof value !== "string" || !/^[0-9a-f]{64}$/.test(value)) fail(); return value; };
  const pathText = value => {
    text(value, 512);
    if (/[\r\n\t\\:]/.test(value) || value.split("/").some(part => !part || part === "." || part === "..")) fail();
    return value;
  };
  const equalSet = (a, b) => a.size === b.size && [...a].every(item => b.has(item));
  const lineKeys = (path, start, end) => {
    if (!integer(start, 1_000_000, 1) || !integer(end, 1_000_000, start) || end - start >= 240) fail();
    return Array.from({length: end - start + 1}, (_, offset) => `${path}\0${start + offset}`);
  };
  const freeze = value => { if (value && typeof value === "object") { Object.values(value).forEach(freeze); Object.freeze(value); } return value; };

  function prepare(job, ranges) {
    const result = job?.result, outcome = result?.outcome;
    if (!object(job) || job.status !== "answered" || job.error || !object(result) || result.status !== "answered" || result.ok !== true || result.error ||
        !object(outcome) || outcome.status !== "answered" || outcome.ok !== true || outcome.source_unchanged !== true || outcome.snapshot_unchanged !== true ||
        outcome.acceptance?.ok !== true || result.semantic_claims_verified !== false || result.repository_code_executed !== false || result.source_write_attempted !== false ||
        !["forge8.explain", "forge8.explain.request"].includes(result.kind)) fail();
    const id = identifier(job.id), version = hash(job.version), question = text(job.question, 2000), kind = job.kind === undefined ? "explain" : job.kind;
    if (!["explain", "project", "continue", "changes"].includes(kind) || !["qwen35", "gemma4", "gemma12b"].includes(job.reader) ||
        !Array.isArray(job.files) || !job.files.length || job.files.length > 1000 || !Array.isArray(ranges) || !ranges.length || ranges.length > 6) fail();
    const inventory = new Map(), ids = new Set();
    for (const file of job.files) {
      if (!object(file) || typeof file.id !== "string" || !/^[0-9]{1,4}$/.test(file.id) || ids.has(file.id) ||
          !integer(file.lines, MAX_BYTES) || inventory.has(file.path)) fail();
      const copy = {id: file.id, path: pathText(file.path), lines: file.lines};
      inventory.set(copy.path, copy); ids.add(copy.id);
    }
    const supplied = new Set(), selectors = [], selected = ranges.map(span => {
      const file = inventory.get(span?.file?.path);
      if (!file || span.file.id !== file.id || span.file.lines !== file.lines || !integer(span.start, file.lines, 1) ||
          !integer(span.end, file.lines, span.start) || span.end - span.start >= 80) fail();
      const selector = `${file.path}:${span.start}-${span.end}`;
      if (selectors.includes(selector)) fail(); selectors.push(selector);
      lineKeys(file.path, span.start, span.end).forEach(key => supplied.add(key));
      return {file: {...file}, start: span.start, end: span.end, role: null};
    });
    if (supplied.size > 240) fail();
    const modelQuestion = text(outcome.question, 2000);
    if (result.question !== modelQuestion) fail();
    let comparison = null, scope = null, continuation = null;
    if (kind === "changes") {
      const value = job.comparison;
      if (!object(value) || typeof value.head !== "string" || !/^(?:[0-9a-f]{40}|[0-9a-f]{64})$/.test(value.head) ||
          value.caller_held_fixed !== true || !Array.isArray(value.ranges) || JSON.stringify(value.ranges) !== JSON.stringify(selectors) || selected.length !== 3 ||
          !selected[0].file.path.startsWith("before/") || selected[1].file.path !== `after/${selected[0].file.path.slice(7)}` || !selected[2].file.path.startsWith("after/")) fail();
      comparison = {head: value.head, original_snapshot_sha256: hash(value.original_snapshot_sha256)};
      selected.forEach((span, index) => { span.role = ["before", "after", "caller"][index]; });
    } else {
      if (job.comparison || modelQuestion !== question) fail();
      const value = job.reading_scope;
      if (!object(value) || !["user_focus", "project_candidates"].includes(value.origin) ||
          JSON.stringify(value.focus) !== JSON.stringify(selectors) || !Array.isArray(value.supplements) || value.supplements.length > 4 ||
          (value.origin === "user_focus" && (value.supplements.length || selected.length > 3))) fail();
      let addedLines = 0;
      const supplements = value.supplements.map(selector => {
        text(selector, 600); const match = selector.match(/^([^:]+):([1-9][0-9]{0,6})-([1-9][0-9]{0,6})$/);
        if (!match) fail();
        const keys = lineKeys(match[1], Number(match[2]), Number(match[3])); addedLines += keys.length;
        if (keys.some(key => !supplied.has(key))) fail(); return selector;
      });
      if (addedLines > 40 || new Set(supplements).size !== supplements.length) fail();
      scope = {origin: value.origin, focus: [...selectors], supplements};
    }
    if (kind === "continue") {
      continuation = {parent_id: identifier(job.continuation?.parent_id), parent_question: text(job.continuation?.parent_question, 2000)};
    }
    const resident = result.kind === "forge8.explain.request", checkpoint = job.request_completion;
    if (resident ? job.gpu !== "resident" || checkpoint?.scope !== "resident_request" || checkpoint.session_finalization !== "pending" :
        job.gpu !== "released" || checkpoint !== undefined || result.request_completion !== undefined || outcome.request_completion !== undefined) fail();
    const session = resident ? identifier(checkpoint.server_session_id) : null;
    const evidenceIds = new Map(), artifacts = new Map(), filePins = new Map(), cited = new Set();
    let citationCount = 0, answerChars = 0;
    if (!Array.isArray(outcome.answer?.claims) || !outcome.answer.claims.length || outcome.answer.claims.length > 10) fail();
    const claims = outcome.answer.claims.map(claim => {
      if (!object(claim) || !["inference", "source_quote"].includes(claim.type) || !Array.isArray(claim.citations) || !claim.citations.length) fail();
      const prose = text(claim.text, claim.type === "source_quote" ? 600 : 12_000); answerChars += [...prose].length;
      const citations = claim.citations.map(reference => {
        if (++citationCount > 32 || !object(reference) || typeof reference.evidence_id !== "string" || !/^E[1-9][0-9]{0,3}$/.test(reference.evidence_id)) fail();
        const file = inventory.get(reference.path), keys = lineKeys(reference.path, reference.start_line, reference.end_line);
        if (!file || reference.end_line > file.lines || reference.end_line - reference.start_line >= 80 || keys.some(key => !supplied.has(key)) ||
            reference.snapshot_inventory_sha256 !== version || !integer(reference.file_size_bytes, MAX_BYTES, 1)) fail();
        keys.forEach(key => cited.add(key));
        const copy = {evidence_id: reference.evidence_id, path: file.path, start_line: reference.start_line, end_line: reference.end_line,
          file_sha256: hash(reference.file_sha256), file_size_bytes: reference.file_size_bytes,
          snapshot_inventory_sha256: version, artifact_sha256: hash(reference.artifact_sha256)};
        const pin = JSON.stringify([copy.path, copy.file_sha256, copy.file_size_bytes]);
        const evidencePin = JSON.stringify([pin, copy.artifact_sha256]);
        if ((filePins.has(copy.path) && filePins.get(copy.path).pin !== pin) || (evidenceIds.has(copy.evidence_id) && evidenceIds.get(copy.evidence_id) !== evidencePin) ||
            (artifacts.has(copy.artifact_sha256) && artifacts.get(copy.artifact_sha256) !== pin)) fail();
        filePins.set(copy.path, {pin, sha256: copy.file_sha256, size: copy.file_size_bytes});
        evidenceIds.set(copy.evidence_id, evidencePin); artifacts.set(copy.artifact_sha256, pin); return copy;
      });
      return {type: claim.type, text: prose, citations};
    });
    if (answerChars > 12_000) fail();
    const coverage = {}, rawCoverage = outcome.coverage;
    for (const name of ["admitted", "observed", "cited", "unread"]) {
      const value = rawCoverage?.[name];
      if (!object(value) || !integer(value.files, inventory.size) || !integer(value.lines, 16 * 1024 * 1024)) fail();
      coverage[name] = {files: value.files, lines: value.lines};
    }
    const covered = name => {
      const rows = rawCoverage[name].ranges, keys = new Set();
      if (!Array.isArray(rows) || !rows.length || rows.length > 6) fail();
      for (const row of rows) {
        const file = inventory.get(row?.path);
        if (!file || !Array.isArray(row.ranges) || !row.ranges.length || row.ranges.length > 32) fail();
        for (const span of row.ranges) {
          if (!object(span) || span.end_line > file.lines) fail();
          lineKeys(file.path, span.start_line, span.end_line).forEach(key => keys.add(key));
          if (keys.size > 240) fail();
        }
      }
      return keys;
    };
    const selectedPaths = new Set(selected.map(span => span.file.path)), citedPaths = new Set(claims.flatMap(claim => claim.citations.map(ref => ref.path)));
    const suppliedCounts = new Map();
    for (const key of supplied) {
      const path = key.slice(0, key.lastIndexOf("\0")); suppliedCounts.set(path, (suppliedCounts.get(path) || 0) + 1);
    }
    const unreadFiles = [...inventory.values()].filter(file => !selectedPaths.has(file.path) || (suppliedCounts.get(file.path) || 0) < file.lines).length;
    if (!equalSet(covered("observed"), supplied) || !equalSet(covered("cited"), cited) || coverage.admitted.files !== inventory.size ||
        coverage.admitted.lines !== [...inventory.values()].reduce((sum, file) => sum + file.lines, 0) || coverage.observed.lines !== supplied.size ||
        coverage.observed.files !== selectedPaths.size || coverage.cited.lines !== cited.size || coverage.cited.files !== citedPaths.size ||
        coverage.unread.files !== unreadFiles || coverage.unread.lines + coverage.observed.lines !== coverage.admitted.lines || !integer(rawCoverage?.excluded?.entries, 1_000_000)) fail();
    coverage.excluded_entries = rawCoverage.excluded.entries;
    const files = [...selectedPaths].map(path => ({...inventory.get(path), file_sha256: filePins.get(path)?.sha256 ?? null, file_size_bytes: filePins.get(path)?.size ?? null}));
    return freeze({id, kind, reader: job.reader, version, question, model_question: comparison ? modelQuestion : null,
      files, ranges: selected, claims, coverage, scope, comparison, continuation, lifecycle: resident ? "resident_request" : "one_shot", server_session_id: session});
  }

  function render(plan, sources) {
    if (!object(plan) || !Array.isArray(plan.files) || !Array.isArray(sources) || sources.length !== plan.files.length) fail();
    const byPath = new Map();
    for (const source of sources) {
      const file = plan.files.find(item => item.path === source?.path);
      if (!file || byPath.has(source.path) || source.version !== plan.version || !Array.isArray(source.lines) || source.lines.length !== file.lines) fail();
      let size = 0;
      for (const line of source.lines) {
        text(line, MAX_BYTES * 6, true);
        if (/[\r\n]/.test(line)) fail(); size += encoder.encode(line).length + 1;
        if (size > MAX_BYTES * 6) fail();
      }
      byPath.set(source.path, source.lines);
    }
    const parts = []; let bytes = 0;
    const append = value => { bytes += encoder.encode(value).length; if (bytes > MAX_BYTES) fail(); parts.push(value); };
    const fenced = value => {
      let width = 3;
      for (const match of value.matchAll(/`+/g)) width = Math.max(width, match[0].length + 1);
      const marker = "`".repeat(width);
      append(`${marker}\n${value}${value.endsWith("\n") ? "" : "\n"}${marker}\n\n`);
    };
    const metadata = value => fenced(JSON.stringify(value, null, 2));
    append("# Forge8 local reading note\n\nThis is an archival display note, not a signed or independently verified receipt. Model claims remain semantically unverified. Review private question/source content before sharing.\n\n");
    append("## Question\n\n"); fenced(plan.question);
    append("## Recorded identity\n\n"); metadata({job_id: plan.id, kind: plan.kind, reader: plan.reader, snapshot_inventory_sha256: plan.version});
    append(plan.lifecycle === "resident_request" ?
      "At answer publication this was a resident-request checkpoint; session cleanup was pending. This note does not assert final release or current model state.\n\n" :
      "The original one-shot answer passed its publication cleanup gate. This note does not recheck that gate or report current model state.\n\n");
    if (plan.server_session_id) metadata({server_session_id: plan.server_session_id});
    if (plan.comparison) {
      append("## HEAD/current comparison\n\nBEFORE is HEAD source; AFTER is current saved source; FIXED CALLER is current source held fixed. The reading snapshot is synthetic, not the original project inventory.\n\n");
      metadata({...plan.comparison, synthetic_snapshot_sha256: plan.version});
      append("### Expanded model question\n\n"); fenced(plan.model_question);
    }
    if (plan.continuation) { append("## Source-only continuation\n\nThe prior question is context for this note only; previous prose was not supplied to the model.\n\n"); metadata(plan.continuation); }
    append("## Answer\n\nOriginal claim text is retained without rewriting. Fences and a missing final newline are formatting separators, not part of the original text. References identify source locations, not sentence-level truth.\n\n");
    for (const claim of plan.claims) {
      append(claim.type === "source_quote" ? "### Host-extracted source quote\n\n" : "### Model interpretation\n\n");
      fenced(claim.text); metadata({citations: claim.citations});
    }
    append("## Supplied source ranges\n\nThese are the original supplied windows, not the current manual draft or a claim of complete dependencies.\n\n");
    if (plan.scope) metadata(plan.scope);
    metadata(plan.ranges.map(span => ({path: span.file.path, start_line: span.start, end_line: span.end, role: span.role})));
    append("## Cached display-source excerpts\n\nThe text below comes from the originally admitted display cache. Line endings are normalized; nonprintable characters may be escaped. It is not reconstructable raw source, and current disk bytes were not reverified. Recorded file hashes identify original cited whole-file bytes; artifact hashes identify retained model-read artifacts, which may cover larger ranges. Neither hashes these display excerpts. Uncited files have no available per-file hash here. Overlaps are shown once without adding lines.\n\n");
    for (const file of plan.files) {
      const spans = plan.ranges.filter(span => span.file.path === file.path).map(span => ({start: span.start, end: span.end})).sort((a, b) => a.start - b.start || a.end - b.end), merged = [];
      for (const span of spans) {
        const last = merged.at(-1);
        if (last && span.start <= last.end) last.end = Math.max(last.end, span.end); else merged.push({...span});
      }
      for (const span of merged) {
        metadata({path: file.path, start_line: span.start, end_line: span.end, recorded_file_sha256: file.file_sha256 ?? "unavailable: selected file was not cited", recorded_file_size_bytes: file.file_size_bytes});
        fenced(byPath.get(file.path).slice(span.start - 1, span.end).join("\n"));
      }
    }
    append("## Coverage and limits\n\nObserved counts mean complete retained reads, not demonstrated comprehension. Cited ranges are a subset of observed ranges; unread and excluded content is not whole-project verification. Excluded paths and private run/log locations are not exported.\n\n");
    metadata(plan.coverage); return parts.join("");
  }
  globalThis.Forge8ReadingNote = Object.freeze({prepare, render});
})();
