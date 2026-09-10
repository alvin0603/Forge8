"""Read-only deployment diagnosis using the production preparation validators."""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import re
import sys

from . import cli
from .server import prepare_server


_NOTICE = ("Diagnostics only: full asset hashes do not prove GPU, port, state-write, "
           "snapshot-publication or inference readiness. No model, runtime or project "
           "process is launched. Windows Python OS identification may use its existing OS-version probe.")
_UNCHECKED = ["gpu", "port", "actual_state_write", "snapshot_publication", "model_start", "inference"]


def run_doctor(args) -> int:
    """Report independent configuration/layout checks and one full preparation."""
    reader = args.reader
    overrides = {name: os.environ.get(name) for name in ("FORGE8_HOME", "FORGE8_STATE_HOME")}
    saved_needed = any(value is None for value in overrides.values())
    configuration = {"path": None, "saved": None, "saved_used": saved_needed,
        "environment_overrides": overrides, "effective_assets": None, "effective_state": None,
        "requested_assets": overrides["FORGE8_HOME"], "requested_state": overrides["FORGE8_STATE_HOME"],
        "assets_origin": "FORGE8_HOME" if overrides["FORGE8_HOME"] is not None else "unresolved",
        "state_origin": "FORGE8_STATE_HOME" if overrides["FORGE8_STATE_HOME"] is not None else "unresolved"}
    checks = {name: {"status": "not_checked", "details": [], "next_steps": []}
              for name in ("configuration", "assets", "state", "resident_layout", "reader_preparation")}
    report = {"schema_version": 1, "kind": "forge8.doctor", "status": "failed", "ok": False,
        "python": {"executable": sys.executable, "version": sys.version.split()[0], "system": None},
        "reader": reader, "configuration": configuration, "checks": checks,
        "reader_paths": None, "preparation": None, "not_checked": list(_UNCHECKED), "notice": _NOTICE}
    active = "configuration"

    def check(name, status, details, next_steps=()):
        checks[name] = {"status": status, "details": list(details), "next_steps": list(next_steps)}

    def failed(name, exc, action):
        check(name, "failed", [f"{type(exc).__name__}: {exc}"], [action])

    try:
        system = platform.system()
        report["python"]["system"] = system
        saved, settings_known = None, not saved_needed
        try:
            configuration["path"] = str(cli._deployment_config_path())
            if saved_needed:
                saved = cli._load_deployment()
                configuration["saved"] = saved
                settings_known = True
                check("configuration", "passed", ["Saved settings read; environment overrides take precedence per field."])
            else:
                check("configuration", "not_checked", ["Saved record is unused: both fields have explicit environment overrides."])
        except (OSError, ValueError, RuntimeError) as exc:
            if saved_needed:
                failed("configuration", exc, "Inspect the native deployment.json shown here; use forge8 configure --assets ABS --state ABS --replace only to intentionally repair this app's saved record.")
            else:
                check("configuration", "not_checked", ["Saved record and its location are unused because both fields are explicitly overridden."])
        if settings_known:
            for field, variable in (("assets", "FORGE8_HOME"), ("state", "FORGE8_STATE_HOME")):
                if overrides[variable] is None:
                    configuration[field + "_origin"] = "saved record" if saved else "checkout defaults"
                    configuration["requested_" + field] = saved[field] if saved else None

        active = "assets"
        assets = None
        try:
            if reader not in {"gemma4", "qwen35", "gemma12b"}:
                raise ValueError("reader must be gemma4, qwen35 or gemma12b")
            assets = cli._resolve_fix_asset_root(reader)
            configuration["effective_assets"] = str(assets)
            paths = cli._fix_asset_anchors(Path(), reader)[:3]
            report["reader_paths"] = dict(zip(("runtime_manifest", "model_manifest", "profile"),
                                              (str(assets / path) for path in paths)))
            check("assets", "passed", ["Selected-reader configuration anchors located; full integrity is checked separately below."])
        except (OSError, ValueError, RuntimeError) as exc:
            failed("assets", exc, "Correct the effective FORGE8_HOME or saved assets path for this native Python and selected reader; see docs/PLATFORMS.md. No alternate deployment was substituted.")

        active = "state"
        state = None
        state_value = configuration["requested_state"]
        if state_value is not None or settings_known and assets is not None:
            try:
                # An explicit state path is independent of assets; the helper
                # does not consult its first argument when configured is supplied.
                state, _, missing = cli._state_directory_plan(assets or Path(), state_value)
                configuration["effective_state"] = str(state)
                check("state", "passed", [f"Existing state ancestors are regular directories; {len(missing)} directories would need creation. No write or publication was attempted."])
                if system == "Linux" and re.match(r"^/mnt/[A-Za-z](?:/|$)", state.as_posix()):
                    check("state", "warning", ["State uses a conventional Windows-mounted WSL path. This spelling is not filesystem identification or a capability test."],
                          ["In WSL, choose a native Linux filesystem state directory, such as $HOME/.local/state/forge8; keep large model assets on D. Verify actual snapshot publication when opening a project."])
            except (OSError, ValueError, RuntimeError) as exc:
                failed("state", exc, "Set FORGE8_STATE_HOME or the saved state field to an absolute, regular private directory; do not use a source checkout or redirected path.")
        else:
            check("state", "not_checked", ["Effective state cannot be derived until its saved configuration or default assets root is available."])

        active = "resident_layout"
        if reader == "gemma4":
            check("resident_layout", "not_applicable", ["Gemma4 uses the one-shot workflow; resident layout is not a general browsing or one-shot requirement."])
        elif assets is not None and state is not None:
            try:
                resolved_assets, resolved_state = assets.resolve(strict=True), state.resolve(strict=False)
                if resolved_assets == resolved_state or resolved_assets in resolved_state.parents:
                    check("resident_layout", "failed", ["State is equal to or inside assets; the selected reader's default resident mode will refuse acquisition. Static browsing and one-shot use are separate."],
                          ["Choose a state directory outside the assets tree (and outside the project being read), or explicitly use read --idle-timeout 0. Forge8 did not change either path."])
                elif resolved_state in resolved_assets.parents:
                    check("resident_layout", "warning", ["The configured state root contains assets, but the actual resident session is below state/runs/read-<id>. The roots alone do not establish a resident-session conflict."],
                          ["Check the actual project and session paths when opening the desk. Separate private state from assets for an unambiguous layout; no session directory was created or checked here."])
                else:
                    check("resident_layout", "passed", ["Assets/state paths do not overlap. No project path was supplied, so project/state overlap is not checked."])
            except (OSError, ValueError, RuntimeError) as exc:
                failed("resident_layout", exc, "Recheck the effective asset/state paths; residency requires separate trees.")

        active = "reader_preparation"
        if assets is not None:
            try:
                if not args.as_json:
                    print("Checking complete runtime/model hashes; no model starts; Ctrl+C cancels.",
                          file=sys.stderr, flush=True)
                prepared = prepare_server(assets, runtime_manifest_path=paths[0],
                    model_manifest_path=paths[1], profile_path=paths[2])
                report["preparation"] = prepared.as_dict()
                if prepared.ok:
                    check("reader_preparation", "passed", ["Production profile/native checks and complete pinned runtime/model hashes passed. Nothing was launched."])
                else:
                    details = list(prepared.errors)
                    if prepared.status == "integrity_failed":
                        details = cli._explain_integrity_error(prepared, reader, assets, paths[0], paths[1]).splitlines()
                    check("reader_preparation", "failed", details or [prepared.status],
                          ["Use this reader's manifest/profile paths shown above. Restore the pinned missing or mismatched assets; do not replace expected hashes to make verification pass. See docs/PLATFORMS.md."])
            except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
                failed("reader_preparation", exc, "Inspect this selected reader's manifests and profile; no runtime was started and no alternative reader was tried.")
        else:
            check("reader_preparation", "not_checked", ["Full verification requires a resolved selected-reader asset root."])
        report["ok"] = (checks["reader_preparation"]["status"] == "passed"
                        and not any(item["status"] == "failed" for item in checks.values()))
        report["status"] = "passed" if report["ok"] else "failed"
        code = 0 if report["ok"] else 2
    except KeyboardInterrupt:
        check(active, "interrupted", ["Diagnostic cancelled; no completed readiness result is available."])
        report["status"], report["ok"], code = "interrupted", False, 130

    encoded = json.dumps(report, ensure_ascii=True, allow_nan=False, indent=2)
    if len(encoded.encode("utf-8")) > 262144:
        report = {"schema_version": 1, "kind": "forge8.doctor", "status": "failed", "ok": False,
            "error": "Diagnostic report exceeds 256 KiB; no partial success report was emitted. Use the explicit native runtime/model verify commands for separate reports.",
            "not_checked": list(_UNCHECKED), "notice": _NOTICE}
        encoded, code = json.dumps(report, indent=2), 2
    if args.as_json:
        print(encoded)
    elif "checks" not in report:
        print("Forge8 doctor FAILED: " + report["error"])
    else:
        print(f"Forge8 doctor {report['status'].upper()} — diagnostics, not inference readiness")
        for text in (f"Python executable: {report['python']['executable']}",
                     f"Python version: {report['python']['version']}",
                     f"Native platform: {report['python']['system']}", f"Reader: {reader}",
                     f"Saved configuration: {configuration['path']}"):
            print(cli._inert_text(text))
        for field in ("assets", "state"):
            value = configuration["effective_" + field] or configuration["requested_" + field]
            print(cli._inert_text(f"{field}: {value} ({configuration[field + '_origin']})"))
        for name, value in (report["reader_paths"] or {}).items():
            print(cli._inert_text(f"{name}: {value}"))
        for name, item in checks.items():
            print(f"{name}: {item['status']}")
            for line in item["details"][:24] + item["next_steps"][:3]:
                safe = cli._inert_text(line)
                print("  " + (safe if len(safe) <= 2400 else "Detail exceeds display limit; use --json for the complete report."))
            if len(item["details"]) > 24:
                print("  More details retained in --json.")
        print(_NOTICE)
    return code
