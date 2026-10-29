"""Check an Isaac Sim Python script before you run it (class sessions 7-8): read-only, nothing is executed here.

Beginner mistakes that are easy to spot from the code: a syntax error, old omni.isaac.* imports that Isaac Sim 6.1
renamed, SimulationApp in the Script Editor (or missing from a standalone script), loops and sleeps that freeze the
app, opening a file instead of using the scene on screen, unchecked prim lookups, and code that deletes or saves
files. Each finding gives the line, why it matters and the fix. python_trial_run can then run the code on a practice
scene to show what it would create.
"""

from __future__ import annotations

import ast
from typing import Any

MAX_CHARS = 50_000
# Old Isaac Sim (2023 / 4.0) extension names -> their 6.1 homes. Sources: isaacsim.core.deprecation_manager's rename
# table and the sensors catalog (sensor_catalog old_to_new).
RENAMED = {
    "omni.isaac.kit": "isaacsim (from isaacsim import SimulationApp)",
    "omni.isaac.core": "isaacsim.core.api (or isaacsim.core.experimental in 6.x)",
    "omni.isaac.core.utils": "isaacsim.core.utils",
    "omni.isaac.sensor": "isaacsim.sensors.experimental.rtx (cameras, lidar) / isaacsim.sensors.experimental.physics",
    "omni.isaac.range_sensor": "isaacsim.sensors.experimental.physics (raycast) or RTX lidar",
    "omni.isaac.wheeled_robots": "isaacsim.robot.wheeled_robots",
    "omni.isaac.franka": "isaacsim.robot.manipulators.examples.franka",
    "omni.isaac.urdf": "isaacsim.asset.importer.urdf",
    "omni.isaac.dynamic_control": "isaacsim.core.experimental.prims (Articulation) - dynamic_control was removed",
}
DESTRUCTIVE = {"os.remove", "os.unlink", "os.rmdir", "shutil.rmtree", "shutil.move", "os.rename"}


def _finding(severity: str, line: int | None, code: str, message: str, fix: str) -> dict[str, Any]:
    return {"severity": severity, "line": line, "code": code, "message": message, "fix": fix}


def _dotted(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def check_script(code: str, where: str = "script_editor") -> dict[str, Any]:
    """Read-only: find beginner mistakes in an Isaac Sim script before running it. where is 'script_editor' (code
    pasted into Window > Script Editor, inside a running Isaac Sim) or 'standalone' (a .py run with python.bat)."""
    if not isinstance(code, str) or not code.strip():
        return {"ok": False, "error": "Paste the script as code."}
    if len(code) > MAX_CHARS:
        return {"ok": False, "error": f"The script is longer than {MAX_CHARS} characters."}
    if where not in ("script_editor", "standalone"):
        return {"ok": False, "error": "where must be 'script_editor' or 'standalone'."}
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return {"ok": True, "read_only": True, "where": where, "runs": False, "findings": [_finding(
            "error", exc.lineno, "syntax", f"Python cannot read line {exc.lineno}: {exc.msg}.",
            "Look at that line and the one above it: a missing ':' or ')' or a wrong indent is the usual cause.")],
            "summary": "Fix the syntax error first; nothing else was checked."}
    findings: list[dict[str, Any]] = []
    imported: set[str] = set()
    sim_app_line = first_omni_line = None
    assigned: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for name in names:
                imported.add(name.split(".")[0])
                imported.add(name)
                old = next((key for key in sorted(RENAMED, key=len, reverse=True) if name == key or
                            name.startswith(key + ".")), None)
                if old:
                    findings.append(_finding("error", node.lineno, "renamed_import",
                                             f"{name} is an old Isaac Sim name; Isaac Sim 6.1 does not have it.",
                                             f"Use {RENAMED[old]}."))
                if (name.startswith(("omni.", "isaacsim.")) or name in ("omni", "isaacsim")) and \
                        not name.startswith("isaacsim.simulation_app") and first_omni_line is None and \
                        not (isinstance(node, ast.ImportFrom) and node.module == "isaacsim" and
                             any(alias.name == "SimulationApp" for alias in node.names)):
                    first_omni_line = node.lineno
            if isinstance(node, ast.ImportFrom) and any(alias.name == "SimulationApp" for alias in node.names):
                sim_app_line = sim_app_line or node.lineno
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for item in ast.walk(target):
                    if isinstance(item, ast.Name):
                        assigned.add(item.id)
        elif isinstance(node, (ast.FunctionDef, ast.For)):
            for item in ast.walk(node.target if isinstance(node, ast.For) else node.args):
                if isinstance(item, (ast.Name, ast.arg)):
                    assigned.add(getattr(item, "id", None) or getattr(item, "arg", ""))
        elif isinstance(node, ast.While) and isinstance(node.test, ast.Constant) and node.test.value is True:
            if not any(isinstance(item, (ast.Break, ast.Return)) for item in ast.walk(node)):
                findings.append(_finding("error" if where == "script_editor" else "warning", node.lineno, "endless_loop",
                                         "A 'while True' loop with no break never ends"
                                         + (": Isaac Sim freezes, because the Script Editor runs on its main thread."
                                            if where == "script_editor" else "."),
                                         "Use a for loop over a range, add a break, or do per-frame work in an "
                                         "Action Graph / physics callback instead."))
        elif isinstance(node, ast.Call):
            name = _dotted(node.func)
            if name in ("time.sleep", "sleep") and where == "script_editor":
                findings.append(_finding("warning", node.lineno, "sleep",
                                         "time.sleep in the Script Editor freezes the whole app while it waits.",
                                         "Wait with a Delay node in an Action Graph, or an async function with "
                                         "await omni.kit.app.get_app().next_update_async()."))
            if name.endswith("Usd.Stage.Open") and where == "script_editor":
                findings.append(_finding("warning", node.lineno, "opens_other_stage",
                                         "Usd.Stage.Open opens a SEPARATE copy of a file, not the scene on screen; "
                                         "changes will not appear in the viewport.",
                                         "Use stage = omni.usd.get_context().get_stage() for the open scene."))
            if name in DESTRUCTIVE or name.endswith((".rmtree", ".unlink")):
                findings.append(_finding("warning", node.lineno, "deletes_files",
                                         f"{name} deletes or moves files on disk; there is no undo.",
                                         "Double-check the path, or test on a copy first."))
            if name.endswith((".Save", ".Export")) and "Layer" not in name:
                findings.append(_finding("warning", node.lineno, "saves_files",
                                         f"{name}() writes a file to disk (Ctrl+Z cannot undo a save).",
                                         "Make sure it is the file you mean; keep a checkpoint first."))
            if name in ("subprocess.run", "subprocess.Popen", "subprocess.call", "os.system"):
                findings.append(_finding("warning", node.lineno, "runs_programs",
                                         f"{name} starts other programs from inside Isaac Sim.",
                                         "Only run programs you trust; prefer Isaac Sim APIs."))
            if name.endswith("GetPrimAtPath"):
                parent = code.splitlines()[node.lineno - 1] if node.lineno <= len(code.splitlines()) else ""
                if "IsValid" not in code and "if not" not in parent:
                    findings.append(_finding("info", node.lineno, "unchecked_prim",
                                             "GetPrimAtPath never fails: with a wrong path it returns an INVALID "
                                             "prim, and the error only shows up later.",
                                             "Check it: if not prim.IsValid(): print('wrong path') (class session 12)."))
    names_used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
    if "omni" in names_used and "omni" not in imported:
        findings.append(_finding("error", None, "missing_import", "The script uses omni.* but never imports it.",
                                 "Add the import, e.g. import omni.usd."))
    if "stage" in names_used and "stage" not in assigned and where == "script_editor":
        findings.append(_finding("error", None, "no_stage", "The script uses 'stage', but nothing creates it: "
                                 "Isaac's Script Editor does not define it for you.",
                                 "Start with: import omni.usd; stage = omni.usd.get_context().get_stage()"))
    if where == "script_editor" and sim_app_line:
        findings.append(_finding("error", sim_app_line, "simulation_app_in_editor",
                                 "SimulationApp starts a NEW Isaac Sim: it is only for standalone scripts, and fails "
                                 "inside the Script Editor of a running app.", "Remove it; the app is already running."))
    if where == "standalone":
        if not sim_app_line:
            findings.append(_finding("error", None, "no_simulation_app",
                                     "A standalone script must start Isaac Sim itself before using omni/isaacsim.",
                                     "First lines: from isaacsim import SimulationApp; app = SimulationApp({'headless': "
                                     "False}); then the other imports; end with app.close()."))
        elif first_omni_line and first_omni_line < sim_app_line:
            findings.append(_finding("error", first_omni_line, "import_before_app",
                                     "omni/isaacsim modules are imported before SimulationApp exists, so they fail.",
                                     "Move those imports below the SimulationApp(...) line."))
    order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda item: (order[item["severity"]], item["line"] or 0))
    errors = sum(1 for item in findings if item["severity"] == "error")
    return {
        "ok": True, "read_only": True, "where": where, "runs": errors == 0, "findings": findings,
        "summary": (f"{errors} error(s) to fix before running." if errors else
                    "No blocking problems found." + (" Read the warnings first." if findings else "")),
        "next": "python_trial_run(code) runs it on a practice scene and lists what it would create.",
        "note": "A static check: it reads the code, it does not run it, so it cannot catch every runtime error.",
    }
