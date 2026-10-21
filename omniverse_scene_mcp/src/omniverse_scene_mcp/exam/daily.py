"""'Start day N' NCP-OUSD lessons: one day of the saved study plan as explanations, docs, exercises and quizzes.

The plan comes from ``blueprint.study_plan`` with the settings saved in the progress store (default 30 days of
60 minutes, starting the first time a day is built). Answers are never included: quiz questions use the bank's
public view, puzzles are generated without their solution and 'predict the result' items keep the expected value
server-side (``check_prediction`` grades them by question id). The only writes go through the store's settings API.
"""

from __future__ import annotations

import json
import math
import re
import zlib
from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING, Any, Iterable

from . import puzzles

if TYPE_CHECKING:
    from .progress import ProgressStore

DEFAULT_DAYS = 30
DEFAULT_MINUTES = 60
MIN_MINUTES = 10
MAX_MINUTES = 480
SCHEDULE_CAP = 60  # fallback when the blueprint does not say (blueprint.MAX_SCHEDULE_DAYS)
QUIZ_PER_BLOCK = 3
DOC_HITS = 3
SNIPPET_CAP = 300
READINGS_PER_BLOCK = 2
MAX_MIXED = 15
MAX_FINAL_QUESTIONS = 8
MAX_REVISIT = 10
MAX_DUE_LISTED = 10
MAX_DUE_PUZZLES = 3
MAX_OUTLINE_DAYS = 60
MAX_OUTPUT_CHARS = 100_000
HANDS_ON_DOMAINS = frozenset({"Composition", "Content Aggregation"})
_PUZZLE_ID = re.compile(r"^liverps-(easy|medium|hard)-(\d{1,9})$")
_SIMPLE_LAYER = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}\.usda$")
_HIDDEN_KEYS = frozenset({"answer", "answers", "correct", "correct_answer", "explanations", "verify", "citations"})
# Objectives whose skill is reading opinion strength; LIVERPS puzzles practise exactly that.
_PUZZLE_KEYWORDS = frozenset({
    "opinion", "strength", "liverps", "livrps", "strength ordering", "sublayer", "reference", "payload",
    "layer offset", "time offset", "variant", "variant set", "variant selection", "inherits", "specializes",
    "property stack", "layer stack", "edit target",
})
_STANDARD_PLAN_NOTES = ("NVIDIA publishes no passing score", "Minutes follow", "Focus order", "estimated_exam_pct")

DOMAIN_INTROS = {
    "Composition": "USD assembles one composed stage from many layers and composition arcs, and when opinions "
    "disagree the strength order decides which one wins.",
    "Content Aggregation": "Large scenes stay light by reusing content: scenegraph instances share one prototype and "
    "point instancers scatter many copies from a few arrays.",
    "Customizing USD": "USD grows through plugins: schemas, kinds, asset resolvers, file formats and Hydra scene "
    "indices are all found through plugInfo.json registration.",
    "Data Exchange": "Moving data between USD and other tools means mapping concepts from one format to the other and "
    "checking what survives each conversion.",
    "Data Modeling": "Data modeling is choosing the prims, attributes, value types, primvars and metadata that make "
    "the data mean exactly what you intend.",
    "Debugging and Troubleshooting": "Troubleshooting works backwards from a symptom (a wrong value, a missing asset, "
    "a slow edit) to the layer, arc or setting that causes it.",
    "Pipeline Development": "Pipeline work turns USD into a team workflow: asset structure rules, file formats, asset "
    "paths, validation and import/export tools.",
    "Visualization": "Visualization covers how geometry gets its look: primvars, UsdPreviewSurface shading networks "
    "and material binding.",
}
PASS_TIPS = {
    "learn": "First pass: read the doc hits, do the exercise, then answer the quiz without looking anything up.",
    "revisit": "Second pass: start with the quiz to find gaps, then reread only what you missed.",
}


# ---------------------------------------------------------------- starter USDA for 'try it' exercises


def _usda(body: str, default_prim: str | None = "World", extra_meta: Iterable[str] = ()) -> str:
    meta = ([f'defaultPrim = "{default_prim}"'] if default_prim else []) + list(extra_meta)
    header = "#usda 1.0\n" + ("(\n" + "".join(f"    {line}\n" for line in meta) + ")\n" if meta else "")
    return header + "\n" + body.strip("\n") + "\n"


_STARTERS: tuple[dict[str, Any], ...] = (
    {
        "topic": "primvars and interpolation",
        "keywords": ("primvar", "interpolation", "displaycolor", "primvarsapi", "facevarying"),
        "layers": {"mesh.usda": _usda('''
def Xform "World"
{
    def Mesh "Quad"
    {
        int[] faceVertexCounts = [4]
        int[] faceVertexIndices = [0, 1, 2, 3]
        point3f[] points = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)]
        color3f[] primvars:displayColor = [(0.9, 0.2, 0.2)] (
            interpolation = "constant"
        )
    }
}''')},
        "queries": ["/World/Quad.primvars:displayColor"],
        "task": "Query the displayColor primvar, then switch its interpolation to \"vertex\" with four colors (one "
        "per point) or \"uniform\" with one color per face. Compose again and check that the array length matches "
        "what the interpolation expects.",
    },
    {
        "topic": "value types",
        "keywords": ("value type", "sdf type", "token", "float", "double", "matrix4d", "array"),
        "layers": {"types.usda": _usda('''
def Scope "Settings"
{
    token mode = "preview"
    string label = "Preview mode"
    float ratio = 0.5
    double precise = 0.123456789
    int[] ids = [1, 2, 3]
    matrix4d offset = ( (1, 0, 0, 0), (0, 1, 0, 0), (0, 0, 1, 0), (5, 0, 0, 1) )
}''', "Settings")},
        "queries": ["/Settings.mode", "/Settings.ratio", "/Settings.offset"],
        "task": "Compare the type_name reported for each query. Add a color3f attribute and a float3[] array, then "
        "explain when token beats string (a fixed set of choices) and when double beats float (precision).",
    },
    {
        "topic": "metadata",
        "keywords": ("metadata", "customdata", "assetinfo"),
        "layers": {"meta.usda": _usda('''
def Xform "World" (
    assetInfo = {
        string name = "World"
        string version = "1.0"
    }
    customData = {
        string department = "layout"
        int revision = 3
    }
)
{
    def Cube "Box" (
        customData = {
            bool approved = 0
        }
    )
    {
        double size = 2 (
            customData = {
                string units = "cm"
            }
        )
    }
}''')},
        "queries": ["/World", "/World/Box.size"],
        "task": "Find the three places metadata lives here: prim customData, prim assetInfo and property customData. "
        "Add a notes.usda layer with an over on /World that sets customData revision = 4, sublayer it from the root "
        "layer, compose, and confirm both layers show up in /World's prim stack (dictionaries merge key by key).",
    },
    {
        "topic": "properties: authored versus fallback",
        "keywords": ("property", "attribute", "relationship", "getproperties", "namespace"),
        "layers": {"props.usda": _usda('''
def Xform "World"
{
    def Cube "Box"
    {
        double size = 4
        custom double user:weight = 5
        rel user:target = </World/Target>
    }

    def Xform "Target"
    {
    }
}''')},
        "queries": ["/World/Box", "/World/Box.size", "/World/Box.user:target"],
        "task": "The prim query lists every property, including schema properties that only have a fallback. Delete "
        "the authored size and compose again: the value falls back to the Cube schema default and no layer appears "
        "in its property stack. Note how the user: namespace groups the custom properties.",
    },
    {
        "topic": "extents and surprising renders",
        "keywords": ("extent", "points", "boundable", "computeextent", "bounds", "purpose", "visibility",
                     "orientation", "metersperunit", "render", "visual"),
        "layers": {"tri.usda": _usda('''
def Xform "World"
{
    def Mesh "Tri"
    {
        float3[] extent = [(0, 0, 0), (1, 1, 0)]
        int[] faceVertexCounts = [3]
        int[] faceVertexIndices = [0, 1, 2]
        uniform token orientation = "rightHanded"
        point3f[] points = [(0, 0, 0), (4, 0, 0), (0, 4, 0)]
        uniform token purpose = "default"
        token visibility = "inherited"
    }
}''', "World", ("metersPerUnit = 0.01", 'upAxis = "Y"'))},
        "queries": ["/World/Tri.extent", "/World/Tri.points"],
        "task": "The authored extent is stale: it no longer bounds the points. Fix it so it covers every point and "
        "compose again. Then try purpose = \"guide\", visibility = \"invisible\" or orientation = \"leftHanded\" and "
        "say which would hide the triangle or flip its facing; metersPerUnit = 0.01 means the units are centimetres.",
    },
    {
        "topic": "UsdPreviewSurface and material binding",
        "keywords": ("usdpreviewsurface", "material", "shader", "material binding", "materialbindingapi",
                     "geomsubset", "bind", "binding strength", "diffusecolor", "shading network", "usdprimvarreader"),
        "layers": {"look.usda": _usda('''
def Xform "World"
{
    def Scope "Looks"
    {
        def Material "Red"
        {
            token outputs:surface.connect = </World/Looks/Red/Surface.outputs:surface>

            def Shader "Surface"
            {
                uniform token info:id = "UsdPreviewSurface"
                color3f inputs:diffuseColor = (0.8, 0.1, 0.1)
                float inputs:roughness = 0.4
                token outputs:surface
            }
        }
    }

    def Cube "Box" (
        prepend apiSchemas = ["MaterialBindingAPI"]
    )
    {
        rel material:binding = </World/Looks/Red>
    }
}''')},
        "queries": ["/World/Box.material:binding", "/World/Looks/Red/Surface.inputs:diffuseColor"],
        "task": "Add a Blue material and bind it to Box from a second layer sublayered above this one, then compose: "
        "the strongest binding opinion wins. For a primvar-driven color, add a UsdPrimvarReader_float3 shader that "
        "reads displayColor and connect its result to inputs:diffuseColor.",
    },
    {
        "topic": "asset paths and resolution",
        "keywords": ("asset path", "resolver", "unresolved", "missing layer", "anchored", "relative", "resolve",
                     "resolution", "uri", "ar", "context", "asset resolver"),
        "layers": {
            "shot.usda": _usda('''
def Xform "World"
{
    def "Crate" (
        prepend references = @./crate.usda@</Crate>
    )
    {
    }

    def "Spare" (
        prepend references = @crate.usda@</Missing>
    )
    {
    }
}'''),
            "crate.usda": _usda('''
def Cube "Crate"
{
    double size = 1
}''', "Crate"),
        },
        "root": "shot.usda",
        "queries": ["/World/Crate.size", "/World/Spare"],
        "task": "This starter fails on purpose: read the composition error for /World/Spare, whose reference names a "
        "prim crate.usda does not have. Fix it (drop the prim path so crate.usda's defaultPrim is used, or name "
        "</Crate>) and compose again. Both @./crate.usda@ and @crate.usda@ are relative paths, anchored to the layer "
        "that authors them.",
    },
    {
        "topic": "model kinds and asset structure",
        "keywords": ("kind", "model kind", "custom kind", "model hierarchy", "asset structure", "naming", "guidelines"),
        "layers": {"set.usda": _usda('''
def Xform "Set" (
    kind = "assembly"
)
{
    def Xform "Chair" (
        kind = "component"
    )
    {
        def Cube "Geom"
        {
        }
    }

    def Xform "Clutter" (
        kind = "group"
    )
    {
        def Xform "Cup" (
            kind = "component"
        )
        {
        }
    }
}''', "Set")},
        "queries": ["/Set/Chair", "/Set/Clutter/Cup"],
        "task": "Each prim query reports its kind. Change Clutter to kind = \"component\" and explain why Cup then "
        "stops being a valid model (a component may not sit inside another component). Then write three naming or "
        "layering rules you would give a team building assets like Chair.",
    },
    {
        "topic": "schemas and custom attributes",
        "keywords": ("schema", "usdgenschema", "isa schema", "api schema", "codeless", "custom attributes",
                     "applied api"),
        "layers": {"schema.usda": _usda('''
def Xform "World"
{
    def Cube "Box" (
        prepend apiSchemas = ["MaterialBindingAPI"]
    )
    {
        custom string studio:assetId = "BX-001"
        custom int studio:revision = 7
    }
}''')},
        "queries": ["/World/Box", "/World/Box.studio:assetId"],
        "task": "The studio: attributes are ad-hoc custom properties, while MaterialBindingAPI is an applied API "
        "schema listed in apiSchemas. Sketch a codeless API schema (for example StudioAssetAPI) that would declare "
        "studio:assetId and studio:revision so importers and exporters can rely on them.",
    },
    {
        "topic": "point instancing",
        "keywords": ("pointinstancer", "protoindices", "invisibleids", "prototypes relationship", "prototype"),
        "layers": {"trees.usda": _usda('''
def Xform "World"
{
    def PointInstancer "Trees"
    {
        int64[] invisibleIds = []
        point3f[] positions = [(0, 0, 0), (2, 0, 0), (4, 0, 0)]
        int[] protoIndices = [0, 0, 1]
        rel prototypes = [
            </World/Trees/Prototypes/Pine>,
            </World/Trees/Prototypes/Oak>,
        ]

        def Scope "Prototypes"
        {
            def Cone "Pine"
            {
            }

            def Sphere "Oak"
            {
            }
        }
    }
}''')},
        "queries": ["/World/Trees.protoIndices", "/World/Trees.prototypes", "/World/Trees.invisibleIds"],
        "task": "Add a third prototype (a Cube) to Prototypes and to the prototypes relationship, point the last "
        "protoIndices entry at it (index 2), and hide the first instance with invisibleIds = [0]. Compose and check "
        "each array: nothing else had to be rewritten.",
    },
    {
        "topic": "scenegraph instancing",
        "keywords": ("instancing", "instanceable", "instance proxy", "instance", "reuse", "large scene"),
        "layers": {
            "yard.usda": _usda('''
def Xform "Yard" (
    kind = "assembly"
)
{
    def "Crate_01" (
        instanceable = true
        prepend references = @crate.usda@
    )
    {
    }

    def "Crate_02" (
        instanceable = true
        prepend references = @crate.usda@
    )
    {
    }
}''', "Yard"),
            "crate.usda": _usda('''
def Xform "Crate" (
    kind = "component"
)
{
    color3f[] primvars:displayColor = [(0.6, 0.4, 0.2)] (
        interpolation = "constant"
    )

    def Cube "Box"
    {
    }
}''', "Crate"),
        },
        "root": "yard.usda",
        "queries": ["/Yard/Crate_01", "/Yard/Crate_01.primvars:displayColor"],
        "task": "Both crates are instances of one prototype, and prims below an instance cannot be edited in place. "
        "Override primvars:displayColor on /Yard/Crate_01 itself (the instance prim is editable) and compose: Box "
        "inherits the new color while both crates still share a prototype.",
    },
)

_GENERIC_STARTER: dict[str, Any] = {
    "topic": "layer stack basics",
    "keywords": (),
    "layers": {
        "shot.usda": _usda('''
over "World"
{
    over "Box"
    {
        double size = 3
    }
}''', "World", ("subLayers = [", "    @layout.usda@", "]")),
        "layout.usda": _usda('''
def Xform "World"
{
    def Cube "Box"
    {
        double size = 1
    }
}'''),
    },
    "root": "shot.usda",
    "queries": ["/World/Box.size"],
    "task": "Read the layer stack and the property stack for /World/Box.size: shot.usda's opinion (3) beats "
    "layout.usda's (1). Then connect it to today's goal: what would a tool working on this two-layer asset (an "
    "exporter, converter, validator or importer) have to read or keep?",
}


# ---------------------------------------------------------------- public API


def build_day(
    day: int | None = None,
    *,
    store: ProgressStore,
    bank: Any = None,
    blueprint: Any = None,
    knowledge: Any = None,
    today: date | datetime | str | None = None,
    exclude_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """One plan day as a lesson; ``day=None`` picks the next uncompleted day.

    ``bank`` needs ``quiz(...)`` (and ``load_bank()`` for predict items), ``blueprint`` needs ``study_plan(...)``
    (``objective``/``domain`` add detail) and ``knowledge`` needs ``search(query, top_k)``; all are optional.
    Each day's topics come from the schedule frozen when the plan was made. ``exclude_ids`` (for example the
    questions of an open mock exam) are never served in quizzes or predict exercises.
    """
    if store is None:
        raise TypeError("build_day needs a progress store")
    bank = bank if bank is not None else _default_bank()
    blueprint = blueprint if blueprint is not None else _default_blueprint()
    day_today = _as_date(today)
    moment = _moment(today)
    summary = _summary(store, bank, moment)
    plan_info = _ensure_plan(store, day_today, blueprint, summary)
    # The live plan only supplies objective texts and notes; the day-by-day topics stay frozen.
    plan = blueprint.study_plan(
        summary if summary["totals"]["answered"] else None, plan_info["days"], plan_info["minutes"]
    )
    schedule = list(plan_info["schedule"] or plan.get("schedule") or [])
    total = len(schedule)
    completed = set(plan_info["settings"]["completed_days"])
    calendar_day = (day_today - plan_info["start"]).days + 1
    base = {
        "total_days": total,
        "plan_start": plan_info["start"].isoformat(),
        "exam_date": plan_info["settings"]["exam_date"],
        "minutes_per_day": plan.get("minutes_per_day", plan_info["minutes"]),
        "calendar_day": calendar_day if 1 <= calendar_day <= total else None,
        "completed_days": sorted(completed),
    }
    if total == 0:
        return {"ok": False, **base, "message": "The study plan has no schedule; set an exam date or days first."}
    if day is None:
        day = next((n for n in range(1, total + 1) if n not in completed), None)
        if day is None:
            return {
                "ok": True,
                "plan_complete": True,
                **base,
                "message": f"All {total} days of this plan are complete. Re-plan with a new exam date (or days) to "
                "start a fresh schedule from today, or keep answering due reviews.",
                "how_to_continue": _how_to_continue(None),
            }
    else:
        number = _day_number(day)
        if number is None or not 1 <= number <= total:
            return {"ok": False, **base, "message": f"day must be a whole number from 1 to {total}."}
        day = number

    entry = schedule[day - 1]
    objectives = _objective_texts(plan)
    due_rows = store.due_reviews(limit=100, now=moment)
    ctx = {
        "bank": bank,
        "blueprint": blueprint,
        "knowledge": knowledge,
        "objectives": objectives,
        "due": due_rows,
        "used": set(),
        "blocked": {str(qid) for qid in exclude_ids or ()},
        "notes": [],
        "plan_start": plan_info["start"].isoformat(),
        "day": day,
        "total": total,
        "bank_items": None,
    }
    result: dict[str, Any] = {
        "ok": True,
        "day": day,
        "date": (plan_info["start"] + timedelta(days=day - 1)).isoformat(),
        "type": entry.get("type", "study"),
        "minutes": entry.get("minutes"),
        **base,
        "completed": day in completed,
    }
    if calendar_day > day and day not in completed:
        result["behind_by_days"] = calendar_day - day
    if result["type"] == "study":
        result["blocks"] = [_study_block(index, block, ctx) for index, block in enumerate(entry.get("blocks") or [], 1)]
        if not result["blocks"]:
            result["message"] = "This study day has no blocks scheduled; use it for due reviews."
    else:
        result.update(_review_day(result["type"], entry, summary, ctx))
    result["progress"] = {
        "answered": summary["totals"]["answered"],
        "accuracy": summary["totals"]["accuracy"],
        "due_count": summary["due_count"],
        "streak_days": summary["streak_days"],
        "weakest_objectives": [row.get("objective_id") for row in summary.get("weakest_objectives") or []],
    }
    result["how_to_continue"] = _how_to_continue(day)
    notes = ctx["notes"] + [
        "Each day's topics were fixed when the plan was made; quizzes put your due reviews first and review days "
        "target your current weak spots. Re-plan with learning_exam_set_date to re-prioritise."
    ]
    notes += [n for n in plan.get("notes") or [] if isinstance(n, str) and not n.startswith(_STANDARD_PLAN_NOTES)][:3]
    result["notes"] = list(dict.fromkeys(notes))
    return _fit(result)


def complete_day(
    day: int | None,
    store: ProgressStore,
    *,
    today: date | datetime | str | None = None,
    blueprint: Any = None,
) -> dict[str, Any]:
    """Mark a plan day done (``None`` = the next open day) and report streaks and the next day."""
    if store is None:
        raise TypeError("complete_day needs a progress store")
    blueprint = blueprint if blueprint is not None else _default_blueprint()
    plan_info = _ensure_plan(store, _as_date(today), blueprint)
    total = len(plan_info["schedule"]) if plan_info["schedule"] else plan_info["scheduled_days"]
    completed = set(plan_info["settings"]["completed_days"])
    if day is None:
        number = next((n for n in range(1, total + 1) if n not in completed), None)
        if number is None:
            return {"ok": False, "message": f"All {total} days are already complete.", "total_days": total,
                    "completed_days": sorted(completed), "plan_complete": True}
    else:
        number = _day_number(day)
        if number is None or not 1 <= number <= total:
            return {"ok": False, "message": f"day must be a whole number from 1 to {total}.", "total_days": total}
    already = number in completed
    if not already:
        completed = set(store.update_settings(completed_days=sorted(completed | {number}))["completed_days"])
    streak = 0
    cursor = number
    while cursor in completed:
        streak += 1
        cursor -= 1
    next_day = next((n for n in range(1, total + 1) if n not in completed), None)
    summary = store.summary(now=_moment(today))
    remaining = total - len([n for n in completed if n <= total])
    if next_day:
        ending = f" Next up: day {next_day}."
    elif plan_info["days"] > total:  # the exam lies beyond the scheduled window (at most 60 days)
        exam_date = plan_info["settings"]["exam_date"]
        when = f"on {exam_date}" if exam_date else f"{plan_info['days']} days after the plan start"
        ending = (f" That was the last scheduled day; your exam is {when}, so re-plan with learning_exam_set_date "
                  "to schedule the remaining days.")
    else:
        ending = " That was the last day of the plan. Good luck in the exam!"
    message = (f"Day {number} was already marked complete." if already else f"Day {number} complete.") + ending
    return {
        "ok": True,
        "day": number,
        "already_completed": already,
        "completed_days": sorted(completed),
        "completed_count": len(completed),
        "total_days": total,
        "remaining_days": remaining,
        "day_streak": streak,
        "study_streak_days": summary["streak_days"],
        "next_day": next_day,
        "plan_complete": next_day is None,
        "message": message,
    }


def set_exam_date(
    when: date | datetime | str | int | None,
    minutes_per_day: int | None = None,
    store: ProgressStore | None = None,
    *,
    today: date | datetime | str | None = None,
    blueprint: Any = None,
) -> dict[str, Any]:
    """Re-plan from today: ``when`` is an exam date (ISO string or date), a number of days, or None (default length).

    Day numbering restarts at day 1 today, so completed-day marks from the previous plan are cleared (answers and
    spaced-repetition cards are not touched).
    """
    if store is None:
        raise TypeError("set_exam_date needs a progress store")
    blueprint = blueprint if blueprint is not None else _default_blueprint()
    day_today = _as_date(today)
    previous = store.get_settings()
    exam_date: str | None = None
    days: int | None = None
    shown = _public_settings(previous)
    number = None if when is None or isinstance(when, date) else _day_number(when)
    if number is not None:
        if number < 1:
            return {"ok": False, "message": "days until the exam must be at least 1.", "settings": shown}
        days = number
        exam_date = (day_today + timedelta(days=days)).isoformat()
    elif when is not None:
        try:
            exam = _as_date(when)
        except (TypeError, ValueError):
            return {"ok": False, "message": "Give the exam date as 'YYYY-MM-DD' or a number of days.", "settings": shown}
        days = (exam - day_today).days
        if days < 1:
            return {"ok": False, "message": f"The exam date {exam.isoformat()} must be after today "
                    f"({day_today.isoformat()}).", "settings": shown}
        exam_date = exam.isoformat()
    if minutes_per_day is None:
        minutes = previous["minutes_per_day"]
    else:
        minutes = _day_number(minutes_per_day)
        if minutes is None or not MIN_MINUTES <= minutes <= MAX_MINUTES:
            return {"ok": False, "message": f"minutes_per_day must be a whole number from {MIN_MINUTES} to "
                    f"{MAX_MINUTES}.", "settings": shown}
    plan_days = days or DEFAULT_DAYS
    summary = _summary(store, None, None)
    plan = blueprint.study_plan(
        summary if summary["totals"]["answered"] else None, plan_days, minutes or DEFAULT_MINUTES
    )
    schedule = list(plan.get("schedule") or [])
    try:
        settings = store.update_settings(
            exam_date=exam_date,
            plan_start=day_today.isoformat(),
            days_until_exam=days,
            minutes_per_day=minutes,
            completed_days=[],
            schedule=schedule or None,
        )
    except (TypeError, ValueError) as exc:
        return {"ok": False, "message": str(exc), "settings": _public_settings(previous)}
    outline = []
    for entry in schedule[:MAX_OUTLINE_DAYS]:
        row = {"day": entry.get("day"), "type": entry.get("type"), "minutes": entry.get("minutes")}
        row["date"] = (day_today + timedelta(days=int(entry.get("day", 1)) - 1)).isoformat()
        ids = [b["objective"] for b in entry.get("blocks") or [] if isinstance(b, dict) and b.get("objective")]
        if ids:
            row["objectives"] = ids
        outline.append(row)
    cleared = len(previous["completed_days"])
    notes = [n for n in plan.get("notes") or [] if isinstance(n, str) and not n.startswith(_STANDARD_PLAN_NOTES)][:3]
    return {
        "ok": True,
        "settings": _public_settings(settings),
        "previous": _public_settings(previous),
        "cleared_completed_days": cleared,
        "days_until_exam": plan_days,
        "scheduled_days": len(schedule),
        "minutes_per_day": plan.get("minutes_per_day", minutes or DEFAULT_MINUTES),
        "outline": outline,
        "message": f"Planned {len(schedule)} days from {day_today.isoformat()}"
        + (f" to the exam on {exam_date}." if exam_date else f" (no exam date; default {DEFAULT_DAYS} days).")
        + (f" {cleared} completed-day marks from the previous plan were cleared." if cleared else ""),
        "notes": notes,
    }


def check_prediction(question_id: str, predicted: Any, *, bank: Any = None) -> dict[str, Any]:
    """Grade a 'predict the result' answer against the question's hidden verify expectation (no pxr needed)."""
    bank = bank if bank is not None else _default_bank()
    item = next((it for it in _bank_items(bank) if it.get("id") == str(question_id).strip()), None)
    check = _predict_check(item) if item else None
    if check is None:
        return {"ok": False, "correct": False, "question_id": question_id,
                "message": "That question id has no 'predict the result' check."}
    expected = check["expect"]
    if check["targets"]:  # accept /A/B, [/A/B], [</A/B>], comma lists and lists of strings, not only JSON
        if isinstance(predicted, (list, tuple)):
            raw = list(predicted)
        else:
            raw = re.split(r"[,\s]+", str(predicted).strip().strip("[]"))
        value: Any = [s for s in (str(t).strip().strip("<>\"'") for t in raw) if s]
    else:
        value = _parse_prediction(predicted)
    return {
        "ok": True,
        "question_id": item["id"],
        "query": check["query"],
        "your_answer": predicted,
        "correct": _same_value(value, expected),
        "expected": expected,
        "explanation": "Compose the layers with usd_playground_compose and read the property stack to see which "
        "opinion produced this value.",
    }


def explain_objective(text: str, domain: str | None, keywords: Iterable[str] = (), pass_name: str = "learn",
                      weight: Any = None) -> str:
    """A short plain-language lesson intro built from an objective's own wording."""
    goal = str(text or "").strip().rstrip(".")
    parts = []
    if goal:
        parts.append(f"Goal: be able to {goal[0].lower() + goal[1:]}.")
    intro = DOMAIN_INTROS.get(str(domain or ""))
    if intro:
        share = f" {domain} is {weight}% of the exam." if isinstance(weight, (int, float)) else ""
        parts.append(f"Why it matters: {intro}{share}")
    words = [str(k) for k in keywords if str(k).strip()][:6]
    if words:
        parts.append(f"Words to know: {', '.join(words)}.")
    parts.append(PASS_TIPS.get(pass_name, PASS_TIPS["learn"]))
    return " ".join(parts)


# ---------------------------------------------------------------- plan and settings helpers


def _default_bank() -> Any:
    from . import bank

    return bank


def _default_blueprint() -> Any:
    from . import blueprint

    return blueprint


def _as_date(value: Any) -> date:
    if value is None:
        return date.today()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and re.fullmatch(r"\s*\d{4}-\d{2}-\d{2}\s*", value):
        return date.fromisoformat(value.strip())
    raise ValueError(f"expected an ISO date such as '2026-11-30', got {value!r}")


def _moment(today: Any) -> datetime | None:
    """Noon UTC of an explicit ``today`` (keeps due/streak maths deterministic); None means 'now'."""
    return None if today is None else datetime.combine(_as_date(today), time(12, 0), tzinfo=timezone.utc)


def _day_number(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _ensure_plan(store: ProgressStore, today: date, blueprint: Any,
                 summary: dict[str, Any] | None = None) -> dict[str, Any]:
    """Effective plan settings. On first use saves plan_start, and whenever no schedule fits the plan it builds one
    from the current progress and saves it, so day N keeps meaning the same topics (the only writes a lesson makes).
    """
    settings = store.get_settings()
    if not settings["plan_start"]:
        settings = store.update_settings(plan_start=today.isoformat())
    start = date.fromisoformat(settings["plan_start"])
    days = settings["days_until_exam"]
    if days is None and settings["exam_date"]:
        days = max(1, (date.fromisoformat(settings["exam_date"]) - start).days)
    days = days or DEFAULT_DAYS
    minutes = settings["minutes_per_day"] or DEFAULT_MINUTES
    cap = getattr(blueprint, "MAX_SCHEDULE_DAYS", SCHEDULE_CAP)
    scheduled_days = max(1, min(days, cap))
    schedule = settings.get("schedule")
    if not schedule or len(schedule) != scheduled_days:
        if summary is None:
            summary = _summary(store, None, None)
        plan = blueprint.study_plan(summary if summary["totals"]["answered"] else None, days, minutes)
        schedule = list(plan.get("schedule") or []) or None
        if schedule:
            try:
                settings = store.update_settings(schedule=schedule)
                schedule = settings["schedule"]
            except (TypeError, ValueError):  # an unusual plan is used as built, just not frozen
                pass
    return {"settings": settings, "start": start, "days": days, "minutes": minutes,
            "scheduled_days": scheduled_days, "schedule": schedule}


def _public_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Settings as tools show them: the frozen schedule is reported as the outline, not repeated here."""
    return {key: value for key, value in settings.items() if key != "schedule"}


def _summary(store: ProgressStore, bank: Any, moment: datetime | None) -> dict[str, Any]:
    domains = list(getattr(bank, "DOMAINS", None) or []) if bank is not None else []
    return store.summary(now=moment, known_domains=domains or None)


def _objective_texts(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for item in plan.get("focus_objectives") or []:
        if isinstance(item, dict) and item.get("id"):
            found[str(item["id"])] = item
    return found


def _objective_info(oid: str, fallback_domain: str | None, ctx: dict[str, Any]) -> dict[str, Any]:
    blueprint = ctx["blueprint"]
    detail = None
    lookup = getattr(blueprint, "objective", None)
    if callable(lookup):
        try:
            detail = lookup(oid)
        except Exception:  # noqa: BLE001 - an injected blueprint may not know the id
            detail = None
    focus = ctx["objectives"].get(oid, {})
    detail = detail if isinstance(detail, dict) else {}
    domains = focus.get("domains") or []
    domain = detail.get("domain_name") or (domains[0] if domains else None) or fallback_domain
    info = {
        "id": oid,
        "text": str(detail.get("text") or focus.get("text") or ""),
        "domain": domain,
        "keywords": [str(k) for k in detail.get("keywords") or []],
        "high_yield": bool(detail.get("high_yield", "high_yield" in (focus.get("reasons") or []))),
        "estimated_exam_pct": detail.get("estimated_exam_pct", focus.get("estimated_exam_pct")),
        "weight": detail.get("domain_weight"),
    }
    readings: list[dict[str, Any]] = []
    domain_lookup = getattr(blueprint, "domain", None)
    if callable(domain_lookup) and domain:
        try:
            dom = domain_lookup(domain)
        except Exception:  # noqa: BLE001
            dom = None
        for reading in (dom or {}).get("readings") or []:
            if isinstance(reading, dict) and reading.get("url"):
                readings.append({"title": reading.get("title"), "url": reading["url"]})
        if info["weight"] is None and isinstance(dom, dict):
            info["weight"] = dom.get("weight")
    info["readings"] = readings[:READINGS_PER_BLOCK]
    return info


def _seed(*parts: Any) -> int:
    return zlib.crc32(":".join(str(p) for p in parts).encode("utf-8")) % 1_000_000


# ---------------------------------------------------------------- study blocks


def _study_block(index: int, block: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    oid = str(block.get("objective") or "")
    also = [str(o) for o in block.get("also") or []]
    pass_name = block.get("pass") if block.get("pass") in PASS_TIPS else "learn"
    info = _objective_info(oid, block.get("domain"), ctx)
    also_info = [_objective_info(other, None, ctx) for other in also]
    ids = [oid, *also]
    lesson = {
        "index": index,
        "minutes": block.get("minutes"),
        "pass": pass_name,
        "objective": {
            "id": oid,
            "text": info["text"],
            "domain": info["domain"],
            "also": [{"id": a["id"], "domain": a["domain"]} for a in also_info],
            "high_yield": info["high_yield"],
            "estimated_exam_pct": info["estimated_exam_pct"],
        },
        "explanation": explain_objective(info["text"], info["domain"], info["keywords"], pass_name, info["weight"]),
        "docs": _doc_hits(info, ctx),
        "readings": info["readings"],
    }
    domains = {info["domain"], *(a["domain"] for a in also_info)}
    lesson["exercise"] = _exercise(info, ids, pass_name, index, bool(domains & HANDS_ON_DOMAINS), ctx)
    exclude = set(ctx["used"])
    if lesson["exercise"].get("kind") == "predict":
        exclude.add(lesson["exercise"]["question_id"])
    lesson["quiz"] = _quiz_for(ids, ctx, _seed(ctx["plan_start"], ctx["day"], oid), exclude)
    return lesson


def _doc_hits(info: dict[str, Any], ctx: dict[str, Any]) -> list[dict[str, Any]]:
    knowledge = ctx["knowledge"]
    if knowledge is None:
        note = "No offline docs object was given, so doc hits were skipped (usd_docs_search finds them)."
        if note not in ctx["notes"]:
            ctx["notes"].append(note)
        return []
    query = " ".join(dict.fromkeys([*info["keywords"][:5], info["text"]])).strip()
    if not query:
        return []
    try:
        hits = knowledge.search(query, top_k=DOC_HITS)
    except Exception as exc:  # noqa: BLE001 - docs are optional; never fail a lesson over them
        note = f"Offline docs search failed ({type(exc).__name__}); doc hits were skipped."
        if note not in ctx["notes"]:
            ctx["notes"].append(note)
        return []
    rows = []
    for hit in list(hits or [])[:DOC_HITS]:
        if not isinstance(hit, dict):
            continue
        rows.append({
            "title": str(hit.get("title") or "")[:200],
            "source": hit.get("source"),
            "url_or_path": hit.get("url_or_path"),
            "snippet": str(hit.get("snippet") or "")[:SNIPPET_CAP],
        })
    return rows


def _exercise(info: dict[str, Any], ids: list[str], pass_name: str, index: int, hands_on: bool,
              ctx: dict[str, Any]) -> dict[str, Any]:
    keywords = {k.lower() for k in info["keywords"]}
    if hands_on:
        puzzle_friendly = bool(keywords & _PUZZLE_KEYWORDS)
        candidates = _predict_candidates(ids, ctx)
        if candidates and (not puzzle_friendly or pass_name == "revisit"):
            return _predict_item(candidates, info["id"], ctx)
        if puzzle_friendly:
            return _puzzle_item(info["id"], pass_name, index, ctx)
        starter = _match_starter(info)
        if starter is not None:
            return _try_it(starter, info)
        return _puzzle_item(info["id"], pass_name, index, ctx)
    return _try_it(_match_starter(info) or _GENERIC_STARTER, info)


def _puzzle_item(oid: str, pass_name: str, index: int, ctx: dict[str, Any]) -> dict[str, Any]:
    if pass_name == "revisit":
        difficulty = "hard"
    else:
        difficulty = "easy" if ctx["day"] <= max(1, ctx["total"] // 2) else "medium"
    puzzle = puzzles.generate_puzzle(seed=_seed(ctx["plan_start"], ctx["day"], oid, index), difficulty=difficulty)
    return {
        "kind": "puzzle",
        "topic": "LIVERPS opinion strength",
        "instruction": "Work out which opinion wins before checking. Read the layers, list the opinions strongest "
        "first, then pick an option.",
        "puzzle": {k: v for k, v in puzzle.items() if k not in _HIDDEN_KEYS},
        "how_to_answer": "learning_composition_puzzle_answer(puzzle_id, answer) with the option text or letter A-E.",
    }


def _predict_candidates(ids: list[str], ctx: dict[str, Any]) -> list[dict[str, Any]]:
    wanted = set(ids)
    rows = []
    for item in _bank_items(ctx["bank"], ctx):
        if (wanted & set(item.get("objective_ids") or []) and item.get("id") not in ctx["used"]
                and item.get("id") not in ctx.get("blocked", ())):
            if _predict_check(item) is not None:
                rows.append(item)
    return sorted(rows, key=lambda it: str(it.get("id")))


def _predict_item(candidates: list[dict[str, Any]], oid: str, ctx: dict[str, Any]) -> dict[str, Any]:
    item = candidates[_seed(ctx["plan_start"], ctx["day"], oid, "predict") % len(candidates)]
    check = _predict_check(item)
    assert check is not None
    ctx["used"].add(item["id"])
    what = "targets" if check["targets"] else "value"
    at = f" at time code {check['time']:g}" if check["time"] is not None else ""
    return {
        "kind": "predict",
        "question_id": item["id"],
        "objective_ids": list(item.get("objective_ids") or []),
        "prompt": f"Before composing anything, read the layers (root layer {check['root']}) and predict the resolved "
        f"{what} of {check['path']}{at}. Write your prediction down, then check it.",
        "layers": dict(item["usda"]),
        "root": check["root"],
        "queries": [check["query"]],
        "check_with": "usd_playground_compose(layers, root, queries) composes these layers and shows the winning "
        "opinion; the expected answer stays on the server under this question_id.",
    }


def _predict_check(item: Any) -> dict[str, Any] | None:
    """The first plain property check of a bank item's verify block, or None if it cannot be a prediction."""
    if not isinstance(item, dict):
        return None
    usda = item.get("usda")
    verify = item.get("verify")
    if not isinstance(usda, dict) or not usda or not isinstance(verify, dict):
        return None
    if any(key in verify for key in ("usda", "mute", "flatten")) or verify.get("load", "all") != "all":
        return None
    if not all(isinstance(name, str) and _SIMPLE_LAYER.match(name) for name in usda):
        return None
    root = verify.get("root") or item.get("root") or next(iter(usda))
    if root not in usda:
        return None
    for check in verify.get("checks") or []:
        path = check.get("path") if isinstance(check, dict) else None
        if not isinstance(path, str) or not path.startswith("/") or "." not in path.rsplit("/", 1)[-1]:
            continue
        expect = check.get("expect")
        if isinstance(expect, dict):
            if set(expect) != {"value"}:
                continue
            expect = expect["value"]
        when = check.get("time")
        if when is not None and (isinstance(when, bool) or not isinstance(when, (int, float))):
            continue
        targets = isinstance(expect, list) and bool(expect) and all(isinstance(t, str) and t.startswith("/")
                                                                     for t in expect)
        query = path if when is None else f"{path}@{when:g}"
        return {"path": path, "time": when, "expect": expect, "root": root, "query": query, "targets": targets}
    return None


def _bank_items(bank: Any, ctx: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    if ctx is not None and ctx.get("bank_items") is not None:
        return ctx["bank_items"]
    loader = getattr(bank, "load_bank", None)
    items: list[dict[str, Any]] = []
    if callable(loader):
        try:
            items = [it for it in loader() if isinstance(it, dict)]
        except (OSError, ValueError):
            items = []
    if ctx is not None:
        ctx["bank_items"] = items
    return items


def _match_starter(info: dict[str, Any]) -> dict[str, Any] | None:
    haystack = " ".join([*info["keywords"], info["text"]]).lower()
    best, best_score = None, 0
    for starter in _STARTERS:
        score = sum(1 for kw in starter["keywords"] if re.search(rf"\b{re.escape(kw)}\b", haystack))
        if score > best_score:
            best, best_score = starter, score
    return best


def _try_it(starter: dict[str, Any], info: dict[str, Any]) -> dict[str, Any]:
    layers = dict(starter["layers"])
    task = starter["task"]
    if starter is _GENERIC_STARTER and info["text"]:
        task = f"{task} Today's goal: {info['text']}"
    return {
        "kind": "try_it",
        "topic": starter["topic"],
        "tool": "usd_playground_compose",
        "instruction": f"Run usd_playground_compose with these layers, root and queries. {task}",
        "layers": layers,
        "root": starter.get("root") or next(iter(layers)),
        "queries": list(starter["queries"]),
    }


def _quiz_for(ids: list[str], ctx: dict[str, Any], seed: int, exclude: set[str]) -> dict[str, Any]:
    due = [row["question_id"] for row in ctx["due"] if set(ids) & set(row.get("objective_ids") or [])]
    questions: list[dict[str, Any]] = []
    for oid in ids:
        if len(questions) >= QUIZ_PER_BLOCK:
            break
        taken = exclude | {q["id"] for q in questions}
        result = _safe_quiz(ctx, objective=oid, count=QUIZ_PER_BLOCK - len(questions), seed=seed,
                            exclude_ids=sorted(taken), prefer_ids=due)
        questions += [q for q in result if q["id"] not in taken]
    questions = questions[:QUIZ_PER_BLOCK]
    ctx["used"].update(q["id"] for q in questions)
    return {
        "question_ids": [q["id"] for q in questions],
        "questions": questions,
        "how_to_answer": "learning_exam_answer(question_id, chosen=[...]) with 0-based option indices or letters.",
    }


def _safe_quiz(ctx: dict[str, Any], **kwargs: Any) -> list[dict[str, Any]]:
    quiz = getattr(ctx["bank"], "quiz", None)
    if not callable(quiz):
        return []
    blocked = ctx.get("blocked") or set()
    if blocked:
        kwargs["exclude_ids"] = sorted(set(kwargs.get("exclude_ids") or ()) | blocked)
    try:
        result = quiz(**kwargs)
    except (TypeError, ValueError) as exc:
        note = f"Some quiz questions could not be picked: {exc}"[:200]
        if note not in ctx["notes"]:
            ctx["notes"].append(note)
        return []
    rows = []
    for question in (result or {}).get("questions") or []:
        if isinstance(question, dict) and question.get("id") and question["id"] not in blocked:
            rows.append({k: v for k, v in question.items() if k not in _HIDDEN_KEYS})
    return rows


# ---------------------------------------------------------------- review days


def _review_day(kind: str, entry: dict[str, Any], summary: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    blocks = [b for b in entry.get("blocks") or [] if isinstance(b, dict)]
    domains: list[str] = []
    revisit_ids: list[str] = []
    practice_minutes = 0
    logistics = None
    for block in blocks:
        if block.get("domains"):
            domains += [str(d) for d in block["domains"]]
            practice_minutes += int(block.get("minutes") or 0)
        if block.get("objectives"):
            revisit_ids += [str(o) for o in block["objectives"]]
        if not block.get("domains") and not block.get("objectives"):
            logistics = {"activity": block.get("activity"), "minutes": block.get("minutes")}

    revisit = []
    reasons: dict[str, list[str]] = {}
    for oid in revisit_ids:
        reasons.setdefault(oid, []).append("high_yield")
    weak_rows = {row.get("objective_id"): row for row in summary.get("weakest_objectives") or [] if isinstance(row, dict)}
    for oid in weak_rows:
        if oid:
            reasons.setdefault(str(oid), []).append("weak")
    for oid, why in list(reasons.items())[:MAX_REVISIT]:
        info = _objective_info(oid, None, ctx)
        row = {"id": oid, "text": info["text"], "domain": info["domain"], "reasons": why}
        if oid in weak_rows:
            row["accuracy"] = weak_rows[oid].get("accuracy")
            row["answered"] = weak_rows[oid].get("answered")
        revisit.append(row)

    seed = _seed(ctx["plan_start"], ctx["day"], kind)
    due_bank = [row["question_id"] for row in ctx["due"] if not _PUZZLE_ID.match(str(row["question_id"]))]
    due_puzzles = []
    for row in ctx["due"]:
        match = _PUZZLE_ID.match(str(row["question_id"]))
        if match and len(due_puzzles) < MAX_DUE_PUZZLES:
            puzzle = puzzles.generate_puzzle(seed=int(match.group(2)), difficulty=match.group(1))
            due_puzzles.append({k: v for k, v in puzzle.items() if k not in _HIDDEN_KEYS})

    questions: list[dict[str, Any]] = []
    if kind == "review":
        target = max(5, min(MAX_MIXED, (practice_minutes or entry.get("minutes") or DEFAULT_MINUTES) // 3))
    else:
        target = min(MAX_FINAL_QUESTIONS, max(4, len(revisit)))
    if due_bank:
        known = {it.get("id") for it in _bank_items(ctx["bank"], ctx)} or set(due_bank)
        wanted = [qid for qid in due_bank if qid in known][: target // 2 or 1]
        if wanted:
            questions += [q for q in _safe_quiz(ctx, count=len(wanted), prefer_ids=wanted, seed=seed)
                          if q["id"] in wanted]
    if kind == "review":
        unique_domains = list(dict.fromkeys(domains))
        shares = _split(target - len(questions), len(unique_domains)) if unique_domains else []
        for name, share in zip(unique_domains, shares):
            taken = {q["id"] for q in questions}
            if share > 0:
                questions += [q for q in _safe_quiz(ctx, domain=name, count=share, seed=seed,
                                                    exclude_ids=sorted(taken)) if q["id"] not in taken]
        if not unique_domains and len(questions) < target:
            taken = {q["id"] for q in questions}
            questions += _safe_quiz(ctx, count=target - len(questions), seed=seed, exclude_ids=sorted(taken))
    else:
        for row in revisit:
            if len(questions) >= target:
                break
            taken = {q["id"] for q in questions}
            questions += [q for q in _safe_quiz(ctx, objective=row["id"], count=1, seed=seed,
                                                exclude_ids=sorted(taken)) if q["id"] not in taken]
    questions = questions[:target]

    result: dict[str, Any] = {
        "blocks": blocks,
        "mixed_practice": {
            "minutes": practice_minutes or None,
            "domains": list(dict.fromkeys(domains)),
            "due_review_ids": [row["question_id"] for row in ctx["due"]][:MAX_DUE_LISTED],
            "due_puzzles": due_puzzles,
            "question_ids": [q["id"] for q in questions],
            "questions": questions,
            "how_to_answer": "learning_exam_answer(question_id, chosen=[...]); time yourself (about 2 minutes "
            "per question is exam pace).",
        },
        "revisit": revisit,
    }
    if logistics:
        result["logistics"] = logistics
    return result


def _split(total: int, parts: int) -> list[int]:
    if parts <= 0 or total <= 0:
        return [0] * max(parts, 0)
    base, extra = divmod(total, parts)
    return [base + (1 if i < extra else 0) for i in range(parts)]


# ---------------------------------------------------------------- shared output helpers


def _how_to_continue(day: int | None) -> list[str]:
    finish = (
        f"When you are done, call learning_exam_day_complete(day={day}) to mark day {day} complete and see the next day."
        if day is not None else "learning_exam_day_complete marks a day complete and names the next one."
    )
    return [
        "Quiz questions: answer each with learning_exam_answer(question_id, chosen=[...]) using 0-based option "
        "indices or letters; results feed your progress and spaced-repetition reviews.",
        "Puzzles: answer with learning_composition_puzzle_answer(puzzle_id, answer) using the option text or letter.",
        "Predict and try-it exercises: run usd_playground_compose(layers, root, queries) with the given USDA "
        "(predict first, then compose to check).",
        "Due reviews: learning_exam_review lists cards that are due again, with their questions.",
        finish,
    ]


def _fit(result: dict[str, Any]) -> dict[str, Any]:
    """Keep the lesson under MAX_OUTPUT_CHARS: drop doc snippets first, then question bodies (ids stay)."""
    if len(json.dumps(result, default=str)) <= MAX_OUTPUT_CHARS:
        return result
    for block in result.get("blocks") or []:
        for hit in block.get("docs") or []:
            hit.pop("snippet", None)
    if len(json.dumps(result, default=str)) <= MAX_OUTPUT_CHARS:
        result.setdefault("notes", []).append("Doc snippets were left out to keep this lesson small.")
        return result
    quizzes = [block.get("quiz") for block in result.get("blocks") or []]
    quizzes.append(result.get("mixed_practice"))
    for quiz in quizzes:
        if isinstance(quiz, dict) and quiz.get("questions"):
            quiz["questions"] = []
            quiz["questions_omitted"] = True
    result.setdefault("notes", []).append(
        "Question texts were left out to keep this lesson small; ask for fewer minutes per day to get them inline."
    )
    return result


def _parse_prediction(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text.lower() in ("none", "null", "no value", "blocked"):
        return None
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    candidate = text.replace("(", "[").replace(")", "]")
    try:
        return json.loads(candidate)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text.strip("\"'")


def _same_value(actual: Any, expected: Any) -> bool:
    if isinstance(expected, bool) or isinstance(actual, bool):
        return isinstance(actual, bool) and isinstance(expected, bool) and actual is expected
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return math.isclose(float(actual), float(expected), rel_tol=1e-5, abs_tol=1e-6)
    if isinstance(expected, (list, tuple)):
        return isinstance(actual, (list, tuple)) and len(actual) == len(expected) and all(
            _same_value(a, e) for a, e in zip(actual, expected)
        )
    if isinstance(expected, str) and isinstance(actual, str):
        return actual.strip().strip("\"'").casefold() == expected.strip().casefold()
    return actual == expected
