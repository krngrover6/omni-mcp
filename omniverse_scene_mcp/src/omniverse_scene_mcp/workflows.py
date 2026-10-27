"""Ready-made MCP prompts: short, step-by-step recipes that name the exact router tools to call, in order.

Why prompts: a client lists prompts (slash commands in Claude Code, "/" or the prompt picker in Codex and Claude
Desktop) without loading anything into the model's context until one is picked. A recipe lets a smaller or cheaper
model drive the router well: it says which tool first, what to preview, and when to stop and ask.

Everything here is plain data plus pure render functions; ``register_prompts(app)`` turns each recipe into a FastMCP
prompt (``app.prompt``) and tolerates an app without prompt support. Prompt arguments arrive from MCP clients as
strings, so every parameter is a string with a default.

Safety rules repeated in every recipe (SAFETY_FOOTER): preview first (dry_run=True), show the user the plan, apply
only after they agree, never save or overwrite files without asking.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable

SAFETY_FOOTER = (
    "Safety rules: read before you change anything. Call every editing tool with dry_run=True first (some tools "
    "default to dry_run=False, so always pass it), show the user the preview, and repeat the call with dry_run=False "
    "only after they say yes. Never call scene_save, project_create(dry_run=False), scene_restore_checkpoint or any "
    "write=True report without asking. If a tool says the bridge or scene is not reachable, stop and tell the user "
    "(router_status / router_health explain why). If a tool you need is not listed, use router_dispatch with a plain "
    "English request instead: it reaches every router feature."
)

# name -> {title, description, params: [(name, default, description)], steps: [str]}.
# Steps are str.format templates over the params.
PROMPTS: dict[str, dict[str, Any]] = {
    "predict_result": {
        "title": "Predict the result",
        "description": "Train LIVRPS: guess which opinion wins on small generated scenes, then on your own scene.",
        "params": [
            ("difficulty", "easy", "Start level: easy, medium or hard."),
            ("rounds", "3", "How many puzzles before moving to your own scene."),
        ],
        "steps": [
            "Call learning_composition_puzzle(difficulty='{difficulty}'). Show the layers and the question, and ask "
            "the user to guess before showing the options' answer. Do not reveal it.",
            "Grade with learning_composition_puzzle_answer(puzzle_id, answer). Explain the winning rule in one or two "
            "sentences (layer order or a LIVRPS letter).",
            "Repeat for {rounds} puzzles. After two right answers in a row, move up a difficulty level.",
            "Then use the user's own scene: pick an attribute (ask, or use scene_explain to find one), call "
            "scene_why_value, and ask its first 'predict' question.",
            "Check the guess with the matching scene_what_if call (in-memory only; nothing changes) and explain.",
            "End with a one-line score and the rule they found hardest.",
        ],
    },
    "fix_connection": {
        "title": "Fix my connection",
        "description": "Find out why the AI can't reach Isaac Sim or Composer, and fix it step by step.",
        "params": [],
        "steps": [
            "Call router_doctor. (If no router tools respond at all, ask the user to run: python -m omniverse_scene_mcp "
            "doctor, from C:\\Users\\krngr\\Omniverse with PYTHONPATH=omniverse_scene_mcp\\src, and paste the result.)",
            "Read the headline, then each part in plain words: router, Isaac Sim bridge, Composer bridge.",
            "Give the next_steps one at a time. The user does each step themselves (Script Editor reloads, starting "
            "apps, restarting the router); never start, stop or reload anything yourself without asking.",
            "After each step, call router_doctor again and confirm the part turned healthy before moving on.",
            "Finish when the verdict is 'ready', then try one read-only call such as scene_summary.",
        ],
    },
    "why_value": {
        "title": "Why does this value appear?",
        "description": "Explain where an attribute's value comes from, then predict and check an override.",
        "params": [
            ("prim_path", "/World/Box", "The prim, e.g. /World/Box."),
            ("attribute", "size", "The attribute name, e.g. size or primvars:displayColor, or 'material'."),
        ],
        "steps": [
            "Call scene_why_value(prim_path='{prim_path}', attribute='{attribute}'). Read the headline aloud in plain "
            "words: the value, and where it is written.",
            "Explain the 'beats' list one opinion at a time: what it says and why it loses (layer order or LIVRPS).",
            "If 'not_authored' or 'computed' is present, explain it separately: a fallback is the schema's built-in "
            "default; a computed value (inherited visibility, world position, a material bound on an ancestor) is "
            "worked out by USD and is not an opinion in any layer.",
            "Ask the user the first 'predict' question and wait for their guess (yes or no, and why).",
            "Run the matching check_with call (scene_what_if). It runs on in-memory copies only; say so. Compare the "
            "result with their guess and explain the rule in one or two sentences.",
            "Offer the second prediction, or another attribute. Never apply a change: to really edit, preview with "
            "scene_set_attribute(..., dry_run=True) and ask first.",
        ],
    },
    "study_session": {
        "title": "Study a teacher's class session",
        "description": "Turn a class recording's subtitles into short lessons, then study them one at a time.",
        "params": [
            ("session", "3", "The class session number, e.g. 3."),
            ("minutes", "15", "About how long each lesson should be (5-60)."),
        ],
        "steps": [
            "Call class_transcripts and check that session {session} has a transcript; if not, show what was found.",
            "Call class_session_plan(session={session}, minutes={minutes}) and show the lessons as a short list "
            "(number, time range, title). Mention how many accuracy flags there are, without details yet.",
            "Start with lesson 1: call class_lesson(session={session}, number=1). Tell the user which part of the "
            "recording to watch, explain the key terms in plain words, then the main points.",
            "Walk through the do_in_app steps one by one; the user does them in Isaac Sim or Composer themselves.",
            "Explain each accuracy flag gently: what was said, the correct idea, and that subtitles can mishear.",
            "Ask the check questions one at a time and wait for answers; then the exam questions, graded with "
            "learning_exam_answer(question_id, chosen=[...]). Explain every wrong answer simply.",
            "Ask whether to continue with the next lesson (class_lesson with number+1) or stop for today.",
        ],
    },
    "start_project": {
        "title": "Start a new OpenUSD project",
        "description": "Plan and create a new, well-organized project folder from a template (preview first).",
        "params": [
            ("name", "my_project", "Folder name for the new project (letters, digits, underscores)."),
            ("template", "blank", "Template name; call project_templates to see them all."),
            ("goal", "", "One sentence: what the project should show or teach."),
        ],
        "steps": [
            "Call project_templates and check that '{template}' exists; if not, show the list and ask which to use.",
            "Call project_list to make sure no project called '{name}' already exists.",
            "Call project_plan(name='{name}', template='{template}', goal='{goal}') and show the folder tree and "
            "layers it would create.",
            "Ask the user to confirm. Only then call project_create(name='{name}', template='{template}', "
            "goal='{goal}', dry_run=False).",
            "Tell the user the scene entry file and that PROJECT_CONTEXT.md records the goal and next steps.",
        ],
    },
    "daily_study": {
        "title": "Daily study session",
        "description": "Run today's NCP-OUSD study plan in the time available.",
        "params": [("minutes", "60", "How many minutes you have today.")],
        "steps": [
            "Call learning_today(minutes={minutes}) and show the plan in a short list.",
            "Follow the plan one block at a time. For a drill: learning_drill_start(...), ask the questions one by "
            "one, then learning_drill_answer(drill_id, answers). For a quiz: learning_exam_quiz(...) then "
            "learning_exam_answer(question_id, chosen=[...]).",
            "After each wrong answer explain the concept in two or three plain sentences (usd_knowledge or "
            "learning_glossary when a definition helps); do not move on until the user is ready.",
            "At the end call learning_exam_progress and say what improved and what to do tomorrow.",
        ],
    },
    "fix_my_error": {
        "title": "Explain and fix an error",
        "description": "Turn a pasted error or traceback into a plain-English cause and a safe fix.",
        "params": [("paste", "", "The error message or traceback, pasted as-is.")],
        "steps": [
            "Call explain_error(text=<the pasted error below>) and show the cause and fix in plain words.",
            "If the paste is empty or the error came from Isaac Sim / USD Composer, call kit_explain_recent_errors() "
            "to read the latest Kit log errors instead.",
            "If the fix touches the scene, inspect first (scene_inspect on the prim path in the error) and preview "
            "any edit with dry_run=True before applying it.",
            "Show corrected Python only if the user asks (python_show can print the snippet for a scene action).",
            "Pasted error:\n{paste}",
        ],
    },
    "check_homework": {
        "title": "Check my homework",
        "description": "Check a class assignment against the saved scene and explain what is missing.",
        "params": [("assignment_id", "", "Assignment id from assignment_list (leave empty to choose).")],
        "steps": [
            "If no assignment id was given ('{assignment_id}' is empty), call assignment_list and ask which one.",
            "Remind the user that the check reads the SAVED scene file; ask them to save in Composer/Isaac Sim first.",
            "Call assignment_check(assignment_id='{assignment_id}') and list each check as pass or fail.",
            "For each failed check, explain why it matters and the smallest fix; do not edit the scene unless the "
            "user asks (then preview with dry_run=True first).",
        ],
    },
    "build_configurator_step": {
        "title": "Build one configurator step",
        "description": "Add one product-configurator feature (variant, material or look) safely, one step at a time.",
        "params": [
            ("prim_path", "/World/Product", "The product prim to configure."),
            ("step", "add a colour variant", "What this step should add."),
        ],
        "steps": [
            "Call scene_inspect(path='{prim_path}') and scene_variants(path='{prim_path}') to see what exists.",
            "Call scene_checkpoint(note='before: {step}', dry_run=True), then with dry_run=False after the user "
            "agrees, so the step can be undone from disk.",
            "Plan the step '{step}' using the smallest tools: scene_create_material / scene_bind_material for looks, "
            "scene_variant_set for switching variants (always pass dry_run=True first).",
            "Show the previews and apply only after the user says yes; then call scene_diff to show what changed.",
            "Explain the USD concept used (variant sets, material binding strength) in two sentences.",
        ],
    },
    "digital_twin_import": {
        "title": "Import an asset for a digital twin",
        "description": "Bring a CAD/FBX/OBJ asset into a project with correct units and up axis, then check it.",
        "params": [
            ("source_file", "", "Path to the file to import (inside the workspace)."),
            ("prim_path", "/World/Asset", "Where the asset should appear in the scene."),
        ],
        "steps": [
            "Call router_help(topic='import') to find the import tool this router version offers and preview the "
            "import of '{source_file}' with it (dry_run=True); if none is listed, use router_dispatch(request='import "
            "{source_file}', dry_run=True).",
            "Check the converted asset with usd_asset_health(scene_file=<converted file>) and explain any unit or "
            "up-axis warning.",
            "Preview placing it: scene_add_asset(prim_path='{prim_path}', asset_path=<converted or wrapped file>, "
            "dry_run=True). Apply with dry_run=False only after the user agrees.",
            "Call scene_bounds(prim_path='{prim_path}') to confirm the size looks right (a chair is about 1 m, not 100 m).",
        ],
    },
    "robot_pick_place": {
        "title": "Plan a robot pick-and-place",
        "description": "Inspect a robot and plan a simple pick-and-place cell in Isaac Sim, step by step.",
        "params": [
            ("robot_path", "/World/Robot", "The robot's root prim."),
            ("object_path", "/World/Box", "The object to pick."),
        ],
        "steps": [
            "Call router_set_scene_target(target='isaac') if the router is pointed at Composer, then router_status.",
            "Call robot_inspect(root_path='{robot_path}') and summarise joints, links and the end effector.",
            "Call scene_physics_report() to check there is a physics scene and that '{object_path}' is a rigid body "
            "with a collider; preview missing pieces with scene_add_physics_scene(dry_run=True) and "
            "scene_make_rigid_body(prim_path='{object_path}', dry_run=True).",
            "Apply the previews only after the user agrees, then sim_play, sim_step(frames=60) and sim_status to "
            "watch the object settle; sim_stop when done.",
            "If a pick-and-place or joint tool is listed by router_help(topic='robots'), describe its plan before "
            "running it; otherwise explain the next manual step in Isaac Sim.",
        ],
    },
    "review_my_scene": {
        "title": "Review my scene",
        "description": "A read-only review: structure, composition, validation and asset health, with fixes ranked.",
        "params": [("focus_path", "", "Optional prim path to focus on (empty = whole scene).")],
        "steps": [
            "Call scene_summary, then scene_explain(focus_path='{focus_path}' or omit when empty) for a plain-English "
            "tour.",
            "Call usd_validate() and usd_asset_health() and merge their findings; drop duplicates.",
            "Call scene_layers() to explain which layer holds which opinions.",
            "Give the user at most five fixes, most important first, each with the tool that would preview it. Do not "
            "apply anything in this review.",
        ],
    },
    "make_portfolio_report": {
        "title": "Make a portfolio report",
        "description": "Turn a project's context, scene tour and health check into a Markdown/HTML report.",
        "params": [
            ("project_dir", "", "The project folder (empty = current scene's project)."),
            ("title", "", "Optional report title."),
        ],
        "steps": [
            "Call project_report(project_dir='{project_dir}', title='{title}', write=False) to preview the report.",
            "Show the outline and ask the user what to add (screenshots: scene_capture_viewport; changes: "
            "scene_journal_summary).",
            "Only after the user agrees, call project_report(..., write=True); pass overwrite=True only if they "
            "explicitly say to replace an existing report.",
            "Tell the user where the Markdown and HTML files were written.",
        ],
    },
}


def prompt_names() -> list[str]:
    return list(PROMPTS)


def prompt_params(name: str) -> dict[str, str]:
    """Parameter name -> default for one prompt."""
    return {param: default for param, default, _ in PROMPTS[name]["params"]}


def render_prompt(name: str, **params: Any) -> str:
    """The recipe as plain text: title, numbered steps with the parameters filled in, and the safety rules.

    Unknown parameters raise TypeError; missing ones use their defaults; None counts as missing.
    """
    if name not in PROMPTS:
        raise KeyError(f"Unknown prompt: {name}. Known prompts: {', '.join(PROMPTS)}")
    spec = PROMPTS[name]
    values = prompt_params(name)
    extra = set(params) - set(values)
    if extra:
        raise TypeError(f"Prompt {name} has no parameter(s): {', '.join(sorted(extra))}")
    for key, value in params.items():
        if value is not None:
            values[key] = str(value)
    lines = [f"{spec['title']}. {spec['description']}", "Do these steps in order, one tool call at a time:"]
    for index, step in enumerate(spec["steps"], start=1):
        lines.append(f"{index}. {step.format(**values)}")
    lines.append(SAFETY_FOOTER)
    return "\n".join(lines)


def _make_prompt_function(name: str) -> Callable[..., str]:
    """A function whose signature lists the prompt's parameters (all str with defaults), for FastMCP."""
    spec = PROMPTS[name]
    try:  # Descriptions show up in the client's prompt form when pydantic is available (it is, with mcp).
        from typing import Annotated

        from pydantic import Field

        def annotation(description: str) -> Any:
            return Annotated[str, Field(description=description)]

    except ImportError:  # pragma: no cover - mcp always brings pydantic

        def annotation(description: str) -> Any:
            return str

    parameters = [
        inspect.Parameter(param, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=annotation(about))
        for param, default, about in spec["params"]
    ]

    def prompt_fn(**kwargs: Any) -> str:
        return render_prompt(name, **kwargs)

    prompt_fn.__name__ = name
    prompt_fn.__qualname__ = name
    prompt_fn.__doc__ = spec["description"]
    prompt_fn.__signature__ = inspect.Signature(parameters, return_annotation=str)  # type: ignore[attr-defined]
    prompt_fn.__annotations__ = {param.name: param.annotation for param in parameters} | {"return": str}
    return prompt_fn


def register_prompts(app: Any, names: list[str] | None = None) -> list[str]:
    """Register the recipes as MCP prompts on a FastMCP app. Returns the names registered.

    Tolerant: an app without ``prompt`` registers nothing; a ``prompt`` decorator without ``title`` support is
    retried without it; a recipe that fails to register is skipped (the router must still start).
    """
    register = getattr(app, "prompt", None)
    if not callable(register):
        return []
    done = []
    for name in names or list(PROMPTS):
        spec = PROMPTS[name]
        fn = _make_prompt_function(name)
        try:
            try:
                decorator = register(name=name, title=spec["title"], description=spec["description"])
            except TypeError:
                decorator = register(name=name, description=spec["description"])
            decorator(fn)
        except Exception:  # noqa: BLE001 - a bad prompt must never stop the router from starting
            continue
        done.append(name)
    return done
