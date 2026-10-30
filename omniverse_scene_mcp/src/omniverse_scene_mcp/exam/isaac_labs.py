"""Hands-on Isaac Sim labs for NCP-OUSD objectives: learn the idea in plain words, then see it in the viewport.

Each lab is data: a short concept, then steps. A step names one router tool with exact arguments, says what to look
for in Isaac Sim, and is either a read (call it directly) or an edit (run it through scene_safe_edit: preview, the
learner's yes, then apply with the confirm code). Labs work in a dedicated study stage, never in a course scene:
isaac_study/study.usda inside the workspace, each lab under its own /World/<Lab> prim.

Some objectives are about compiling plugins or writing resolvers; a viewport cannot show those, so their lab says so
and points at the Python or USDA exercise instead of pretending.
"""

from __future__ import annotations

from typing import Any

from .progress import workspace_root

STUDY_FOLDER = "isaac_study"
STUDY_STAGE = "study.usda"


def _assets() -> str:
    return (workspace_root() / "isaacsim-assets-6.1.0" / "Assets" / "Isaac" / "6.1" / "Isaac").as_posix()


def _read(tool: str, arguments: dict[str, Any], look: str, say: str = "") -> dict[str, Any]:
    return {"kind": "read", "say": say, "tool": tool, "arguments": arguments, "look": look}


def _edit(tool: str, arguments: dict[str, Any], look: str, say: str = "") -> dict[str, Any]:
    return {"kind": "edit", "say": say, "tool": tool, "arguments": arguments, "look": look}


def _ui(say: str, look: str) -> dict[str, Any]:
    """Something the learner does by hand in Isaac Sim (no tool call): a click, a drag, finding a panel."""
    return {"kind": "ui", "say": say, "tool": None, "arguments": {}, "look": look}


# Taught before any exam objective: the learner may know nothing about Isaac Sim or USD yet.
FOUNDATIONS = ("basics_isaac", "basics_usd")
# Labs for the mentorship class sessions (Isaac Sim skills that are not NCP-OUSD objectives), one per session,
# following the syllabus (Downloads/digital-twins-physical-ai-mentorship): lab id -> class session.
CLASS_LABS = {
    "action_graph": 6,
    "python_in_isaac": 7,
    "python_automation": 8,
    "robot_drive": 9,
    "sensors_perception": 10,
    "warehouse_setup": 11,
    "warehouse_debug": 12,
    "package_sorting": 13,
    "factory_cell": 14,
    "capstone_challenge": 15,
}

# Class session 7 scripts. The learner types them into Window > Script Editor; python_check_script and
# python_trial_run look at them first (on a throwaway practice scene that has /World/Box).
PY_READ = (
    "import omni.usd\n"
    "\n"
    "stage = omni.usd.get_context().get_stage()   # the scene that is open right now\n"
    "box = stage.GetPrimAtPath(\"/World/Box\")      # find one prim by its path\n"
    "print(\"found:\", box.IsValid())               # False means: wrong path\n"
    "size = box.GetAttribute(\"size\").Get()        # read one attribute\n"
    "print(\"size is\", size)\n"
)
PY_BUGGY = (
    "import time\n"
    "import omni.usd\n"
    "\n"
    "stage = omni.usd.get_context().get_stage()\n"
    "while True:                 # wait for the box to fall\n"
    "    time.sleep(1)\n"
    "    print(stage.GetPrimAtPath(\"/World/Box\"))\n"
)
PY_MANY_BOXES = (
    "import omni.usd\n"
    "from pxr import Gf, UsdGeom\n"
    "\n"
    "stage = omni.usd.get_context().get_stage()\n"
    "UsdGeom.Xform.Define(stage, \"/World/PyLab\")              # a group for this lab\n"
    "for i in range(5):                                          # i = 0, 1, 2, 3, 4\n"
    "    cube = UsdGeom.Cube.Define(stage, f\"/World/PyLab/Box_{i}\")\n"
    "    cube.CreateSizeAttr(0.5)\n"
    "    UsdGeom.XformCommonAPI(cube).SetTranslate(Gf.Vec3d(i, 12, 0.5))\n"
    "    print(\"made\", cube.GetPath())\n"
)
# Class session 13: scene_spawn_packages(seed=4) makes these colours (a seed repeats a batch; the tests check it).
SORT_PACKAGE_COLOURS = {"Package_001": "red", "Package_002": "red", "Package_003": "blue",
                        "Package_004": "blue", "Package_005": "red", "Package_006": "blue"}


def _physics_first() -> dict[str, Any]:
    return _edit("scene_add_physics_scene", {},
                 "Nothing visible: the scene has gravity and physics. (Skip this step if it says one exists already.)",
                 "Make sure the scene has physics.")


def _floor(group: str, centre: list[float], half: float) -> list[dict[str, Any]]:
    """Three steps: a flat cube floor with a collider, so things that fall land on it."""
    size = round(2 * half)
    return [
        _edit("scene_create", {"path": f"{group}/Floor", "type_name": "Cube"}, "A big grey cube appears.",
              "Make a floor for this lab (a cube we will flatten)."),
        _edit("scene_move", {"path": f"{group}/Floor", "translation": [centre[0], centre[1], -0.05],
                             "scale": [half, half, 0.05]},
              f"It flattens into a {size} x {size} m slab whose top is the ground line.", "Flatten it."),
        _edit("scene_make_collider", {"prim_path": f"{group}/Floor"},
              "Nothing visible: the floor is now solid, so things can land on it.", "Give the floor a collider."),
    ]


def _session_labs() -> dict[str, dict[str, Any]]:
    """Class sessions 7-15 of the mentorship syllabus, each in its own /World/<Group> of the study stage."""
    from ..graph_builder import SCRIPT_TEMPLATES

    study = (workspace_root() / STUDY_FOLDER).as_posix()
    belt_json = f"{study}/belt_speed.json"
    dataset = f"{study}/sort_dataset"
    challenge_file = f"{study}/debug/frozen_box.usda"
    # Session 12: the spawn_package template makes 20 cm boxes; the Franka's fingers open about 8 cm.
    small_spawner = SCRIPT_TEMPLATES["spawn_package"]["code"].replace("cube.CreateSizeAttr(0.2)",
                                                                       "cube.CreateSizeAttr(0.05)")
    sort_packages = [f"/World/Sort/Packages/{name}" for name in SORT_PACKAGE_COLOURS]
    sort_randomize = {"prims": sort_packages, "position": {"min": [15.1, 9.6, 0.1], "max": [15.9, 10.4, 0.1]},
                      "lights": ["/World/Sort/Light"], "intensity": [500, 2000]}
    sdg_args = {"project_dir": study, "name": "sort_dataset", "camera": "/World/Sort/SortCam", "frames": 12,
                "width": 320, "height": 240, "annotations": ["rgb", "semantic_segmentation"],
                "randomize": sort_randomize}
    return {
        "python_in_isaac": {
            "title": "Class session 7: Python for Isaac Sim (find a prim, read it, make boxes with a loop)",
            "objectives": [],
            "class_session": 7,
            "words": {
                "Python": "a programming language: lines of instructions the computer follows top to bottom, "
                          "like a recipe",
                "Script Editor": "a panel inside Isaac Sim (Window > Script Editor) where you type Python and press "
                                 "Run; the code works on the scene that is open",
                "variable": "a named box that holds a value, e.g. size = 2 (later you write size and get 2)",
                "data type": "what kind of value it is: a whole number (int), a decimal (float), text (str), "
                             "True/False (bool), a list [1, 2, 3]",
                "list": "several values in order inside [ ], like a shopping list",
                "loop": "'for i in range(5):' repeats the indented lines 5 times, with i = 0, 1, 2, 3, 4",
                "condition": "'if x > 1:' runs the indented lines only when the test is True",
                "function": "a named, reusable set of lines, e.g. print('hi') or GetPrimAtPath(path)",
                "import": "'import omni.usd' loads a toolbox of ready-made functions so you can use them",
                "object": "a value with its own functions, called with a dot: prim.IsValid(), stage.Traverse()",
                "f-string": "text with values filled in: f\"Box_{i}\" becomes Box_0, Box_1, ...",
            },
            "concept": (
                "In class the teacher used only the Python that Isaac needs. A script in the Script Editor always "
                "starts the same way: import omni.usd, then stage = omni.usd.get_context().get_stage() (the open "
                "scene). stage.GetPrimAtPath('/World/Box') finds one prim by its address; a wrong path does not "
                "crash, it gives an invalid prim, so check prim.IsValid(). prim.GetAttribute('size').Get() reads a "
                "value, and UsdGeom.XformCommonAPI(prim).SetTranslate(...) moves it. A for loop repeats the same "
                "lines, so one short script can make many boxes. Before running code on your scene, we check it "
                "(python_check_script) and try it on a throwaway practice scene (python_trial_run)."),
            "steps": [
                _ui("Open the Script Editor: in Isaac Sim's top menu bar click Window > Script Editor. A panel opens "
                    "(usually docked at the bottom, under the viewport) with a big text box and a Run button.",
                    "An empty text area. You will type (or paste) Python there and press Run (or Ctrl+Enter)."),
                _read("python_practice_list", {"level": "isaac"},
                      "Five Isaac exercises in order: get the stage, find and read a prim, move a prim, many boxes, "
                      "physics and Play. They are your homework practice after this lab.",
                      "See the Isaac Python exercises (nothing runs)."),
                _read("python_check_script", {"code": PY_READ},
                      "No errors: the script imports omni.usd, gets the stage and reads one attribute.",
                      "Check a small reading script for beginner mistakes before running it."),
                _read("python_trial_run", {"code": PY_READ},
                      "It printed 'found: True' and the box's size, and created nothing: reading never changes "
                      "the scene.", "Run it on a throwaway practice scene (World + Box), never your real one."),
                _ui("Now in Isaac: type the same script into the Script Editor, but change \"/World/Box\" to "
                    "\"/World/Basics/MyCube\" (the cube from Lesson 0b). Press Run.",
                    "The Script Editor's output shows found: True and the cube's size (0.5 if you did Lesson 0b). "
                    "If it says found: False, the path has a typo: compare it with the Stage panel."),
                _read("python_check_script", {"code": PY_BUGGY},
                      "It flags the 'while True' loop and time.sleep: in the Script Editor they freeze Isaac Sim, "
                      "because the app cannot draw while your script waits. This is basic debugging: read the "
                      "finding, then fix the line it names.",
                      "Check a script with a classic mistake (do NOT run this one)."),
                _read("python_trial_run", {"code": PY_MANY_BOXES},
                      "It reports six new prims on the practice scene: the group /World/PyLab and Box_0 ... Box_4. "
                      "The loop ran five times, and the f-string gave each box its own name.",
                      "Try the loop that makes many boxes on the practice scene first."),
                _ui("In Isaac: clear the Script Editor (select all, Delete), type the many-boxes script and press "
                    "Run. This changes the study scene by your own hand, like typing a value in the Property panel; "
                    "the trial run above was its preview.",
                    "Five 0.5 m cubes appear in a row along X at Y = 12, and a PyLab group in the Stage panel."),
                _read("scene_children", {"path": "/World/PyLab"},
                      "Box_0 ... Box_4: Python made exactly the prims the trial run predicted.",
                      "Check from my side what the script made."),
                _read("python_practice_get", {"exercise_id": "isaac03-move-prim"},
                      "The task, starter code and hints for moving a prim from a script (the solution stays hidden).",
                      "Open the next exercise: changing a transform from Python."),
            ],
            "try_yourself": "Change the loop so it makes 10 boxes in two rows (hint: x = i % 5, y = 12 + i // 5). "
                            "Check it with python_check_script, preview it with python_trial_run, then solve "
                            "isaac04-many-boxes with python_practice_run(exercise_id, code).",
        },
        "python_automation": {
            "title": "Class session 8: Python automation and Script Nodes (random packages, press T to move)",
            "objectives": [],
            "class_session": 8,
            "words": {
                "automation": "letting code do a repeated job for you, like a dishwasher instead of washing by hand",
                "random": "picked by chance each time, like rolling a die",
                "seed": "a starting number for the random picks: the same seed gives the same 'random' result "
                        "again, like replaying a recorded dice game",
                "spawn": "create a new object in the scene while you work or while it plays",
                "rigid body": "a prim that physics moves: it falls, slides and bumps",
                "Script Node": "an Action Graph node whose job is a piece of Python you write: Python inside "
                               "the graph",
                "setup / compute": "a Script Node's two main functions: setup runs once at the start, compute "
                                   "runs every time the node is triggered",
                "pin": "a dot on a node's side: inputs on the left (what it needs), outputs on the right "
                       "(what it gives)",
                "reset": "making a Script Node forget its old code so new code is used (its Reset button)",
            },
            "concept": (
                "Hour 1: instead of placing packages one by one, a script places many with random positions, sizes "
                "and colours: a package generator. A seed makes the 'random' batch repeatable, which is how you "
                "debug random things. Hour 2: a Script Node puts Python inside an Action Graph. Its inputs are pins "
                "you choose (here: which prim, how far to move), compute() runs each time a wire triggers it, and "
                "outputs pass information on to the next node. One thing surprises everyone: a Script Node "
                "compiles its code once; after you change the code it must be reset, or the old code keeps "
                "running."),
            "steps": [
                _physics_first(),
                _edit("scene_create", {"path": "/World/Spawn", "type_name": "Xform"}, "A Spawn group appears.",
                      "Make a group for this lab."),
                *_floor("/World/Spawn", [0, 16], 1.5),
                _edit("scene_spawn_packages", {"parent": "/World/Spawn/Packages", "count": 6,
                                               "region_min": [-0.5, 15.5, 0.5], "region_max": [0.5, 16.5, 1.0],
                                               "size_range": [0.1, 0.2], "colors": ["red", "green", "blue"],
                                               "seed": 7},
                      "Six coloured boxes of different sizes float above the floor. The preview listed every "
                      "position, size and colour, and the Python that does it (random.uniform, random.choice).",
                      "Run the random package generator: 6 packages, sizes 10-20 cm, three colours, seed 7."),
                _read("scene_children", {"path": "/World/Spawn/Packages"},
                      "Package_001 ... Package_006: names made by the loop, like Box_0 ... Box_4 in session 7."),
                _ui("Press Play (left toolbar of the viewport) and watch, then press Stop.",
                    "The packages fall and pile on the floor (they are rigid bodies). Stop puts them back."),
                _edit("scene_create", {"path": "/World/Spawn/Target", "type_name": "Cube"}, "A grey cube appears.",
                      "Make the cube our Script Node will move."),
                _edit("scene_move", {"path": "/World/Spawn/Target", "translation": [1, 17, 0.3],
                                     "scale": [0.1, 0.1, 0.1]},
                      "A small 20 cm cube sits at the floor's corner. It has no physics, so only our code moves it.",
                      "Make it small and put it aside."),
                _edit("graph_create", {"graph_path": "/World/Spawn/Graph"},
                      "A Graph prim appears in the Stage panel.", "Make an empty Action Graph."),
                _edit("graph_add_script_node", {"graph_path": "/World/Spawn/Graph", "name": "MoveBox",
                                                "template": "move_on_trigger",
                                                "values": {"inputs:prim_path": "/World/Spawn/Target"},
                                                "trigger_key": "T"},
                      "The preview shows the node's Python (setup, compute, cleanup) and its two input pins: "
                      "prim_path and offset (0, 0, 0.5). After applying: a Script Node and an On Keyboard Input (T).",
                      "Add a Script Node that moves a prim each time it is triggered, fired by the T key."),
                _ui("In the Stage panel, right-click /World/Spawn/Graph > Open Graph. Click the MoveBox node and "
                    "look at the Property panel (bottom right).",
                    "A white wire from KeyT's Pressed to MoveBox's Exec In. The Property panel shows the Script "
                    "field (the Python) and the inputs prim_path and offset."),
                _ui("Press Play, click once inside the viewport (so it gets the keyboard), then press T three times.",
                    "The target cube jumps up 0.5 m per press. It stays there after Stop: no physics put it back."),
                _edit("graph_set_value", {"graph_path": "/World/Spawn/Graph", "pin": "MoveBox.inputs:offset",
                                          "value": [0.2, 0, 0]},
                      "MoveBox's offset input now reads (0.2, 0, 0); press T while playing: it slides sideways.",
                      "Change an INPUT pin: the same code now does something else."),
                _edit("graph_add_script_node", {"graph_path": "/World/Spawn/Graph", "name": "Count",
                                                "template": "counter"},
                      "A second Script Node with one output pin, count.", "Add a counter Script Node."),
                _edit("graph_connect", {"graph_path": "/World/Spawn/Graph", "source": "MoveBox.outputs:execOut",
                                        "target": "Count.inputs:execIn"},
                      "A white wire from MoveBox to Count: after each move, the counter runs.",
                      "Pass the trigger on from one Script Node to the next."),
                _read("graph_inspect", {"root_path": "/World/Spawn/Graph"},
                      "'When key T is pressed -> MoveBox -> Count': the chain in plain words."),
                _ui("Press Play, click the viewport, press T four times. Then click Count in the graph and read "
                    "outputs:count in the Property panel. Press Stop.",
                    "count = 4: an output carries information out of a Script Node, ready for the next node."),
                _ui("Only read this one: if you edit the Python in a Script Node's Script field by hand, press the "
                    "node's Reset button in the Property panel, or Isaac keeps running the OLD code. "
                    "(graph_set_value on inputs:script resets it for you.)",
                    "Say back in one sentence why new Script Node code can be ignored."),
            ],
            "try_yourself": "Build the between-session random package spawner: graph_add_script_node with "
                            "template='spawn_package', trigger_key='P', values={'inputs:parent': "
                            "'/World/Spawn/Spawned', 'inputs:spawn_at': [0, 16, 1]}. Press P while playing. "
                            "Why does each new package get a different colour?",
        },
        "robot_drive": {
            "title": "Class session 9: robot simulation (drive a two-wheel Jetbot: move, stop, turn, move)",
            "objectives": [],
            "class_session": 9,
            "words": {
                "link": "one solid part of a robot (the body, a wheel), like a bone",
                "joint": "the connection that lets two links move against each other, like an elbow or an axle",
                "degree of freedom (DOF)": "one way a robot can move; each turning joint adds one. A Jetbot has 2 "
                                           "(two wheels); a Franka arm has 9 (7 arm joints + 2 fingers)",
                "articulation": "a robot whose links and joints physics solves together as one machine",
                "differential drive": "steering by giving the left and right wheels different speeds, like a "
                                      "tank or a wheelchair: same speed = straight, opposite speeds = spin",
                "velocity target": "the speed you ask a joint's motor to reach, in degrees per second",
                "controller": "the part that turns 'go 0.3 m/s and turn' into a speed for each wheel",
                "waypoint": "a point on a route the robot drives to, then the next, like stops on a bus line",
            },
            "concept": (
                "A robot is links joined by joints. A Jetbot is a body (chassis) with two wheel joints, so it has 2 "
                "degrees of freedom. It has no steering wheel: if both wheels turn forward at the same speed it "
                "goes straight; if they turn opposite ways it spins on the spot; zero stops it. First we send wheel "
                "speeds by hand while it plays. Then an Action Graph does a route for us: move -> stop -> turn -> "
                "move, each step waiting some seconds, with a Differential Controller node turning 'speed and turn "
                "rate' into the two wheel speeds."),
            "steps": [
                _physics_first(),
                _edit("scene_create", {"path": "/World/Drive", "type_name": "Xform"}, "A Drive group appears.",
                      "Make a group for this lab."),
                *_floor("/World/Drive", [8, 0], 3),
                _edit("robot_add_ready", {"name": "jetbot", "prim_path": "/World/Drive/Jetbot",
                                          "position": [6, 0, 0]},
                      "A small two-wheeled robot appears on the floor, facing +X.", "Add a Jetbot."),
                _read("scene_children", {"path": "/World/Drive/Jetbot"},
                      "Its links: chassis, left_wheel, right_wheel (and a few helper prims).",
                      "Look at the robot's structure: links."),
                _read("robot_joint_table", {"robot_root": "/World/Drive/Jetbot"},
                      "Two revolute (turning) joints, left_wheel_joint and right_wheel_joint: 2 degrees of freedom.",
                      "Now its joints."),
                _ui("Press Play. Nothing moves yet: no one asked the wheels to turn.",
                    "The Jetbot sits on the floor (keep it playing for the next steps)."),
                _edit("robot_live_spin", {"robot_root": "/World/Drive/Jetbot",
                                          "velocities": {"left_wheel_joint": 360, "right_wheel_joint": 360}},
                      "Both wheels turn one full turn per second: the Jetbot drives straight ahead (+X).",
                      "Same speed on both wheels: forward."),
                _read("robot_joint_state", {"robot_root": "/World/Drive/Jetbot"},
                      "Each wheel's velocity is near 360 deg/s and its position keeps growing.",
                      "Read what the joints are doing right now."),
                _edit("robot_live_spin", {"robot_root": "/World/Drive/Jetbot",
                                          "velocities": {"left_wheel_joint": -180, "right_wheel_joint": 180}},
                      "The wheels turn opposite ways: the Jetbot spins on the spot (turns left).",
                      "Opposite speeds: turn."),
                _edit("robot_live_spin", {"robot_root": "/World/Drive/Jetbot",
                                          "velocities": {"left_wheel_joint": 0, "right_wheel_joint": 0}},
                      "It stops.", "Zero: stop."),
                _ui("Press Stop.", "The Jetbot jumps back to where it started."),
                _edit("graph_apply", {"name": "drive_sequence", "graph_path": "/World/Drive/RouteGraph",
                                      "options": {"robot": "/World/Drive/Jetbot"}},
                      "The preview lists the nodes: On Playback Tick, Delays, variables speed/turn, a Differential "
                      "Controller and an Articulation Controller for the wheels. After applying: a RouteGraph prim.",
                      "Build the route graph: move 2 s -> stop 1 s -> turn about 90 degrees -> move 2 s."),
                _read("graph_inspect", {"root_path": "/World/Drive/RouteGraph"},
                      "The steps in order, each setting speed and turn, then waiting its seconds."),
                _ui("Press Play and watch for about 7 seconds, then press Stop.",
                    "It drives 0.6 m, stops, turns left about 90 degrees, drives 0.6 m. (This recipe has not been "
                    "driven live on this laptop yet: if the Jetbot does not move, tell me what you see and we check "
                    "it together with scene_diagnose(focus='graph').)"),
            ],
            "try_yourself": "Between-session task, a simple route: make your own steps list, e.g. a square "
                            "(move, turn, move, turn, ...), with graph_apply(name='drive_sequence', graph_path="
                            "'/World/Drive/Square', options={'robot': '/World/Drive/Jetbot', 'steps': [...]}). Delete "
                            "RouteGraph first so two graphs do not fight over the wheels. Which number makes the turn "
                            "90 degrees?",
        },
        "sensors_perception": {
            "title": "Class session 10: sensors and perception (camera, IMU, stop before an obstacle)",
            "objectives": [],
            "class_session": 10,
            "words": {
                "sensor": "a part that measures something about the world, like your eyes or your inner ear",
                "RGB camera": "a camera that gives a colour picture: red, green, blue values for every pixel",
                "depth camera": "a camera that gives, for every pixel, how far away that point is (metres)",
                "LiDAR": "a sensor that shoots laser beams around and measures how far each one travels: a cloud "
                         "of 3D points",
                "IMU": "a sensor that feels acceleration and turning (like the one that rotates your phone's "
                       "screen); standing still it feels gravity, about 9.8 m/s^2",
                "perception": "turning sensor data into a fact the robot can use: 'there is an obstacle 0.4 m ahead'",
                "sense -> decide -> act": "every robot loop: measure, choose what to do, do it, repeat",
            },
            "concept": (
                "A simulated sensor gives the same kind of data as a real one, but perfect and free: a camera gives "
                "a picture, a depth camera a distance per pixel, a LiDAR a point cloud, an IMU acceleration and "
                "turning. Where you place it matters: a camera on the chassis moves with the robot. Perception is "
                "the step from data to a decision. In class: sense how far the obstacle is, decide 'closer than "
                "0.5 m?', act by stopping the wheels. Our obstacle_stop graph measures the distance directly "
                "between two prims, like a perfect range sensor; the camera readings show what a real robot would "
                "have to work from."),
            "steps": [
                _physics_first(),
                _edit("scene_create", {"path": "/World/Sense", "type_name": "Xform"}, "A Sense group appears.",
                      "Make a group for this lab."),
                *_floor("/World/Sense", [8, 8], 3),
                _edit("robot_add_ready", {"name": "jetbot", "prim_path": "/World/Sense/Jetbot",
                                          "position": [6, 8, 0]},
                      "A Jetbot appears on this floor, facing +X.", "Add a Jetbot."),
                _edit("scene_create", {"path": "/World/Sense/Obstacle", "type_name": "Cube"}, "A grey cube appears.",
                      "Make an obstacle."),
                _edit("scene_move", {"path": "/World/Sense/Obstacle", "translation": [8.5, 8, 0.15],
                                     "scale": [0.15, 0.15, 0.15]},
                      "A 30 cm box stands 2.5 m in front of the Jetbot.", "Put it in the robot's way."),
                _edit("scene_make_collider", {"prim_path": "/World/Sense/Obstacle"},
                      "Nothing visible: the obstacle is solid now.", "Make it solid."),
                _edit("sensor_create", {"kind": "rgb_camera", "parent_path": "/World/Sense/Jetbot/chassis",
                                        "name": "FrontCam", "offset": [0.08, 0, 0.06]},
                      "A camera prim FrontCam appears under chassis in the Stage panel, looking forward (+X).",
                      "Mount a camera on the robot's body, at the front."),
                _ui("Look through it: at the top-left of the viewport click the camera button (it says "
                    "Perspective) > Cameras > FrontCam. Afterwards switch back to Perspective the same way.",
                    "The robot's-eye view: the floor and the grey obstacle straight ahead."),
                _edit("sensor_create", {"kind": "imu", "parent_path": "/World/Sense/Jetbot/chassis", "name": "Imu"},
                      "An Imu prim appears under chassis (an IMU is invisible: it only measures).",
                      "Add an IMU to the body."),
                _read("sensor_list", {"root_path": "/World/Sense"},
                      "FrontCam (camera) and Imu, both carried by the chassis link."),
                _ui("Press Play (keep it playing for the next three readings).", "The Jetbot sits still."),
                _read("sensor_read", {"prim_path": "/World/Sense/Jetbot/chassis/FrontCam"},
                      "A summary of the picture (rgb size, mean colour) and of depth: its smallest value is roughly "
                      "the distance to the obstacle's front. The first call may say 'warming up': ask again.",
                      "Read the camera (a summary, never the whole picture)."),
                _read("sensor_read", {"prim_path": "/World/Sense/Jetbot/chassis/Imu"},
                      "linear_acceleration shows about 9.8 along Z: standing still, the IMU feels gravity.",
                      "Read the IMU."),
                _ui("Press Stop.", "Everything goes back to its start."),
                _edit("graph_apply", {"name": "obstacle_stop", "graph_path": "/World/Sense/StopGraph",
                                      "options": {"robot": "/World/Sense/Jetbot",
                                                  "sensor_prim": "/World/Sense/Jetbot/chassis",
                                                  "obstacle": "/World/Sense/Obstacle", "stop_distance": 0.5,
                                                  "speed": 0.3}},
                      "The preview shows the loop: every frame measure the distance (sense), compare it with 0.5 m "
                      "(decide), drive or stop the wheels (act).", "Build the sense -> decide -> act graph."),
                _read("graph_inspect", {"root_path": "/World/Sense/StopGraph"},
                      "The distance, the compare and the two wheel speeds, in plain words."),
                _ui("Press Play and watch.",
                    "The Jetbot drives toward the box and stops about half a metre before it, by itself."),
                _ui("Press Stop. In the viewport drag the obstacle's red arrow (X) to move it 1 m closer, press Play "
                    "again, then Stop.", "It still stops in time: the decision uses the measured distance, not a "
                                         "fixed place."),
            ],
            "try_yourself": "Add the other sensors from class and read them while playing: sensor_create "
                            "kind='depth_camera' (name FrontDepth) and kind='lidar' with params={'config': "
                            "'Example_Rotary'} on /World/Sense/Jetbot/chassis. (The laptop GPU has 6 GB: delete "
                            "sensors you no longer need.) Which one would you trust in fog, and why?",
        },
        "warehouse_setup": {
            "title": "Class session 11: smart warehouse setup (a box arrives, the robot picks it, drops it off)",
            "objectives": [],
            "class_session": 11,
            "words": {
                "conveyor": "a moving belt that carries boxes, like a supermarket checkout belt",
                "kinematic": "a rigid body that physics does not push around but that can push others: the belt "
                             "stays put but moves what lies on it",
                "pickup zone": "where the robot takes a box from (here: the end of the belt)",
                "drop zone": "where the robot puts it down",
                "curb": "a low edge (2 cm) at the belt's end that stops boxes but lets the robot lift them out",
                "reach": "how far a robot arm can stretch; a Franka reaches about 85 cm",
                "end effector / gripper": "the 'hand' at the end of the arm; the Franka's has two fingers",
                "workflow": "the chain of steps from start to finish: box arrives -> robot picks -> robot places",
            },
            "concept": (
                "Session 11 joins everything so far into one scene that works end to end, not perfectly. The "
                "layout copies the one that worked live on this laptop: an UNTURNED Franka arm (facing +X), the "
                "pickup spot 0.6 m straight in front of it, the drop zone about 45 degrees to its side. The belt "
                "is not turned either: it carries boxes toward the arm because its conveyor node's direction is "
                "(-1, 0, 0). A low 2 cm curb stops the box at the pickup spot. During the pick the belt is "
                "stopped (speed 0), and the arm moves slowly (pace 0.3), otherwise the box slips or is flung."),
            "steps": [
                _physics_first(),
                _edit("scene_create", {"path": "/World/Warehouse", "type_name": "Xform"},
                      "A Warehouse group appears.", "Make a group for the warehouse."),
                _edit("scene_create", {"path": "/World/Warehouse/Belt", "type_name": "Cube"}, "A grey cube.",
                      "Make the belt (a cube we will stretch)."),
                _edit("scene_move", {"path": "/World/Warehouse/Belt", "translation": [16, 0, 0.05],
                                     "scale": [1.2, 0.2, 0.05]},
                      "A long flat belt: 2.4 m long, 40 cm wide, 10 cm high.", "Stretch it into a belt."),
                _edit("scene_make_rigid_body", {"prim_path": "/World/Warehouse/Belt", "kinematic": True},
                      "Nothing visible: the belt is a KINEMATIC rigid body with a collider: it never falls but "
                      "can carry things.", "Make the belt kinematic."),
                _edit("graph_apply", {"name": "conveyor", "graph_path": "/World/Warehouse/BeltGraph",
                                      "options": {"belt": "/World/Warehouse/Belt", "velocity": 0.2,
                                                  "direction": [-1, 0, 0]}},
                      "A BeltGraph appears: a Conveyor Belt node at 0.2 m/s toward -X, with a Velocity variable.",
                      "Make it a conveyor. Direction (-1, 0, 0) reverses it; turning the belt would NOT (seen "
                      "live: the node ignores the belt's rotation)."),
                _edit("scene_create", {"path": "/World/Warehouse/Curb", "type_name": "Cube"}, "A grey cube.",
                      "Make the curb."),
                _edit("scene_move", {"path": "/World/Warehouse/Curb", "translation": [15.05, 0, 0.11],
                                     "scale": [0.025, 0.2, 0.01]},
                      "A thin 2 cm edge across the belt near its -X end.", "Shape and place it."),
                _edit("scene_make_collider", {"prim_path": "/World/Warehouse/Curb"},
                      "Nothing visible: the curb is solid. (A tall wall was tried live: the box rubbed on it and "
                      "slipped out of the fingers.)", "Make it solid."),
                _edit("scene_create", {"path": "/World/Warehouse/DropZone", "type_name": "Cube"}, "A grey cube.",
                      "Make the drop zone."),
                _edit("scene_move", {"path": "/World/Warehouse/DropZone", "translation": [15.05, 0.45, 0.025],
                                     "scale": [0.15, 0.15, 0.025]},
                      "A 30 cm square pad beside the belt's end.", "Shape and place it."),
                _edit("scene_make_collider", {"prim_path": "/World/Warehouse/DropZone"},
                      "Nothing visible: boxes can rest on it.", "Make it solid."),
                _edit("scene_set_attribute", {"path": "/World/Warehouse/DropZone", "name": "primvars:displayColor",
                                              "value": [[0.85, 0.15, 0.12]]},
                      "The pad turns red.", "Colour it so you can see it."),
                _edit("scene_spawn_packages", {"parent": "/World/Warehouse/Packages", "count": 1,
                                               "region_min": [16.6, -0.12, 0.25], "region_max": [17.1, 0.12, 0.35],
                                               "size_range": [0.05, 0.06], "colors": ["red"], "seed": 3},
                      "One small red box (5-6 cm, small enough for the gripper) floats above the far end of the "
                      "belt.", "Add the box that will arrive."),
                _edit("robot_add_ready", {"name": "franka", "prim_path": "/World/Warehouse/Franka",
                                          "position": [14.55, 0, 0]},
                      "A Franka arm stands past the belt's end, facing along the belt (+X). The curb is 0.6 m in "
                      "front of it.", "Add the robot arm, not turned."),
                _read("scene_children", {"path": "/World/Warehouse"},
                      "Belt, BeltGraph, Curb, DropZone, Packages and Franka: the whole warehouse."),
                _ui("Press Play. Wait: the first Play after building can take a moment. Watch the box.",
                    "The box drops onto the belt and rides toward the arm until the curb stops it."),
                _ui("Keep playing. Stop the belt: right-click /World/Warehouse/BeltGraph in the Stage panel > Open "
                    "Graph; in the graph window's Variables panel set Velocity to 0.",
                    "The belt stops. The box rests still against the curb, ready to be picked."),
                _edit("robot_live_pick", {"robot_root": "/World/Warehouse/Franka",
                                          "object_path": "/World/Warehouse/Packages/Package_001",
                                          "place_position": [15.05, 0.45, 0.1], "pace": 0.3},
                      "The arm opens its gripper, reaches down, closes on the box, lifts it, swings left and sets "
                      "it on the red pad (about half a minute).",
                      "Pick the box and drop it off, slowly (pace 0.3)."),
                _read("robot_live_pick_status", {},
                      "result done and placed true: the box is within 10 cm of the drop spot. If the fingers press "
                      "into the belt, the pick can be retried with grip_height=0.01.",
                      "Check where the box ended up."),
                _ui("Press Stop. Set Velocity back to 0.2 in the graph's Variables panel.",
                    "Everything returns to its start: the box is back above the belt."),
            ],
            "try_yourself": "Between-session task: rebuild the warehouse yourself at another spot (e.g. y = -3) "
                            "from memory, then say why the arm is not turned and why the curb is only 2 cm.",
        },
        "warehouse_debug": {
            "title": "Class session 12: warehouse automation and debugging (spawn boxes, find what is broken)",
            "objectives": [],
            "class_session": 12,
            "words": {
                "automation": "the scene does the job by itself: a key or a timer makes new boxes, no hand placing",
                "debugging": "finding WHY something does not work: look at the symptom, guess a cause, test it",
                "symptom": "what you see going wrong ('the box falls through the floor')",
                "cause": "the real reason behind it ('the box has no collider')",
                "collider": "the invisible solid shape physics uses to know where a thing is",
                "diagnose": "read the scene and list likely causes, like a doctor's check-up",
                "wrong prim path": "a path with a typo: the graph or joint points at something that does not exist",
            },
            "concept": (
                "Hour 1: the warehouse from session 11 gets automation: a Script Node spawns a new box each time "
                "you press P, so boxes keep arriving (Python and Action Graph together). We shrink the spawned "
                "box from 20 cm to 5 cm by changing the node's code; changing a Script Node's code needs a reset, "
                "and graph_set_value does it. Hour 2: debugging. Every bug has a symptom you see and a cause you "
                "must find. The usual ones in class: no physics scene, no collider, kinematic by mistake, a wrong "
                "prim path, an Action Graph that is never triggered. We plant one bug ourselves, diagnose it, fix "
                "it, then solve a broken practice scene without opening it."),
            "steps": [
                _read("scene_children", {"path": "/World/Warehouse"},
                      "Belt, BeltGraph, Curb, DropZone, Packages and Franka. If it says no such prim, do the "
                      "warehouse_setup lab first.", "Check that the session 11 warehouse is here."),
                _edit("graph_create", {"graph_path": "/World/Warehouse/SpawnGraph"},
                      "A SpawnGraph prim appears.", "Make a graph for the box spawner."),
                _edit("graph_add_script_node", {"graph_path": "/World/Warehouse/SpawnGraph", "name": "Spawner",
                                                "template": "spawn_package",
                                                "values": {"inputs:parent": "/World/Warehouse/Spawned",
                                                           "inputs:spawn_at": [17.0, 0.0, 0.3]},
                                                "trigger_key": "P"},
                      "A Script Node Spawner and an On Keyboard Input (P). Its code makes a random-coloured box "
                      "with physics at spawn_at, the far end of the belt.", "Add the spawner, fired by P."),
                _edit("graph_set_value", {"graph_path": "/World/Warehouse/SpawnGraph", "pin": "Spawner.inputs:script",
                                          "value": small_spawner},
                      "The only change in the code: CreateSizeAttr(0.2) -> CreateSizeAttr(0.05). The result says "
                      "the Script Node was reset, so the new code is used.",
                      "Change the node's code so boxes are 5 cm (the gripper opens about 8 cm)."),
                _ui("Press Play, click the viewport, press P once. Wait until the box reaches the curb (about 10 s), "
                    "then press P again. Press Stop at the end.",
                    "Small boxes appear at the far end and ride to the curb, one after another. Boxes made while "
                    "playing may stay after Stop; delete /World/Warehouse/Spawned to clean up."),
                _ui("Only read this one: why one box at a time? Seen live: boxes pile up at the curb about 3 cm "
                    "apart, and the gripper's fingers land on the neighbours.",
                    "Say back how you would space the boxes out (hint: a timer instead of a key)."),
                _edit("scene_create", {"path": "/World/Debug", "type_name": "Xform"}, "A Debug group appears.",
                      "Now debugging. Make a group for the bug hunt."),
                *_floor("/World/Debug", [8, 16], 1.5),
                _edit("scene_create", {"path": "/World/Debug/Box", "type_name": "Cube"}, "A grey cube appears.",
                      "Make a box."),
                _edit("scene_move", {"path": "/World/Debug/Box", "translation": [8, 16, 1], "scale": [0.1, 0.1, 0.1]},
                      "A 20 cm box floats 1 m above the floor.", "Make it small and lift it."),
                _edit("scene_make_rigid_body", {"prim_path": "/World/Debug/Box", "add_collider": False},
                      "Nothing visible yet. On purpose, like the teacher breaking a scene: physics but NO collider.",
                      "Plant a bug: give the box physics without a collider."),
                _ui("Press Play and watch the box, then press Stop.",
                    "Symptom: the box falls straight through the floor. Before the next step, say what you think "
                    "the cause is."),
                _read("scene_diagnose", {"focus": "physics"},
                      "A finding on /World/Debug/Box: 'A rigid body with no collider has no solid shape', with the "
                      "fix: scene_make_collider.", "Ask for a physics check-up."),
                _edit("scene_make_collider", {"prim_path": "/World/Debug/Box"},
                      "Nothing visible: the box has a solid shape now.", "Fix the cause, not the symptom."),
                _ui("Press Play, then Stop.", "The box lands on the floor and stays there: fixed."),
                _read("scene_diagnose", {"focus": "graph"},
                      "Graph findings, if any: nodes nothing triggers, or inputs pointing at prims that do not "
                      "exist (a wrong prim path). No findings means every graph is wired.",
                      "Check all Action Graphs for the 'graph not triggering' and 'wrong path' bugs."),
                _read("debug_challenge_list", {},
                      "Seven broken practice scenes, each with only its symptom (level 1-3).",
                      "See the bug-hunt challenges."),
                _edit("debug_challenge_make", {"challenge_id": "frozen_box"},
                      f"A new file {challenge_file}: a scene where the box hangs in the air although physics and "
                      "colliders are there. It is written next to the study stage; the study stage itself does "
                      "not change.", "Make one broken practice scene as a new file."),
                _read("debug_challenge_answer", {"challenge_id": "frozen_box", "hint": True},
                      "Two hints, never the answer.", "Ask for hints first."),
                _ui(f"Open {challenge_file} in Notepad (right-click it in File Explorer > Open with > Notepad): "
                    "a .usda file is readable text. Find the Box and read its physics lines.",
                    "Look for a line that would stop physics from moving the box."),
                _read("debug_challenge_answer", {"challenge_id": "frozen_box",
                                                 "answer": "<your one-sentence explanation>"},
                      "correct true with the cause and fix, or 'not yet' and another hint.",
                      "Answer in your own words: replace the answer text with your explanation."),
                _read("scene_diagnose", {"scene_file": challenge_file, "focus": "physics"},
                      "The check-up of the saved file names the same cause: compare it with your answer.",
                      "Confirm with a check-up of the file, without opening it in Isaac."),
            ],
            "try_yourself": "Make box_wont_fall or exploding_boxes with debug_challenge_make and solve it the same "
                            "way: symptom -> your guess -> hint -> answer -> scene_diagnose(scene_file=...).",
        },
        "package_sorting": {
            "title": "Class session 13: package sorting (a camera sees the colour, the graph picks the bin)",
            "objectives": [],
            "class_session": 13,
            "words": {
                "perception": "turning a picture into a fact: 'this package is red'",
                "classification": "putting each thing into one group (red or blue), like sorting laundry",
                "decision": "choosing what to do from the fact: red -> Bin A",
                "detection zone": "the box-shaped area where the camera checks packages",
                "pixel": "one dot of a picture, with a red, green and blue value",
                "sRGB / linear": "two ways to store colour: camera pixels are sRGB (brightened for screens), "
                                 "physics-style colour data is linear; you must convert before comparing",
                "synthetic data (SDG)": "training pictures made by the simulator, with the right answers "
                                        "(labels) included for free",
                "semantic label": "a name tag on a prim ('red') that SDG writes into its answer pictures",
                "domain randomization": "changing positions and light every picture, so a model learns the "
                                        "object and not the background",
            },
            "concept": (
                "Package -> camera -> colour detection -> red to Bin A, blue to Bin B. A camera looks down on the "
                "detection zone. For each package in the zone, the sorter finds where the package is in the "
                "picture, reads a small patch of pixels, converts them from sRGB to linear (camera pixels are "
                "brightened for screens; seen live, without this blue looked like purple), ignores grey floor "
                "pixels and picks the nearest colour. Then it decides the bin and pushes the package there. Part 2 "
                "makes a small labelled dataset with the simulator and learns the colours from it."),
            "steps": [
                _physics_first(),
                _edit("scene_create", {"path": "/World/Sort", "type_name": "Xform"}, "A Sort group appears.",
                      "Make a group for this lab."),
                *_floor("/World/Sort", [17, 10], 2.5),
                _edit("scene_create", {"path": "/World/Sort/BinA", "type_name": "Cube"}, "A grey cube.",
                      "Make Bin A (a flat pad)."),
                _edit("scene_move", {"path": "/World/Sort/BinA", "translation": [18.5, 11.5, 0.005],
                                     "scale": [0.25, 0.25, 0.005]}, "A thin 50 cm pad on the floor.",
                      "Flatten and place it."),
                _edit("scene_create", {"path": "/World/Sort/BinB", "type_name": "Cube"}, "A grey cube.",
                      "Make Bin B."),
                _edit("scene_move", {"path": "/World/Sort/BinB", "translation": [18.5, 8.5, 0.005],
                                     "scale": [0.25, 0.25, 0.005]}, "A second pad, 3 m from the first.",
                      "Flatten and place it."),
                _edit("scene_create_light", {"path": "/World/Sort/Light", "light_type": "dome", "intensity": 1000},
                      "The scene gets even light from all around, so the camera sees true colours.",
                      "Add a dome light (we also vary its brightness in part 2)."),
                _edit("scene_spawn_packages", {"parent": "/World/Sort/Packages", "count": 6,
                                               "region_min": [15.1, 9.6, 0.3], "region_max": [15.9, 10.4, 0.6],
                                               "size_range": [0.12, 0.18], "colors": ["red", "blue"], "seed": 4},
                      "Six packages, three red and three blue, inside the detection zone.",
                      "Add the packages (seed 4 always gives the same three red and three blue)."),
                _edit("sensor_create", {"kind": "rgb_camera", "parent_path": "/World/Sort", "name": "SortCam",
                                        "offset": [15.5, 10, 5], "rotation": [0, 0, 0]},
                      "A camera 5 m above the zone. Rotation (0, 0, 0) keeps a camera's natural direction: "
                      "straight down.", "Mount the sorting camera."),
                _ui("Look through it: viewport camera button (top-left, says Perspective) > Cameras > SortCam. "
                    "Then switch back to Perspective.",
                    "From above: the six coloured packages in the middle of the picture."),
                _edit("graph_apply", {"name": "color_sort", "graph_path": "/World/Sort/SortGraph",
                                      "options": {"packages": "/World/Sort/Packages",
                                                  "bins": {"red": "/World/Sort/BinA", "blue": "/World/Sort/BinB"},
                                                  "zone_min": [14.8, 9.3, 0.0], "zone_max": [16.2, 10.7, 1.0],
                                                  "camera": "/World/Sort/SortCam"}},
                      "An On Playback Tick and a Script Node Sorter with pins packages, routes, zone, camera; "
                      "routes says red -> BinA, blue -> BinB.",
                      "Build the sorter: sense (camera), decide (routes), act (push)."),
                _read("graph_inspect", {"root_path": "/World/Sort/SortGraph"},
                      "Every frame while playing -> Sorter, with its inputs in plain words."),
                _ui("Press Play and watch for 10 seconds (the camera needs a moment to warm up).",
                    "Each package hops toward a pad: red ones to Bin A, blue ones to Bin B."),
                _ui("Open the graph (right-click SortGraph > Open Graph), click Sorter and read outputs:sorted and "
                    "outputs:last_seen in the Property panel. Then press Stop.",
                    "sorted = 6 and last_seen like 'Package_004: blue -> /World/Sort/BinB'. Stop puts them back."),
                _ui("Part 2 needs a fresh Isaac Sim: save (File > Save), close Isaac Sim, start it again and open "
                    "study.usda. Why: the laptop GPU has 6 GB, so run one heavy dataset job per fresh session, and "
                    "the sorter's camera reader can block the dataset writer. Do not press Play before part 2.",
                    "Isaac is open again on study.usda; the Sort group is still there."),
                _edit("semantic_labels", {"mapping": {f"/World/Sort/Packages/{name}": colour
                                                      for name, colour in SORT_PACKAGE_COLOURS.items()}},
                      "Nothing visible: each package gets a name tag, red or blue.",
                      "Label each package with its class (its colour)."),
                _read("sdg_plan", sdg_args,
                      f"The plan: 12 pictures of 320 x 240 from SortCam with colour + label images, written to "
                      f"{dataset}; every picture moves the packages and changes the light.",
                      "Plan a small synthetic dataset (nothing is written)."),
                _edit("sdg_start", sdg_args,
                      "A job id. The packages jump around for a few seconds while pictures are taken, then return "
                      "to their places.", "Make the dataset."),
                _read("dataset_check", {"directory": dataset, "expected_frames": 12,
                                        "required_modalities": ["rgb", "semantic_segmentation"]},
                      "12 frames, each with an rgb picture and a label picture, nothing broken.",
                      "Check the dataset."),
                _edit("sdg_train_colour_model", {"dataset_dir": dataset},
                      "One learned colour per class (red, blue) and how well it did on pictures it did not learn "
                      "from. Applying saves colour_model.json in the dataset folder.",
                      "Learn the colours from the labelled pictures."),
            ],
            "try_yourself": f"Use what the model learned: delete SortGraph, then rebuild it with the same options "
                            f"plus 'palette': '{dataset}/colour_model.json'. Does it still sort all six? Between "
                            "sessions: build a sorter for at least two package types of your own.",
        },
        "factory_cell": {
            "title": "Class session 14: factory cell (conveyor + sensor + robot arm pick and place)",
            "objectives": [],
            "class_session": 14,
            "words": {
                "factory cell": "one work station: a belt brings parts, a robot handles them, bins collect them",
                "link / joint": "the arm's solid parts and the hinges between them (session 9)",
                "end effector": "the tool at the arm's tip; here the gripper",
                "gripper": "a two-finger hand that closes on an object",
                "pick position / place position": "where the arm takes the box / where it puts it",
                "digital twin": "a virtual copy of a real machine that follows the real machine's data",
                "data link": "a connection that feeds numbers from a file into the scene while it plays",
                "pace": "how fast the arm moves from phase to phase; lower is slower and gentler",
            },
            "concept": (
                "Session 14 combines physics, a sensor, Action Graph, Python and robot control in one cell. "
                "scene_build_factory_cell builds it in one previewable step with the layout that worked live: an "
                "unturned Franka, the pick spot 0.6 m straight ahead, bins about 45 degrees to each side, the belt "
                "reversed through its conveyor node's direction (not by turning it), a 2 cm curb. Package -> "
                "conveyor -> sensor -> arm -> pick -> move -> place. New today: the belt's speed comes from a data "
                "file, like a real factory's control system feeding its digital twin; setting it to 0 stops the "
                "belt for the pick."),
            "steps": [
                _physics_first(),
                _edit("scene_build_factory_cell", {"root": "/World/Cell", "origin": [8, 24, 0],
                                                   "colours": ["red", "blue"], "packages": 1},
                      "The preview lists every part and where it goes. After applying: a belt, a curb, a red and a "
                      "blue bin, a top camera, one package (red, from seed 3) and a Franka.",
                      "Build the whole cell in one step."),
                _read("scene_children", {"path": "/World/Cell"},
                      "Belt, Stopper (the curb), Bin_red, Bin_blue, TopCam, Packages, Franka, BeltGraph."),
                _read("robot_joint_table", {"robot_root": "/World/Cell/Franka"},
                      "Seven arm joints (panda_joint1-7) and two finger joints: the links, joints and gripper of "
                      "an arm.", "Look at the arm's joints."),
                _read("graph_inspect", {"root_path": "/World/Cell/BeltGraph"},
                      "A Conveyor Belt node with direction (-1, 0, 0) and a Velocity variable of 0.2 m/s.",
                      "See how the belt is driven."),
                _ui(f"Make the data file: open Notepad (Start menu, type Notepad), type {{\"speed\": 0.2}} and use "
                    f"File > Save As: {belt_json} with 'Save as type: All files' (so it does not end in .txt).",
                    "A small file belt_speed.json in the isaac_study folder."),
                _read("data_link_preview", {"source": belt_json},
                      "A JSON live feed with one column, speed = 0.2.", "Check what the data file holds."),
                _ui("Press Play. The first Play after building may take a moment.",
                    "The package drops onto the belt and rides toward the arm."),
                _edit("data_link_start", {"source": belt_json,
                                          "mappings": [{"column": "speed",
                                                        "target": "/World/Cell/BeltGraph.graph:variable:Velocity"}],
                                          "period": 0.5},
                      "The link runs: every half second it reads the file and writes speed into the belt's "
                      "Velocity. Nothing changes yet (still 0.2).", "Connect the file to the belt."),
                _ui("When the package rests at the curb: in Notepad change 0.2 to 0 and save (Ctrl+S).",
                    "Within a second the belt stops: the scene followed the data, like a digital twin."),
                _read("data_link_status", {}, "running, the number of updates and Velocity = 0.",
                      "Check the link."),
                _read("sensor_read", {"prim_path": "/World/Cell/TopCam"},
                      "A summary of the top camera's picture; its smallest depth is about the distance down to the "
                      "package's top. The first call may say 'warming up': ask again.",
                      "The sensor step: look at the pick spot from above."),
                _edit("robot_live_pick", {"robot_root": "/World/Cell/Franka",
                                          "object_path": "/World/Cell/Packages/Package_001",
                                          "place_position": [7.05, 24.45, 0.1], "pace": 0.3},
                      "The arm opens, reaches, grips, lifts the package off the curb, swings and sets it on the "
                      "red bin.", "Pick the red package and place it on the red bin, slowly (pace 0.3)."),
                _read("robot_live_pick_status", {},
                      "result done and placed true (within 10 cm of the bin's centre). Live on this laptop the "
                      "package landed 6.9 cm from the centre: a success.", "Check the result."),
                _read("data_link_stop", {}, "The link stops and puts the belt's Velocity back.",
                      "Disconnect the data file."),
                _ui("Press Stop.", "Everything returns to its start."),
                _read("homework_check", {"session": 14},
                      "Each part graded pass, partial or fail with evidence: arm, gripper, conveyor, a graspable "
                      "package in reach, a place target, a sensor, control logic and physics.",
                      "Grade the cell like the class homework."),
            ],
            "try_yourself": "Record the pick and replay it: robot_record_start(robot_root='/World/Cell/Franka', "
                            "seconds=60) while playing, run the pick, robot_record_stop(name='my_pick'), then press "
                            "Stop and Play and robot_replay_motion(file='my_pick'). Why does the replay not need "
                            "the package's position?",
        },
        "capstone_challenge": {
            "title": "Class session 15: final capstone (explain your mini smart warehouse, then a live challenge)",
            "objectives": [],
            "class_session": 15,
            "words": {
                "capstone": "the final project that uses everything you learned",
                "integration": "joining separate parts so they work as one system",
                "pipeline": "the chain package -> sensor -> perception -> decision -> robot control -> pick/move "
                            "-> sort/place",
                "live challenge": "a small timed change to your own scene, to show you understand it rather than "
                                  "copied it",
                "routes": "the sorter's table that says which colour goes to which bin",
            },
            "concept": (
                "In the capstone you explain your own simulation: the USD hierarchy (which group holds what), the "
                "physics setup, the robots, the sensors, the Action Graphs, the Python, the perception and "
                "decision logic, and what happens when something goes wrong. Then the teacher asks for a live "
                "change, e.g. 'make red packages go to Bin B' or 'make the robot stop earlier'. If you know where "
                "each decision lives (a graph variable, a node input, a prim's attribute), every change is one "
                "small edit. This lab uses the groups from the session 9-14 labs."),
            "steps": [
                _read("scene_children", {"path": "/World"},
                      "Your groups: Drive, Sense, Warehouse, Debug, Sort, Cell (and the earlier labs).",
                      "Start the demonstration with the USD hierarchy."),
                _ui("Explain the pipeline out loud using your scene: which prim is the PACKAGE, the SENSOR, where "
                    "PERCEPTION and DECISION happen (Sorter, StopGraph), what does ROBOT CONTROL (Jetbot graphs, "
                    "robot_live_pick), PICK/MOVE and SORT/PLACE.",
                    "One sentence per box of the pipeline, each naming a real prim path."),
                _read("graph_inspect", {},
                      "Every Action Graph in the scene explained in plain words: check it matches what you said.",
                      "Compare your explanation with what the graphs really do."),
                _read("scene_diagnose", {},
                      "A list of possible problems (or none), each with symptom, cause and fix: this is 'what "
                      "happens when something goes wrong'.", "The debugging part of the demonstration."),
                _read("homework_check", {"session": 14},
                      "The factory cell graded part by part.", "Grade the factory cell once more."),
                _edit("graph_set_value", {"graph_path": "/World/Sort/SortGraph", "pin": "Sorter.inputs:routes",
                                          "value": "{\"red\": \"/World/Sort/BinB\", \"blue\": \"/World/Sort/BinA\"}"},
                      "The Sorter's routes input now reads red -> BinB, blue -> BinA.",
                      "The class's example challenge: swap the bins. The decision lives in one input."),
                _ui("Press Play and watch for 10 seconds, then press Stop.",
                    "Red packages now go to Bin B and blue ones to Bin A: one input changed the whole behaviour."),
                _read("live_challenge_start", {"level": "easy"},
                      "A task that fits your scene (e.g. change the belt speed, the stop distance or the route), "
                      "the time allowed and its id.", "Get a live challenge."),
                _ui("Do the task yourself in Isaac Sim (Property panel or the graph's Variables panel), within the "
                    "time allowed.", "Your change, made by hand."),
                _read("live_challenge_check", {"id": "<the id from live_challenge_start>"},
                      "done or not yet, with what it read in the scene. Not yet? Call again with hint=True.",
                      "Check it: replace id with the id you were given."),
            ],
            "try_yourself": "Ask for live_challenge_start(level='medium'), solve it without hints, then explain in "
                            "three sentences what you changed and why it worked. Finally put the routes back "
                            "(red -> BinA, blue -> BinB).",
        },
    }


def _labs() -> dict[str, dict[str, Any]]:
    props = f"{_assets()}/Props/Blocks"
    franka = f"{_assets()}/Robots/FrankaRobotics/FrankaPanda/franka.usd"
    return {
        "basics_isaac": {
            "title": "Lesson 0a: finding your way around Isaac Sim",
            "objectives": [],
            "words": {
                "viewport": "the big 3D window in the middle where you see the scene, like a camera you can move",
                "Stage panel": "the list (top right) of everything in the scene, shown as a tree of folders",
                "Property panel": "the panel (bottom right) showing the settings of whatever you selected",
                "select": "click a thing (in the viewport or the Stage panel) so the app knows you mean it",
                "Play / Stop": "the buttons on the toolbar at the left edge of the viewport that start and reset physics",
            },
            "concept": (
                "Isaac Sim is a program for building 3D worlds where robots can be tested before they exist in real "
                "life. Its screen has three areas you will use every day: the viewport in the middle (your camera "
                "into the world), the Stage panel at the top right (the list of everything in the world) and the "
                "Property panel at the bottom right (the settings of the thing you clicked). You change what you "
                "see in the viewport by moving the camera, not the objects."),
            "steps": [
                _ui("Look at the middle of the screen: that is the viewport. You should see a grey grid (the ground).",
                    "A grid and probably nothing else: a new scene is empty."),
                _ui("Move the camera: hold Alt and drag with the LEFT mouse button to orbit around; drag with the "
                    "MIDDLE button (press the wheel) to slide sideways; roll the wheel to zoom in and out.",
                    "The grid turns, slides and gets bigger or smaller. The world did not move, your camera did."),
                _ui("Find the Stage panel at the top right. Click the small arrow next to World to open it.",
                    "A tree: World and whatever is inside it (maybe a ground or a light)."),
                _ui("Find the Property panel at the bottom right. Click World in the Stage panel.",
                    "The Property panel fills with World's settings (Transform: Translate, Rotate, Scale)."),
                _read("scene_summary", {},
                      "I read the same scene from my side: stage_name should end with isaac_study/study.usda.",
                      "Check that Isaac and I are looking at the same file."),
                _read("scene_children", {"path": "/World"},
                      "The same children you saw under World in the Stage panel.",
                      "Ask the scene what is inside World."),
            ],
            "try_yourself": "Without help: orbit the camera all the way round, then select World and press F. "
                            "What does F do?",
        },
        "basics_usd": {
            "title": "Lesson 0b: what a USD scene is made of (stage, prim, path, attribute)",
            "objectives": [],
            "words": {
                "USD": "Universal Scene Description: the file format and language Isaac Sim uses to describe a 3D "
                       "world, like a recipe for the whole scene",
                "stage": "the whole scene you have open right now (everything in the Stage panel)",
                "prim": "one thing in the scene: a cube, a light, a robot part, or a folder (Xform) holding others",
                "path": "a prim's address, like a folder path: /World/Basics/MyCube",
                "attribute": "one setting of a prim with a value, e.g. a cube's size = 2",
                "Xform": "a prim that is just a container you can move; like a folder that also has a position",
                "layer / .usda file": "the file the stage is saved in; .usda is readable text you can open in Notepad",
            },
            "concept": (
                "A USD scene (the stage) is a tree of prims, like folders and files on your computer. Every prim has "
                "an address called a path, starting at / (for example /World/Basics/MyCube). Each prim has "
                "attributes: named settings with values, like size = 2 or a position. Everything you see in Isaac "
                "Sim is just prims and attribute values; when you change a value, the scene changes. When you save, "
                "all of it is written into a .usda text file you can read."),
            "steps": [
                _edit("scene_create", {"path": "/World/Basics", "type_name": "Xform"},
                      "A new folder-like prim Basics appears under World in the Stage panel.",
                      "Make a container prim (an Xform)."),
                _edit("scene_create", {"path": "/World/Basics/MyCube", "type_name": "Cube"},
                      "A grey cube appears in the viewport, and MyCube appears inside Basics in the Stage panel.",
                      "Make a cube inside it. Its path is /World/Basics/MyCube."),
                _read("scene_select", {"paths": ["/World/Basics/MyCube"]},
                      "MyCube is highlighted in the Stage panel, and the Property panel shows its settings.",
                      "Select it, so Isaac shows its attributes (selecting changes nothing in the scene)."),
                _read("scene_properties", {"prim_path": "/World/Basics/MyCube"},
                      "The cube's attributes: size is 2 (the default, nobody wrote it yet), visibility, and more.",
                      "List the cube's attributes from my side."),
                _edit("scene_set_attribute", {"path": "/World/Basics/MyCube", "name": "size", "value": 0.5},
                      "The cube shrinks to a quarter of its size. In the Property panel, Size now reads 0.5.",
                      "Change one attribute: size."),
                _edit("scene_move", {"path": "/World/Basics/MyCube", "translation": [0, 0, 1]},
                      "The cube lifts 1 m up. In the Property panel, Translate Z reads 1.",
                      "Change its position (the translate attribute)."),
                _ui("Now you: in the Property panel, find Translate and type 2 into the X box, then press Enter.",
                    "The cube jumps 2 m sideways. You just changed an attribute by hand, exactly like I did."),
                _read("scene_inspect", {"path": "/World/Basics/MyCube"},
                      "My read now shows translation x = 2: we both see the same stage."),
            ],
            "try_yourself": "Say MyCube's path out loud, then name two of its attributes and their values.",
        },
        "action_graph": {
            "title": "Class session 6: Action Graph from scratch (press W to push a cube)",
            "objectives": [],
            "class_session": 6,
            "words": {
                "Action Graph": "a picture-program: boxes joined by wires that say 'when this happens, do that', "
                                "instead of typed code",
                "node": "one box in the graph that does one job (wait for a key, write a value, add two numbers)",
                "pin": "a little dot on a node's side: inputs on the left (what it needs), outputs on the right "
                       "(what it gives)",
                "execution wire": "a white wire that says WHEN the next node runs, like a domino knocking the next",
                "data wire": "a coloured wire that carries a VALUE (a number, a vector) from one node to another",
                "event node": "a node that starts a chain when something happens: a key press, every frame (tick)",
                "graph variable": "a named value stored in the graph that any node can read, and that you can "
                                  "change while it runs, like a dial on a machine",
                "velocity": "speed with a direction, e.g. (2, 0, 0) = 2 metres per second along X",
            },
            "concept": (
                "In class the teacher made a cube move with the W key without writing code. An Action Graph is a set "
                "of nodes joined by wires. Execution (white) wires decide WHEN something happens: 'when W is "
                "pressed -> write a value'. Data wires carry WHAT the value is: 'the value comes from the variable "
                "forward'. The cube needs physics (a rigid body) so that writing its velocity makes it move, and "
                "something to land on (a floor with a collider)."),
            "steps": [
                _edit("scene_add_physics_scene", {},
                      "Nothing visible: the scene now has gravity and physics. (Skip if it says one exists.)",
                      "Turn physics on for the scene."),
                _edit("scene_create", {"path": "/World/AG", "type_name": "Xform"}, "A group AG appears.",
                      "Make a group for this lab."),
                _edit("scene_create", {"path": "/World/AG/Floor", "type_name": "Cube"}, "A big grey cube.",
                      "Make a floor (a flattened cube)."),
                _edit("scene_move", {"path": "/World/AG/Floor", "translation": [0, -4, -0.05],
                                     "scale": [3, 3, 0.05]},
                      "It flattens into a 6 x 6 m slab just under the ground line.", "Flatten it."),
                _edit("scene_make_collider", {"prim_path": "/World/AG/Floor"},
                      "Nothing visible: things can now land on the floor.", "Give the floor a collider."),
                _edit("scene_create", {"path": "/World/AG/Cube", "type_name": "Cube"}, "A cube appears.",
                      "Make the cube we will push."),
                _edit("scene_move", {"path": "/World/AG/Cube", "translation": [0, -4, 0.5],
                                     "scale": [0.25, 0.25, 0.25]},
                      "A small 0.5 m cube floats above the floor.", "Make it small and lift it."),
                _edit("scene_make_rigid_body", {"prim_path": "/World/AG/Cube"},
                      "Nothing yet: press Play and it would fall onto the floor (then press Stop).",
                      "Give the cube physics (a rigid body with a collider)."),
                _edit("graph_create", {"graph_path": "/World/AG/Graph"},
                      "A Graph prim appears. Right-click it in the Stage panel > Open Graph to see the empty canvas.",
                      "Make an empty Action Graph."),
                _edit("graph_add_node", {"graph_path": "/World/AG/Graph", "name": "KeyW",
                                         "node_type": "omni.graph.action.OnKeyboardInput",
                                         "values": {"inputs:keyIn": "W"}},
                      "An 'On Keyboard Input' box appears on the canvas; its outputs include Pressed and Released.",
                      "Add the EVENT node: it fires when W is pressed."),
                _edit("graph_add_variable", {"graph_path": "/World/AG/Graph", "name": "forward",
                                             "value_type": "float[3]", "default": [2, 0, 0]},
                      "In the graph window's Variables panel, 'forward' appears with (2, 0, 0).",
                      "Add a graph VARIABLE: the push direction and speed."),
                _edit("graph_add_node", {"graph_path": "/World/AG/Graph", "name": "ReadForward",
                                         "node_type": "omni.graph.core.ReadVariable",
                                         "values": {"inputs:variableName": "forward"}},
                      "A 'Read Variable' box appears; its Value output carries (2, 0, 0).",
                      "Add a node that READS the variable."),
                _edit("graph_add_node", {"graph_path": "/World/AG/Graph", "name": "Push",
                                         "node_type": "omni.graph.nodes.WritePrimAttribute",
                                         "values": {"inputs:name": "physics:velocity", "inputs:usdWriteBack": True}},
                      "A 'Write Prim Attribute' box appears. It will write the cube's velocity.",
                      "Add the node that DOES the job: write a value into an attribute."),
                _edit("graph_set_value", {"graph_path": "/World/AG/Graph", "pin": "Push.inputs:prim",
                                          "value": ["/World/AG/Cube"]},
                      "Push's Prim input now shows /World/AG/Cube.", "Tell it WHICH prim: the cube."),
                _edit("graph_connect", {"graph_path": "/World/AG/Graph", "source": "KeyW.outputs:pressed",
                                        "target": "Push.inputs:execIn"},
                      "A white EXECUTION wire joins Pressed to Push: 'when W is pressed, run Push'.",
                      "Wire WHEN: the key press starts the write."),
                _edit("graph_connect", {"graph_path": "/World/AG/Graph", "source": "ReadForward.outputs:value",
                                        "target": "Push.inputs:value"},
                      "A coloured DATA wire carries (2, 0, 0) into Push's Value.",
                      "Wire WHAT: the value to write comes from the variable."),
                _read("graph_inspect", {"root_path": "/World/AG/Graph"},
                      "The flow reads: 'When key W is pressed -> Write Prim Attribute writes physics:velocity = "
                      "variable forward on /World/AG/Cube'.", "Ask the graph to explain itself."),
                _ui("Press Play (left toolbar). Click once inside the viewport so it has the keyboard, then press W.",
                    "The cube slides along X at 2 m/s. (Press Stop to put it back.)"),
                _ui("In the graph window's Variables panel, change forward to (0, 2, 0). Press Play, click the "
                    "viewport, press W.",
                    "Now it slides along Y: you changed the behaviour without touching a node, as in class."),
            ],
            "try_yourself": "Add an S key that pushes backwards: an On Keyboard Input (S), a new variable back = "
                            "(-2, 0, 0), a Read Variable and a Write Prim Attribute. Which two wires do you need?",
        },
        **_session_labs(),
        "surprising_renders": {
            "title": "Why did it vanish? Visibility, purpose and stale extents",
            "objectives": ["5.5", "6.4", "5.6"],
            "words": {'visibility': 'a setting that hides or shows a prim (and everything inside it)', 'purpose': 'who a prim is for: default (everyone), render, proxy (a fast stand-in) or guide (helper drawings)', 'extent': 'the box a shape says it fits inside; programs use it to frame and skip things'},
            "concept": (
                "Sometimes a prim is in the scene but you can't see it, or it looks wrong. The usual reasons: "
                "visibility='invisible' hides it (and everything under it); purpose='guide' or 'proxy' means 'only "
                "show this for a special view', so normal rendering skips it; the extent (the box USD thinks the "
                "shape fits in) can be stale after the shape changes, so culling and framing go wrong; a "
                "leftHanded orientation flips which side faces you; and unit/up-axis mismatches make things tiny "
                "or sideways. Think of extent as the label on a moving box: if you put a bigger thing inside and "
                "keep the old label, the movers plan with the wrong size."),
            "steps": [
                _edit("scene_create", {"path": "/World/Render", "type_name": "Xform"},
                      "A new empty group Render appears in the Stage panel.", "Make a group for this lab."),
                _edit("scene_create", {"path": "/World/Render/Box", "type_name": "Cube"},
                      "A grey 2 m cube appears at the origin (Cube's fallback size is 2).", "Add a cube."),
                _read("scene_bounds", {"prim_path": "/World/Render/Box"},
                      "authored extent [-1,-1,-1]..[1,1,1] matches the computed one: the label fits the box."),
                _edit("scene_set_attribute", {"path": "/World/Render/Box", "name": "visibility", "value": "invisible"},
                      "The cube disappears from the viewport but is still listed in the Stage panel.",
                      "Hide it with visibility."),
                _edit("scene_set_attribute", {"path": "/World/Render/Box", "name": "visibility", "value": "inherited"},
                      "It comes back: 'inherited' means 'visible unless a parent is invisible'.", "Show it again."),
                _edit("scene_set_attribute", {"path": "/World/Render/Box", "name": "purpose", "value": "guide"},
                      "It disappears again: guides are only drawn when a viewer turns guides on.",
                      "Now try purpose."),
                _edit("scene_set_attribute", {"path": "/World/Render/Box", "name": "purpose", "value": "default"},
                      "Back to normal.", "Put purpose back."),
                _edit("scene_set_attribute", {"path": "/World/Render/Box", "name": "size", "value": 4.0},
                      "The cube doubles to 4 m.", "Change the shape's size..."),
                _read("scene_bounds", {"prim_path": "/World/Render/Box"},
                      "Compare authored and computed extent: if the authored one still says 1, it is stale. That is "
                      "objective 5.6: recompute the extent (UsdGeom.Boundable.ComputeExtentFromPlugins) after changing "
                      "points or size.", "...and check whether the extent label kept up."),
            ],
            "try_yourself": "Set visibility='invisible' on /World/Render (the parent). Does Box hide too? Why?",
        },
        "value_ignored": {
            "title": "Why is my value ignored? Layers, strength and the edit target",
            "objectives": ["1.1", "1.8", "6.2", "1.5"],
            "words": {'layer': 'one file (sheet) of settings', 'sublayer': 'another layer stacked under yours; it is weaker', 'opinion': 'a value one layer writes for an attribute', 'edit target': 'the layer your next change is written into'},
            "concept": (
                "A USD scene is a stack of transparent sheets (layers). Each sheet can write an 'opinion' for a value. "
                "When two sheets disagree, the sheet higher in the stack wins. The root layer (your study.usda) sits "
                "above its sublayers, so if both write a ball's radius, the root wins. The edit target is the sheet "
                "your next edit is written on. Many 'my change is ignored' bugs are: you wrote on a weaker sheet "
                "than the one that already has an opinion."),
            "steps": [
                _edit("scene_create", {"path": "/World/Layers", "type_name": "Xform"}, "A Layers group appears.",
                      "Make a group for this lab."),
                _edit("scene_create", {"path": "/World/Layers/Ball", "type_name": "Sphere"},
                      "A sphere of radius 1 appears.", "Add a ball (written on the root layer)."),
                _edit("scene_set_attribute", {"path": "/World/Layers/Ball", "name": "radius", "value": 2.0},
                      "The ball grows to radius 2. This opinion lives on study.usda (the root layer).",
                      "Give it a radius on the root layer."),
                _edit("scene_add_sublayer", {"layer_path": "layers/anim.usda", "create_if_missing": True},
                      "Nothing changes in the viewport: a new, empty sheet anim.usda joined the stack, BELOW the root.",
                      "Add a weaker sheet: a sublayer."),
                _read("scene_layers", {}, "Strongest first: session layer, study.usda, then anim.usda."),
                _edit("scene_set_edit_target", {"layer": "anim.usda"},
                      "Nothing visible: from now on edits are written on anim.usda.", "Write on the weaker sheet."),
                _edit("scene_set_attribute", {"path": "/World/Layers/Ball", "name": "radius", "value": 5.0},
                      "The ball does NOT change. The preview already warned a stronger opinion hides it.",
                      "Try to make it radius 5 from the weaker sheet."),
                _read("scene_why_value", {"prim_path": "/World/Layers/Ball", "attribute": "radius"},
                      "Radius 2 wins from study.usda; radius 5 in anim.usda loses because the root is stronger."),
                _edit("scene_set_edit_target", {"layer": "study.usda"},
                      "Edits go to the root layer again.", "Always put the edit target back."),
            ],
            "try_yourself": "Predict: if you delete the radius from study.usda, what radius shows? Check with "
                            "scene_what_if(prim_path='/World/Layers/Ball', attribute='radius').",
        },
        "arcs": {
            "title": "Sublayer, reference or payload?",
            "objectives": ["1.3", "1.6"],
            "words": {'reference': 'pull one asset file into one prim, like a sticker of a ready-made model', 'payload': 'a reference you can unload to save memory', 'composition arc': 'any way USD pulls in content from elsewhere (sublayer, reference, payload, variant...)'},
            "concept": (
                "A sublayer adds a whole sheet to your stack (good for departments: layout, lighting). A reference "
                "pulls one asset into one prim, like placing a sticker of a ready-made model (good for props you "
                "reuse). A payload is a reference you can switch off: unloaded, it costs no memory (good for heavy "
                "assets). Your own local edits on that prim beat what the referenced file says: that is the L in "
                "LIVRPS beating the R."),
            "steps": [
                _edit("scene_create", {"path": "/World/Arcs", "type_name": "Xform"}, "An Arcs group appears.",
                      "Make a group."),
                _edit("scene_add_asset", {"prim_path": "/World/Arcs/RefBlock", "asset_path": f"{props}/red_block.usd",
                                          "translation": [0, 0, 0.5], "scale": 10},
                      "A red block (5 cm, scaled x10 so you can see it) appears.", "Reference a red block."),
                _edit("scene_add_payload", {"prim_path": "/World/Arcs/PayBlock", "asset_path": f"{props}/blue_block.usd",
                                            "translation": [1, 0, 0.5], "scale": 10},
                      "A blue block appears next to it, brought in as a payload.", "Payload a blue block."),
                _read("scene_composition", {"prim_path": "/World/Arcs/RefBlock"},
                      "The arcs list shows a reference (R) to red_block.usd."),
                _edit("scene_unload_payloads", {"paths": ["/World/Arcs/PayBlock"]},
                      "The blue block vanishes, but PayBlock stays in the Stage panel: its contents left memory.",
                      "Switch the payload off."),
                _edit("scene_load_payloads", {"paths": ["/World/Arcs/PayBlock"]}, "The blue block is back.",
                      "Switch it on again."),
            ],
            "try_yourself": "Which would you use for a 2 GB factory building you only need sometimes, and why?",
        },
        "instancing": {
            "title": "Instancing: many copies, one prototype",
            "objectives": ["1.2", "2.4", "2.2", "1.10"],
            "words": {'instance': 'a copy that shares its insides with other copies instead of owning them', 'prototype': 'the one shared original that instances point at', 'instance proxy': 'a read-only view of a part inside an instance'},
            "concept": (
                "If a warehouse has 1,000 identical boxes, storing 1,000 full copies wastes memory. Instancing keeps "
                "ONE shared prototype and lets each box point at it, like 1,000 shortcuts to one file. You mark a "
                "referencing prim instanceable. The catch: the parts inside an instance are shared, so you can't "
                "edit them one copy at a time; you edit the instance prim itself (its transform, a material bound "
                "on it) or make that one copy non-instanceable. Point instancing is the even lighter option for "
                "huge counts of simple copies."),
            "steps": [
                _edit("scene_create", {"path": "/World/Inst", "type_name": "Xform"}, "An Inst group appears.",
                      "Make a group."),
                _edit("scene_add_asset", {"prim_path": "/World/Inst/Box1", "asset_path": f"{props}/red_block.usd",
                                          "translation": [0, 2, 0.5], "scale": 10, "instanceable": True},
                      "A red block appears.", "Add the first instanced block."),
                _edit("scene_add_asset", {"prim_path": "/World/Inst/Box2", "asset_path": f"{props}/red_block.usd",
                                          "translation": [1, 2, 0.5], "scale": 10, "instanceable": True},
                      "A second identical block.", "Add a second one."),
                _read("scene_composition", {"prim_path": "/World/Inst/Box2"},
                      "It reports instanceable / an instance sharing a prototype with Box1."),
                _read("scene_children", {"path": "/World/Inst/Box1"},
                      "Children are instance proxies: you can look, not edit."),
                _edit("scene_move", {"path": "/World/Inst/Box2", "translation": [1, 2, 1.0]},
                      "Box2 rises: the instance prim itself can be edited.", "Edit the instance root (allowed)."),
            ],
            "try_yourself": "Try scene_set_attribute on /World/Inst/Box1/Cube (inside the instance) through "
                            "scene_safe_edit. What does the preview say, and how would you recolour only Box1?",
        },
        "variants": {
            "title": "Variant sets: one asset, switchable versions",
            "objectives": ["1.7", "1.6"],
            "words": {'variant set': 'a menu of versions on one asset', 'variant': 'one item in that menu', 'selection': 'which item is picked right now'},
            "concept": (
                "A variant set is a menu on an asset: pick one item and the asset changes, without copying files. "
                "The Franka robot has a 'Gripper' menu: Default, None, Robotiq_2F_85 and AlternateFinger. Your "
                "selection is just a small opinion on your prim; the asset file stays untouched. Variants are right "
                "when the versions are alternatives of the SAME thing (colours, LODs, grippers); they are wrong for "
                "unrelated things or when you need several at once."),
            "steps": [
                _edit("scene_create", {"path": "/World/Var", "type_name": "Xform"}, "A Var group appears.",
                      "Make a group."),
                _edit("robot_add_ready", {"name": "franka", "prim_path": "/World/Var/Franka", "position": [0, -2, 0]},
                      "A Franka arm appears.", "Add a Franka (it references franka.usd)."),
                _read("variant_list", {"root_path": "/World/Var/Franka"},
                      "Two sets: Mesh (Performance/Quality) and Gripper (Default selected)."),
                _edit("variant_select", {"prim_path": "/World/Var/Franka", "variant_set": "Gripper", "variant": "None"},
                      "The hand and fingers disappear.", "Pick Gripper = None."),
                _edit("variant_select", {"prim_path": "/World/Var/Franka", "variant_set": "Gripper",
                                         "variant": "Robotiq_2F_85"},
                      "A different (Robotiq) gripper appears.", "Pick the Robotiq gripper."),
                _edit("variant_select", {"prim_path": "/World/Var/Franka", "variant_set": "Gripper",
                                         "variant": "Default"}, "The normal Franka hand is back.", "Back to Default."),
            ],
            "try_yourself": "Your selection lives in study.usda, franka.usd says 'Default'. Which wins and which "
                            "LIVRPS letter explains it?",
        },
        "primvars": {
            "title": "Primvars: colour data on a mesh",
            "objectives": ["5.1", "8.1"],
            "words": {'primvar': 'extra data painted on a shape that renderers can read, like colour', 'interpolation': 'how many values a primvar has and how they spread over the shape', 'displayColor': 'the simple built-in colour primvar'},
            "concept": (
                "A primvar is extra data painted onto geometry that renderers can read, like displayColor. Its "
                "interpolation says how many values there are and where they go: constant = one value for the whole "
                "shape; uniform = one per face; vertex = one per point (blended across faces); faceVarying = one per "
                "corner of each face (sharp seams). The array length must match, or the renderer ignores it."),
            "steps": [
                _edit("scene_create", {"path": "/World/Prim", "type_name": "Xform"}, "A Prim group appears.",
                      "Make a group."),
                _edit("scene_create", {"path": "/World/Prim/Box", "type_name": "Cube"}, "A grey cube.",
                      "Add a cube."),
                _edit("scene_move", {"path": "/World/Prim/Box", "translation": [0, 4, 1]}, "It moves aside.",
                      "Move it where you can see it."),
                _edit("scene_set_attribute", {"path": "/World/Prim/Box", "name": "primvars:displayColor",
                                              "value": [[0.9, 0.1, 0.1]]},
                      "The cube turns red: one colour, constant interpolation.", "Paint it with displayColor."),
                _read("scene_properties", {"prim_path": "/World/Prim/Box"},
                      "primvars:displayColor is color3f[] with 1 value: that is constant interpolation."),
            ],
            "try_yourself": "A cube has 6 faces. How many colours would uniform interpolation need? Then do the "
                            "day's usd_playground_compose exercise to try vertex and uniform for real.",
        },
        "materials": {
            "title": "Materials and binding",
            "objectives": ["8.2", "8.3"],
            "words": {'material': 'a recipe for how a surface looks', 'UsdPreviewSurface': 'the simple material recipe every USD viewer understands', 'binding': 'the link that says which material a prim uses'},
            "concept": (
                "A material is a recipe for how a surface looks; UsdPreviewSurface is the simple recipe every viewer "
                "understands (colour, roughness, metallic...). Binding connects a material to a prim. A child's own "
                "binding normally beats its parent's, unless the parent binds with strength "
                "'strongerThanDescendants', which forces its material onto everything below."),
            "steps": [
                _edit("scene_create", {"path": "/World/Mat", "type_name": "Xform"}, "A Mat group appears.",
                      "Make a group."),
                _edit("scene_create", {"path": "/World/Mat/Box", "type_name": "Cube"}, "A grey cube.",
                      "Add a cube."),
                _edit("scene_move", {"path": "/World/Mat/Box", "translation": [0, 7, 1]}, "It moves aside.",
                      "Move it where you can see it."),
                _edit("scene_create_material", {"path": "/World/Mat/Looks/Blue", "diffuse_color": [0.1, 0.3, 0.9],
                                                "roughness": 0.4}, "Nothing visible yet: a material exists but is "
                      "bound to nothing.", "Make a blue UsdPreviewSurface material."),
                _edit("scene_bind_material", {"prim_path": "/World/Mat/Box", "material_path": "/World/Mat/Looks/Blue"},
                      "The cube turns blue.", "Bind it to the cube."),
                _read("scene_why_value", {"prim_path": "/World/Mat/Box", "attribute": "material"},
                      "It explains which binding wins and from where."),
            ],
            "try_yourself": "Make a red material, bind it to /World/Mat with strength 'strongerThanDescendants'. "
                            "Predict the cube's colour first.",
        },
        "attributes": {
            "title": "Attributes: types, authored values and fallbacks",
            "objectives": ["5.4", "5.2"],
            "words": {'type': 'what kind of value an attribute holds (number, text, colour...)', 'fallback': 'the built-in value USD reports when nobody wrote one', 'authored': 'actually written by some layer', 'token': 'a value from a fixed list of choices'},
            "concept": (
                "Every attribute has a type (double, float, token, string, color3f, arrays...). If nobody authored a "
                "value, USD returns the schema's fallback: a Cube's size is 2 even though no file says so. "
                "'Authored' means some layer wrote it. Tokens are for fixed choices (like visibility), strings for "
                "free text, doubles for precise numbers, float/color3f for lighter graphics data."),
            "steps": [
                _edit("scene_create", {"path": "/World/Attr", "type_name": "Xform"}, "An Attr group appears.",
                      "Make a group."),
                _edit("scene_create", {"path": "/World/Attr/Box", "type_name": "Cube"}, "A grey cube.",
                      "Add a cube."),
                _read("scene_properties", {"prim_path": "/World/Attr/Box"},
                      "size shows 2 as a fallback (not authored); note each attribute's type."),
                _edit("scene_set_attribute", {"path": "/World/Attr/Box", "name": "size", "value": 1.0},
                      "The cube shrinks.", "Author a size."),
                _read("scene_why_value", {"prim_path": "/World/Attr/Box", "attribute": "size"},
                      "Now size is authored in study.usda, not a fallback."),
                _edit("scene_set_attribute", {"path": "/World/Attr/Box", "name": "note", "value": "inspected",
                                              "create_type": "string"},
                      "Nothing visible: a custom string attribute was added.", "Add your own custom attribute."),
            ],
            "try_yourself": "Why is visibility a token and not a string?",
        },
    }


# Objectives a viewport cannot show (compiling, resolvers, Hydra plugins, exporters): honest pointers instead.
_CODE_ONLY = {
    "3.1": "compiling a plugin against one USD build", "3.2": "building OpenUSD from source",
    "3.3": "registering a custom model kind in plugInfo", "3.4": "designing IsA/API schemas",
    "3.5": "writing a custom Ar resolver", "3.7": "a Hydra scene index plugin", "3.8": "a resolver that emits prims",
    "6.1": "Sdf.ChangeBlock batching", "6.5": "TfDebug, Trace and TfMallocTag",
    "4.7": "writing an exporter", "4.8": "writing an importer", "7.5": "a custom Ar resolver",
    "7.8": "writing an importer",
}


def list_labs() -> dict[str, Any]:
    labs = _labs()
    return {"ok": True, "labs": [{"id": key, "title": lab["title"], "objectives": lab["objectives"],
                                  "steps": len(lab["steps"])} for key, lab in labs.items()],
            "study_stage": f"{STUDY_FOLDER}/{STUDY_STAGE}",
            "start_here": list(FOUNDATIONS),
            "class_labs": {name: f"class session {session}" for name, session in CLASS_LABS.items()},
            "note": "Beginners do the foundation labs (start_here) first, before any exam objective."}


def lab(objective_id: str | None = None, lab_id: str | None = None) -> dict[str, Any]:
    """The lab for an exam objective (e.g. '1.8') or a lab id, with the study-stage setup and how to run steps."""
    labs = _labs()
    if lab_id is not None:
        chosen = labs.get(lab_id)
        if chosen is None:
            return {"ok": False, "error": f"Unknown lab '{lab_id}'. Labs: {', '.join(labs)}."}
        key = lab_id
    elif objective_id is not None:
        objective_id = str(objective_id).strip()
        key = next((name for name, item in labs.items() if objective_id in item["objectives"]), None)
        if key is None:
            topic = _CODE_ONLY.get(objective_id)
            why = (f"Objective {objective_id} is about {topic}: that is code and build work, not something a "
                   "viewport can show." if topic else
                   f"There is no Isaac lab for objective {objective_id} yet.")
            return {"ok": True, "lab": None, "objective": objective_id, "message": why,
                    "instead": ("Learn it from the day's explanation and docs, then use python_show / "
                                "python_practice_list for code, or the day's usd_playground_compose exercise.")}
        chosen = labs[key]
    else:
        return list_labs()
    root = (workspace_root() / STUDY_FOLDER / STUDY_STAGE).as_posix()
    return {
        "ok": True, "lab_id": key, **chosen,
        "setup": [
            f"Isaac Sim must have the study stage open: {root}. Check with scene_summary: stage_name should end "
            f"with {STUDY_FOLDER}/{STUDY_STAGE}.",
            f"If it does not exist yet: in Isaac Sim do File > New, then File > Save As > {root}. Never run labs in "
            "a course scene or the physics test cell.",
            "If a lab group (e.g. /World/Layers) already exists from an earlier run, skip its create steps or delete "
            "the group first (scene_safe_edit with scene_delete).",
        ],
        "how_to_run": [
            "The learner may know nothing yet: teach every entry of 'words' (plain meaning plus an everyday example) "
            "before the concept, and explain every click (which menu or panel, where on screen).",
            "Teach the concept first in plain words; ask the learner to say it back in one sentence.",
            "Do the steps in order, ONE at a time. Before each, say what we are about to do and predict what will "
            "happen; after it, ask the learner what they see in Isaac Sim, then compare with 'look'.",
            "kind 'read': call the tool directly. kind 'edit': scene_safe_edit(tool=..., arguments=...), show the "
            "preview, wait for yes, then call again with confirm=<code>. kind 'ui': the learner does it by hand in "
            "Isaac Sim; wait until they say what they see.",
            "End with 'try_yourself', then the day's quiz.",
        ],
    }
