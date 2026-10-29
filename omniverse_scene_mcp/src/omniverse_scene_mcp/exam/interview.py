"""Interview practice: spoken-style questions on OpenUSD, Omniverse and Isaac Sim, graded against key points.

A free-text answer is checked point by point: each key point lists several accepted phrasings (any one counts), so
the grade says which ideas you covered and which you left out, then shows a short model answer. It is a coach, not
an examiner: keyword matching cannot judge whether a sentence is right, only whether the idea came up.
Stateless: nothing is saved.
"""

from __future__ import annotations

import random
import re
from typing import Any

LEVELS = ("junior", "mid")


def _q(qid: str, topic: str, level: str, question: str, points: list[tuple[str, list[str]]], model: str,
       follow_up: str) -> dict[str, Any]:
    return {"id": qid, "topic": topic, "level": level, "question": question,
            "key_points": [{"point": point, "any": phrases} for point, phrases in points],
            "model_answer": model, "follow_up": follow_up}


QUESTIONS: list[dict[str, Any]] = [
    _q("usd-what", "usd_basics", "junior", "What is OpenUSD, and why do studios and robotics teams use it?", [
        ("It describes 3D scenes (a scene description / file format and API)", ["scene description", "describe 3d",
         "describes scenes", "file format", "framework"]),
        ("Non-destructive layering: many people or tools add opinions without overwriting each other",
         ["non-destructive", "nondestructive", "layer", "opinion"]),
        ("Composition assembles a scene from many files (references, payloads...)", ["composition", "compose",
         "reference", "payload"]),
        ("Interchange between tools (DCCs, simulators, renderers)", ["interchange", "between tools", "exchange",
         "interoperab", "different tools", "pipeline"]),
        ("It scales to very large scenes (lazy loading, instancing)", ["scale", "large scene", "lazy", "instanc",
         "performance"]),
    ], "OpenUSD is Pixar's open framework for describing, composing and exchanging 3D scenes. Its layers let many "
       "people and tools author non-destructively, composition builds a scene from many files, and it scales to huge "
       "scenes, which is why Omniverse and Isaac Sim use it as their common scene language.",
       "How is a layer different from a stage?"),
    _q("stage-layer-prim", "usd_basics", "junior", "Explain stage, layer and prim, and how they relate.", [
        ("A layer is one file (or in-memory document) of opinions", ["layer is", "one file", "a file", "document"]),
        ("A stage is the composed result of a stack of layers", ["stage is", "composed", "result", "view of"]),
        ("A prim is a node in the scene hierarchy with a path", ["prim is", "node", "hierarch", "path"]),
        ("Prims hold properties: attributes and relationships", ["attribute", "relationship", "propert"]),
    ], "A layer is one file of opinions. The stage is what you get after composing the root layer, its sublayers and "
       "every arc they bring in. A prim is one named node in that hierarchy (like /World/Robot), and it carries "
       "attributes (values) and relationships (pointers to other prims).",
       "What does the edit target decide?"),
    _q("livrps", "composition", "junior", "What does LIVRPS mean, and why does it matter?", [
        ("It is the strength order of composition arcs", ["strength", "order", "stronger", "weaker", "wins"]),
        ("Local, Inherits, Variants, (Relocates,) References, Payloads, Specializes", ["local", "inherit",
         "variant", "reference", "payload", "speciali"]),
        ("When two opinions disagree, the stronger arc wins", ["win", "override", "disagree", "conflict"]),
        ("Local opinions (the layer stack) beat everything else", ["local", "layer stack", "sublayer"]),
    ], "LIVRPS (LIVERPS with relocates) is composition's strength order: Local layer-stack opinions, then Inherits, "
       "Variants, rElocates, References, Payloads, Specializes. When two opinions set the same property, the stronger "
       "one wins, so a local override beats a value that came in through a reference.",
       "Why is specializes the weakest arc, and when would you use it?"),
    _q("ref-vs-payload", "composition", "junior", "When would you use a payload instead of a reference?", [
        ("Both bring another file's prims in", ["both", "bring in", "another file", "external"]),
        ("A payload can be unloaded / loaded on demand", ["unload", "load on demand", "deferred", "lazy", "not load"]),
        ("Used for heavy assets to keep the stage light and fast", ["heavy", "large", "memory", "fast", "performance"]),
        ("Payloads are weaker than references in LIVRPS", ["weaker", "strength", "livrps", "livers"]),
    ], "Both pull another file into the scene. A payload can be left unloaded, so you use it for heavy assets (a "
       "warehouse full of detailed shelves): open the stage fast, then load only what you need. It also sits just "
       "below references in strength.",
       "What happens to a prim's children when its payload is unloaded?"),
    _q("variants", "composition", "mid", "How would you build a product configurator (e.g. bag colours and sizes) "
       "in USD?", [
        ("Variant sets hold the alternatives", ["variant set", "variantset", "variants"]),
        ("A variant selection picks one, non-destructively", ["selection", "select", "switch", "pick"]),
        ("Each variant holds only the opinions that differ (material, mesh, visibility)", ["material", "mesh",
         "visibility", "differ", "override"]),
        ("Separate variant sets for independent choices (colour vs size)", ["separate", "independent", "two variant",
         "multiple variant", "each choice"]),
        ("Validate every combination", ["every combination", "test each", "check", "validate", "all combinations"]),
    ], "Put the alternatives in variant sets on the asset: one set per independent choice (colour, size), each "
       "variant authoring only what differs, such as the material binding or which mesh is visible. The app (or an "
       "Action Graph) switches the selection, which is non-destructive. Then check every combination renders.",
       "Where would you author the default selection, and why does layer strength matter for it?"),
    _q("instancing", "performance", "mid", "A warehouse scene with 5,000 identical boxes is slow. What do you do?", [
        ("Use instancing: mark the references instanceable", ["instanc", "instanceable"]),
        ("Instances share one prototype, so memory and load time drop", ["prototype", "share", "memory", "one copy"]),
        ("Point instancer for very many simple copies", ["point instancer", "pointinstancer"]),
        ("Payloads / unload what is not needed", ["payload", "unload"]),
        ("Measure first (profile, frame stats)", ["measure", "profile", "statistic", "frame", "fps"]),
    ], "Measure first, then make the boxes instanceable references so they share one prototype instead of 5,000 "
       "copies; for huge counts of simple objects a PointInstancer is lighter still. Heavy parts go behind payloads so "
       "they can stay unloaded.",
       "What can you no longer edit on an instance proxy, and how do you work around it?"),
    _q("units-upaxis", "debugging", "junior", "You import a robot and it is 100 times too big and lying on its side. "
       "What happened?", [
        ("metersPerUnit mismatch (cm vs m)", ["meters per unit", "metersperunit",
         "units", "centimet"]),
        ("upAxis mismatch (Y-up vs Z-up)", ["upaxis", "up axis", "y-up", "z-up", "y up", "z up"]),
        ("Fix with a wrapper transform / conversion, not by editing the source", ["wrapper", "scale", "rotate",
         "convert", "xform"]),
        ("Isaac Sim expects metres and Z-up", ["metre", "meter", "z-up", "z up"]),
    ], "Units and up axis. The asset was authored in centimetres (metersPerUnit 0.01) and Y-up, while the Isaac stage "
       "is metres and Z-up. Fix it with a wrapper transform (scale 0.01, rotate 90 degrees about X) or re-export in "
       "the right units rather than editing the source file.",
       "Why does a wrong scale also break physics, not just the look?"),
    _q("value-ignored", "debugging", "mid", "You set an attribute in a layer but the scene ignores it. How do you "
       "debug that?", [
        ("Find which opinion wins (a stronger layer or arc)", ["stronger", "wins", "winning opinion", "override",
         "livrps"]),
        ("Inspect the property stack / composition (e.g. GetPropertyStack, usdview)", ["property stack",
         "getpropertystack", "prim stack", "composition", "usdview", "resolve"]),
        ("Check the edit target was the layer you meant", ["edit target", "wrong layer"]),
        ("Check time samples beat a default value", ["time sample", "timesample", "animated", "default value"]),
        ("Check muted layers / unloaded payloads / variant selection", ["mute", "unload", "variant selection",
         "inactive"]),
    ], "Look at what wins: list the property's opinions strongest first (GetPropertyStack, or usdview's composition "
       "view). Usual culprits are a stronger layer or arc, edits that went to a different edit target, time samples "
       "that override the default value, or a muted layer, unloaded payload or other variant selection.",
       "How would you prove which layer wrote the winning value?"),
    _q("rigid-collider", "physics", "junior", "What is the difference between a rigid body and a collider in "
       "PhysX/USD Physics?", [
        ("A rigid body moves under physics (mass, velocity, gravity)", ["moves", "dynamic", "gravity", "mass",
         "velocity", "simulat"]),
        ("A collider gives a shape that things can touch", ["shape", "touch", "contact", "collision"]),
        ("Collider without a rigid body is static (a floor, a wall)", ["static", "floor", "wall", "does not move",
         "doesn't move"]),
        ("You need both for an object that falls and lands", ["both", "together", "fall"]),
        ("A PhysicsScene is needed for anything to simulate", ["physics scene", "physicsscene"]),
    ], "RigidBodyAPI makes a prim move under physics (mass, velocity, gravity); CollisionAPI gives it a shape others "
       "can touch. A collider alone is static, like a floor. A box that falls and lands needs both, and nothing "
       "simulates without a PhysicsScene.",
       "Why would a rigid body with no collider fall through the floor?"),
    _q("joints-drives", "physics", "mid", "Explain joints, drives and limits, using a robot arm or a door.", [
        ("A joint connects two bodies and removes degrees of freedom", ["connect", "two bodies", "body0", "body1",
         "degree", "freedom"]),
        ("Revolute = hinge, prismatic = slider (fixed, spherical, D6 too)", ["revolute", "hinge", "prismatic",
         "slider"]),
        ("Limits stop the motion at a range", ["limit", "range", "stop"]),
        ("A drive is a motor: target position/velocity with stiffness and damping", ["drive", "motor", "target",
         "stiffness", "damping"]),
        ("Articulation root makes a jointed robot one reduced-coordinate system", ["articulation"]),
    ], "A joint connects body0 and body1 and leaves only some motion free: a revolute joint is a hinge (a door), a "
       "prismatic joint a slider (a drawer). Limits bound the motion, e.g. the door opens 0 to 100 degrees. A drive is "
       "a motor that pulls toward a target position or velocity with stiffness and damping. On a robot an "
       "articulation root makes the whole chain one accurate reduced-coordinate system.",
       "What happens if the stiffness is very high and the damping is zero?"),
    _q("isaac-sensors", "isaac", "mid", "How do you add and read a camera and an IMU on an Isaac Sim robot?", [
        ("Put the sensor under the robot link that should carry it", ["under the link", "child", "link", "parent",
         "attach"]),
        ("Camera: a USD Camera read through a render product / annotators (rgb, depth)", ["render product",
         "annotator", "rgb", "depth", "camerasensor", "rtx"]),
        ("IMU needs a rigid-body link and the simulation playing", ["rigid", "play", "simulat"]),
        ("Read data each step (get_data) while playing", ["get_data", "read", "each step", "every frame"]),
        ("Watch scale / frames: don't nest under a scaled mesh", ["scale", "frame", "offset", "orientation"]),
    ], "Mount each sensor as a child of the link that should carry it, at an offset in that link's frame, never under "
       "a scaled mesh. A camera is a USD Camera that you read through a render product with annotators such as rgb "
       "and depth. An IMU needs a rigid-body link and reads only while the simulation plays; call get_data each step.",
       "Why does a falling IMU read almost zero acceleration?"),
    _q("sdg", "isaac", "mid", "What is synthetic data generation, and how would you make a labelled dataset in Isaac "
       "Sim?", [
        ("Rendered training data with perfect labels", ["synthetic", "rendered", "training data", "perfect label",
         "ground truth"]),
        ("Semantic labels on the prims", ["semantic", "label", "class"]),
        ("Replicator: render product + writer (BasicWriter) + annotators", ["replicator", "writer", "basicwriter",
         "annotator", "render product"]),
        ("Domain randomization (lighting, pose, materials) for variety", ["random", "domain randomization",
         "variation", "lighting"]),
        ("Validate the output (frame counts, labels)", ["validate", "check", "verify", "count"]),
    ], "Synthetic data is rendered training data whose labels are exact because the scene knows what every pixel is. "
       "Label the prims with semantic classes, then use Replicator: a camera render product, annotators (rgb, "
       "segmentation, 2D boxes) and a writer such as BasicWriter, with domain randomization for variety. Always check "
       "the output: every frame written, labels present.",
       "Why does a model trained only on synthetic data sometimes fail on real images?"),
    _q("pipeline-layers", "pipeline", "mid", "Several departments edit the same factory scene. How do you organise "
       "the layers?", [
        ("One sublayer per department / task", ["sublayer", "per department", "each department", "one layer"]),
        ("Order the layer stack by who should win", ["order", "stronger", "strength", "top"]),
        ("Assets referenced, not copied", ["reference", "asset"]),
        ("Avoid editing the same prim in two layers / clear ownership", ["owner", "same prim", "conflict",
         "clobber"]),
        ("Version control / checkpoints", ["version", "git", "checkpoint", "history"]),
    ], "Give each department its own sublayer (layout, lighting, physics) in a deliberate order so the right one "
       "wins, reference assets instead of copying them, give each prim a clear owner, and keep the files under "
       "version control.",
       "How do you move an opinion from one layer to another without changing the result?"),
    _q("project-story", "project", "junior", "Tell me about a project you built with Omniverse or Isaac Sim.", [
        ("The goal / problem", ["goal", "problem", "wanted", "built", "aim"]),
        ("What you did yourself (tools, code, scene)", ["i built", "i wrote", "i made", "i created", "i added",
         "i set up", "i designed"]),
        ("A technical challenge and how you solved it", ["challenge", "bug", "problem", "issue", "fixed", "solved",
         "debug"]),
        ("How you checked it worked (tests, measured results)", ["test", "verified", "measured", "checked",
         "validated"]),
        ("The result / what you learned", ["result", "learned", "learnt", "outcome", "next time"]),
    ], "Use the STAR shape. Example: 'I built an MCP router that lets an AI assistant drive Isaac Sim and USD "
       "Composer safely. Running it live, I found that manual PhysX stepping froze sleeping bodies; I traced it to a "
       "single pose write-back, fixed it to write every frame, and added a regression test. It now has 1,500+ tests "
       "on two USD versions, and I learned to verify live, not just in unit tests.'",
       "What would you do differently next time?"),
]
TOPICS = sorted({question["topic"] for question in QUESTIONS})
_BY_ID = {question["id"]: question for question in QUESTIONS}


def _public(question: dict[str, Any]) -> dict[str, Any]:
    return {"id": question["id"], "topic": question["topic"], "level": question["level"],
            "question": question["question"], "key_point_count": len(question["key_points"])}


def questions(topic: str | None = None, level: str | None = None, count: int = 5,
              seed: int | None = None) -> dict[str, Any]:
    """Interview questions (answers withheld), optionally filtered by topic and level, shuffled by seed."""
    if topic is not None and topic not in TOPICS:
        return {"ok": False, "error": f"Unknown topic '{topic}'. Topics: {', '.join(TOPICS)}."}
    if level is not None and level not in LEVELS:
        return {"ok": False, "error": f"Unknown level '{level}'. Levels: {', '.join(LEVELS)}."}
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= len(QUESTIONS):
        return {"ok": False, "error": f"count must be 1 to {len(QUESTIONS)}."}
    pool = [q for q in QUESTIONS if (topic is None or q["topic"] == topic) and (level is None or q["level"] == level)]
    random.Random(seed).shuffle(pool)
    return {"ok": True, "topics": TOPICS, "levels": list(LEVELS), "available": len(pool),
            "questions": [_public(q) for q in pool[:count]],
            "how": ("Answer out loud first (time yourself: 1-2 minutes), then type it into "
                    "learning_interview_answer(question_id, answer) for feedback.")}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().replace("-", " ").replace("_", " "))


def grade(question_id: str, answer: str) -> dict[str, Any]:
    """Which key points the answer covered, which it missed, a rating and the model answer."""
    question = _BY_ID.get(question_id)
    if question is None:
        return {"ok": False, "error": f"Unknown question id '{question_id}'. learning_interview_questions lists them."}
    if not isinstance(answer, str) or not answer.strip():
        return {"ok": False, "error": "Type your answer (a few sentences) to get feedback."}
    text = _norm(answer)
    covered, missed = [], []
    for point in question["key_points"]:
        hit = any(_norm(phrase) in text for phrase in point["any"])
        (covered if hit else missed).append(point["point"])
    score = round(100 * len(covered) / len(question["key_points"]))
    rating = "strong" if score >= 75 else "good start" if score >= 50 else "needs work"
    words = len(answer.split())
    tips = []
    if words < 40:
        tips.append(f"Only {words} words: interviewers want 1-2 minutes, with a concrete example.")
    if missed:
        tips.append("Add the missing points in your own words, then try again.")
    if not re.search(r"\b(for example|e\.g\.|such as|like|when i|in my)\b", answer.lower()):
        tips.append("Give an example ('for example, in my physics test cell...'): it shows you have done it.")
    return {"ok": True, "question_id": question_id, "question": question["question"], "score_pct": score,
            "rating": rating, "covered": covered, "missed": missed, "tips": tips,
            "model_answer": question["model_answer"], "follow_up": question["follow_up"],
            "note": "Keyword coaching: it checks which ideas came up, not whether each sentence is correct."}
