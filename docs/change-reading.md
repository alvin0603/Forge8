# Understand a change at one caller

Compare retained HEAD source with saved changes while keeping one current caller
fixed. This narrows a reading question; it does not find every affected caller or
verify the model's explanation. See [measured limits](VALIDATION.md).

The interface is currently Traditional Chinese; the action descriptions here are
English explanations, not literal button labels.

## Open the existing reading desk

Use your existing native installation and provisioned reading-model assets.
PowerShell and WSL run the same app with their respective native Python/Git/runtime.

```powershell
$Forge8 = ".\.venv\Scripts\forge8.exe"
& $Forge8 read "C:\work\project"
```

```bash
forge8_cli=./.venv-wsl/bin/forge8
"$forge8_cli" read /mnt/c/work/project
```

Adjust the paths to your existing installation and project. Open the private
localhost URL printed by the command, then switch to change-reading mode.
Building the source comparison and browsing it do not load the model.

To inspect one concrete input without a model, add `--allow-experiments` when
opening the desk. A current-version synchronous top-level Python function offers
a paired-trial action: the same raw JSON is sent to two separate full-module
WASI guests, HEAD then current. This does not require the three AI question roles
below and does not execute their fixed caller. [Paired execution consent, runtime
setup and limits](isolated-experiments.md#compare-head-and-current-with-the-same-input).

The comparison is local **HEAD versus currently saved filesystem bytes**.
It is not a staged/index diff and cannot see unsaved editor buffers. “Added” means
absent from the retained HEAD sources; it does not mean Git-untracked. “Absent”
means absent from the retained current snapshot, not proof of intentional deletion.

## Select the question's three roles

1. Inspect a modified file's HEAD and current versions. A Python definition-pair
   button or a two-sided line-range button can replace all existing selections.
   These shortcuts are navigation, not proof of matching runtime behavior.
2. Keep exactly three explicit roles: **HEAD source**, **corresponding current source**,
   and **fixed current caller**. The first two must use the same original file path;
   the third must come from `after/`, the currently saved version.
3. Include the needed constants, defaults and configuration in the appropriate
   before/after ranges. Select a wider span, choose its role in the selection
   dropdown, then add/replace that role. This preserves the other
   roles. A definition shortcut does not automatically find globals or helpers.
   For Python, inspecting name sources on either selection distinguishes
   parameters, locals, outer captures and module/class lookups, and lists possible
   same-file declarations. Inspect them, then explicitly expand just that role if
   the complete range fits. These are not runtime values or complete dependencies;
   inspect each version separately. No model runs during this preparation.
   Import declarations also offer import-source navigation. Every hop stays on
   that selection's HEAD/current side; missing historical files never fall back
   to current files. Source jumps preserve all three roles and the draft question,
   and do not automatically expand the compared source or invoke a model.
4. Inspect the selected current caller yourself. It is held fixed against both
   callee versions. Even if its file changed, this does **not** claim that the
   historical caller was identical or that a same-named call binds to this callee.
5. Ask for the changed condition, a concrete distinguishing input, its consequence
   for this fixed caller, and anything the selected source cannot establish.

For example, when comparing `cache.get(key) or DEFAULT` with
`cache.get(key, DEFAULT)`, include `DEFAULT` as well as the relevant caller.
Ask about missing, empty and populated entries; do not assume the model saw a
constant merely because its file appears in the browser.

The current-code and caller ranges may overlap; exact duplicate selections are
refused. Source evidence
can be deduplicated, but the request retains explicit role names and coordinates;
it does not assume that “third evidence block” necessarily means the caller.

References distinguish `before/` HEAD source from `after/` saved source and retain
their original line numbers. Click them to check the exact version and code.
Valid references establish retained locations, not the truth of an explanation.
The reader does not run the source or its tests, prove coverage, or compute a
complete impact graph. Missing necessary context should remain unknown; the model
can still make unsupported assumptions, so inspect its explanation.

## Bounds and unsupported comparisons

- The complete retained before + after sources **and provenance file together**
  must fit 1,000 files, 16 MiB total, and 256 KiB per file. A large pair is refused,
  not silently reduced to whichever files fit. Listed exclusions are outside scope.
- Exactly three role ranges: at most 80 lines and 4,000 citable characters each;
  shared limits remain 240 union lines, 9,000 characters of citable JSON and
  12,000 characters of user context. Over-budget selections stop before inference.
- Your question is limited to 1,300 characters. Role instructions and source paths
  must also fit the composed 2,000-character question; long paths may require a
  shorter question. Sources are not silently trimmed to make a request fit.
- The change catalogue is bounded to 300 combined hunks/definition units and
  256 KiB of JSON. Coarse spans may include unchanged middle lines; unavailable
  details are labelled. An unrepresentable inventory fails instead of showing
  “no changes.” Use source views for limited details in an admitted comparison;
  return to ordinary reading if the complete comparison cannot be admitted.
- Python units are static lexical definitions, not a call graph. Duplicate names,
  stubs and unavailable parsing can disable pairing. Other admitted UTF-8 languages
  support manual before/after/caller ranges; Python parsing is not required.
- Added-only, absent-only and line-ending-only changes can be inspected, but do
  not form this workflow's modified-file pair. Automatic project/locate actions,
  traceback mapping and observation import are unavailable in comparison mode.

Use a complete local checkout with native Git 2.43 or newer. Linked worktrees,
`.git` indirection, partial clones/promisor stores and indirect object/config
layouts are unsupported. Missing HEAD objects are not fetched. No checkout,
staging, filters, textconv or project hooks are used by source capture.

## State placement and returning to ordinary reading

Private state must be outside the asset directory and outside existing `runs`
directories. Comparison capsules live in `comparison-sources/`, beside `runs/`.
WSL state must use Linux ext4, not `/mnt/c` or `/mnt/d`; large assets may stay on D.
The legacy default `assets/.forge8` is refused early for comparison mode.

If changing a saved deployment, supply **both** paths and use `--replace`, then
reopen the desk. These examples do not move assets or alter global shell settings:

```powershell
& $Forge8 configure --assets "D:\Forge8\assets" --state "D:\Forge8\state\windows" --replace
```

```bash
"$forge8_cli" configure --assets /mnt/d/Forge8/assets --state "$HOME/.local/state/forge8" --replace
```

Bare `configure` shows the saved record. Do not reconfigure an already-correct
setup. Explicit `FORGE8_HOME` / `FORGE8_STATE_HOME` override saved paths.
Return to ordinary reading whenever a comparison is unsupported or unnecessary.
Saving files or moving HEAD requires a refresh; source guards reject stale
comparison answers before model startup and again before final acceptance.
A successful refresh clears session answer history; a failed refresh preserves it.
No automatic retry turns a changed source version into the same question result.
