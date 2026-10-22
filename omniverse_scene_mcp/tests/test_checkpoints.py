"""Scene checkpoints: create/list/diff/restore/prune on temporary project folders (no app, no network).

Fixture-free test_* functions. Every test points $OMNIVERSE_ROOT at its own temporary workspace and clears
$OMNIVERSE_SCENE_CHECKPOINTS, so the user's real scenes are never touched.
"""

import contextlib
import hashlib
import json
import os
import tempfile
from pathlib import Path

try:
    from pxr import Sdf
except ImportError:  # pragma: no cover - depends on the local environment
    import unittest

    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

from omniverse_scene_mcp import checkpoints
from omniverse_scene_mcp.checkpoints import CheckpointError

MAIN = """#usda 1.0
(
    defaultPrim = "World"
    subLayers = [
        @./lighting.usda@
    ]
)

def Xform "World"
{
    def Cube "Box_01" (
        prepend references = @../assets/box.usda@
    )
    {
        double size = 1
        double3 xformOp:translate = (0.6, 0, 0)
        uniform token[] xformOpOrder = ["xformOp:translate"]
    }

    def Sphere "Ball"
    {
        double radius = 0.5
    }

    def Xform "Lamp_Holder"
    {
        def Cone "Shade"
        {
            double height = 2
        }
    }

    def Xform "Crate" (
        prepend payload = @../assets/crate.usdc@
        variants = {
            string look = "red"
        }
        prepend variantSets = "look"
    )
    {
        variantSet "look" = {
            "red" {
            }
            "blue" {
            }
        }
    }

    def Xform "Shelf"
    {
        token kind_note = "wood"
    }
}

over "Shared" (
    prepend references = @../../outside/shared.usda@
)
{
}

over "Ghost" (
    prepend references = @./missing.usda@
)
{
}
"""

LIGHTING = """#usda 1.0

def DistantLight "Sun"
{
    float inputs:intensity = 1000
}
"""

BOX = """#usda 1.0
(
    defaultPrim = "Box"
)

def Cube "Box"
{
    double size = 1
}
"""


@contextlib.contextmanager
def _workspace():
    """A temporary workspace with proj/ (PROJECT_CONTEXT.md, scenes/, assets/) and outside/shared.usda."""
    saved = {name: os.environ.get(name) for name in ("OMNIVERSE_ROOT", checkpoints.ENV_BASE)}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        os.environ["OMNIVERSE_ROOT"] = str(root)
        os.environ.pop(checkpoints.ENV_BASE, None)
        try:
            proj = root / "proj"
            (proj / "scenes").mkdir(parents=True)
            (proj / "assets").mkdir()
            (root / "outside").mkdir()
            (proj / "PROJECT_CONTEXT.md").write_text("# test project\n", encoding="utf-8")
            (proj / "scenes" / "main.usda").write_text(MAIN, encoding="utf-8")
            (proj / "scenes" / "lighting.usda").write_text(LIGHTING, encoding="utf-8")
            (proj / "assets" / "box.usda").write_text(BOX, encoding="utf-8")
            (root / "outside" / "shared.usda").write_text("#usda 1.0\n", encoding="utf-8")
            crate = Sdf.Layer.CreateAnonymous("crate.usda")
            spec = Sdf.CreatePrimInLayer(crate, "/Crate")
            spec.specifier = Sdf.SpecifierDef
            spec.typeName = "Cube"
            Sdf.AttributeSpec(spec, "size", Sdf.ValueTypeNames.Double).default = 0.5
            crate.defaultPrim = "Crate"
            assert crate.Export(str(proj / "assets" / "crate.usdc"))
            del crate, spec
            yield root
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def _scene(root):
    return root / "proj" / "scenes" / "main.usda"


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _tree(folder):
    """relative path -> (sha256, size, mtime_ns) for every file under folder."""
    folder = Path(folder)
    return {
        p.relative_to(folder).as_posix(): (_sha(p), p.stat().st_size, p.stat().st_mtime_ns)
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    }


def _edit(path, old, new):
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new), encoding="utf-8")


def _make_user_edits(root):
    """Move, resize, delete, rename, reparent, variant switch, new prim, new sublayer and a .usdc change."""
    scene = _scene(root)
    _edit(scene, "double3 xformOp:translate = (0.6, 0, 0)", "double3 xformOp:translate = (1.2, 0, 0)")
    _edit(scene, "double size = 1\n        double3", "double size = 2\n        double3")
    _edit(scene, '    def Sphere "Ball"\n    {\n        double radius = 0.5\n    }\n\n', "")
    _edit(scene, 'def Xform "Lamp_Holder"', 'def Xform "Lamp"')
    shelf = '    def Xform "Shelf"\n    {\n        token kind_note = "wood"\n    }\n'
    _edit(scene, shelf, '    def SphereLight "Light"\n    {\n    }\n')
    _edit(scene, '            "blue" {\n            }\n        }\n    }',
          '            "blue" {\n            }\n        }\n\n        def Xform "Shelf"\n        {\n'
          '            token kind_note = "wood"\n        }\n    }')
    _edit(scene, 'string look = "red"', 'string look = "blue"')
    _edit(scene, "@./lighting.usda@", "@./lighting.usda@,\n        @./extra.usda@")
    (scene.parent / "extra.usda").write_text('#usda 1.0\n\ndef Xform "Extra"\n{\n}\n', encoding="utf-8")
    crate_path = root / "proj" / "assets" / "crate.usdc"
    crate = Sdf.Layer.OpenAsAnonymous(str(crate_path))
    crate.GetAttributeAtPath("/Crate.size").default = 0.75
    assert crate.Export(str(crate_path))
    del crate


# ------------------------------------------------------------------------------------------------------ create


def test_dry_run_create_plans_local_layers_and_writes_nothing():
    with _workspace() as root:
        before = _tree(root)
        plan = checkpoints.create_checkpoint(_scene(root), "first try")
        assert plan["ok"] and plan["dry_run"] and not plan["created"]
        assert plan["project"].endswith("/proj")
        assert plan["scene_relative"] == "scenes/main.usda"
        roles = {item["path"]: item["role"] for item in plan["files"]}
        assert roles == {
            "scenes/main.usda": "root",
            "scenes/lighting.usda": "sublayer",
            "assets/box.usda": "reference",
            "assets/crate.usdc": "payload",
        }
        assert plan["files"][0]["path"] == "scenes/main.usda"
        reasons = {item["path"]: item["reason"] for item in plan["not_copied"]}
        assert reasons["../../outside/shared.usda"] == "outside the project folder"
        assert reasons["./missing.usda"] == "missing on disk"
        assert plan["total_bytes"] == sum(item["size"] for item in plan["files"])
        assert checkpoints.UNSAVED_WARNING in plan["warnings"]
        assert "Nothing was written" in plan["message"]
        assert plan["id"].startswith("0001-") and plan["id"].endswith("-first-try")
        assert _tree(root) == before
        assert not (root / "proj" / ".checkpoints").exists()
        json.dumps(plan)


def test_create_copies_and_verifies_files_and_list_numbers_them():
    with _workspace() as root:
        scene_files = {p: _sha(p) for p in (root / "proj").rglob("*") if p.is_file()}
        made = checkpoints.create_checkpoint(_scene(root), "Before lighting!", dry_run=False)
        assert made["ok"] and made["created"]
        folder = Path(made["path"])
        assert folder.parent == root / "proj" / ".checkpoints" / "main"
        assert folder.name == made["id"] and made["id"].startswith("0001-")
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["note"] == "Before lighting!" and manifest["kind"] == "manual"
        assert manifest["scene"] == "scenes/main.usda"
        for record in manifest["files"]:
            copy = folder / "files" / record["path"]
            original = root / "proj" / record["path"]
            assert _sha(copy) == record["sha256"] == _sha(original)
            assert record["size"] == original.stat().st_size
            assert record["source_mtime_ns"] == original.stat().st_mtime_ns
            assert not Path(record["path"]).is_absolute()
        assert {p: _sha(p) for p in scene_files} == scene_files  # the scene itself is untouched
        assert not list(folder.parent.glob(".incoming-*"))
        second = checkpoints.create_checkpoint(_scene(root), dry_run=False)
        assert second["id"].startswith("0002-") and second["id"].endswith("-checkpoint")
        listed = checkpoints.list_checkpoints(_scene(root))
        assert listed["count"] == 2 and listed["latest"] == second["id"]
        assert [row["number"] for row in listed["checkpoints"]] == [1, 2]
        assert listed["checkpoints"][0]["note"] == "Before lighting!"
        assert listed["checkpoints"][0]["file_count"] == 4
        json.dumps(listed)
        json.dumps(made)


def test_list_and_diff_without_checkpoints():
    with _workspace() as root:
        listed = checkpoints.list_checkpoints(_scene(root))
        assert listed["count"] == 0 and listed["latest"] is None
        assert not (root / "proj" / ".checkpoints").exists()
        try:
            checkpoints.diff(_scene(root))
        except CheckpointError as exc:
            assert "No checkpoints yet" in str(exc)
        else:
            raise AssertionError("diff without checkpoints should fail")


# -------------------------------------------------------------------------------------------------------- diff


def test_diff_explains_moves_renames_deletes_variants_sublayers_and_usdc_changes():
    with _workspace() as root:
        first = checkpoints.create_checkpoint(_scene(root), "start", dry_run=False)
        unchanged = checkpoints.diff(_scene(root))
        assert unchanged["same"] and unchanged["files"] == []
        assert unchanged["summary"] == ["No differences: the files are identical."]
        _make_user_edits(root)
        result = checkpoints.diff(_scene(root))  # latest checkpoint -> current files
        assert result["a"] == first["id"] and result["b"] == "current" and not result["same"]
        summary = result["summary"]
        expected = [
            "You moved /World/Box_01 from x=0.6 to x=1.2.",
            "You changed size on /World/Box_01 from 1 to 2.",
            "You deleted /World/Ball (a Sphere).",
            "You renamed /World/Lamp_Holder to Lamp.",
            "You moved /World/Shelf under /World/Crate (now /World/Crate/Shelf).",
            "You switched the 'look' variant on /World/Crate from red to blue.",
            "You added the sublayer ./extra.usda.",
            "You added /World/Light (a SphereLight).",
            "In assets/crate.usdc: You changed size on /Crate from 0.5 to 0.75.",
            "In scenes/extra.usda: scenes/extra.usda became part of the scene.",
        ]
        for sentence in expected:
            assert sentence in summary, (sentence, summary)
        assert not any("Shade" in s for s in summary)  # children of a renamed prim are not "added"
        rows = {row["path"]: row for row in result["files"]}
        assert rows["scenes/main.usda"]["status"] == "changed"
        assert rows["scenes/extra.usda"]["status"] == "added"
        assert rows["assets/crate.usdc"]["status"] == "changed"
        assert "scenes/lighting.usda" not in rows and "assets/box.usda" not in rows
        main_diff = rows["scenes/main.usda"]["diff"]
        assert "-        double3 xformOp:translate = (0.6, 0, 0)" in main_diff
        assert "+        double3 xformOp:translate = (1.2, 0, 0)" in main_diff
        crate_diff = rows["assets/crate.usdc"]["diff"]  # crate exported to text in memory
        assert "-    double size = 0.5" in crate_diff and "+    double size = 0.75" in crate_diff
        moved = next(c for c in result["changes"] if c["kind"] == "attribute_changed" and "translate" in c["path"])
        assert moved["path"] == "/World/Box_01.xformOp:translate" and moved["old"] == "(0.6, 0, 0)"
        assert any("unsaved changes" in w for w in result["warnings"])
        json.dumps(result)

        second = checkpoints.create_checkpoint(_scene(root), "after", dry_run=False)
        between = checkpoints.diff(_scene(root), a=1, b=second["id"])
        assert between["a"] == first["id"] and between["b"] == second["id"]
        assert "You moved /World/Box_01 from x=0.6 to x=1.2." in between["summary"]
        backwards = checkpoints.diff(_scene(root), a="latest", b="0001")
        assert "You moved /World/Box_01 from x=1.2 to x=0.6." in backwards["summary"]
        assert "You removed the sublayer ./extra.usda." in backwards["summary"]
        assert "In scenes/extra.usda: scenes/extra.usda is no longer part of the scene." in backwards["summary"]
        assert "You renamed /World/Lamp to Lamp_Holder." in backwards["summary"]


def test_diff_caps_lines_and_changes_and_rejects_unknown_ids():
    with _workspace() as root:
        checkpoints.create_checkpoint(_scene(root), dry_run=False)
        _make_user_edits(root)
        small = checkpoints.diff(_scene(root), max_lines=5, max_changes=2)
        assert small["truncated"]
        assert len(small["changes"]) == 2
        assert small["summary"][-1].startswith("...and ")
        total_lines = sum(len(row["diff"].splitlines()) for row in small["files"] if row["diff"])
        assert total_lines <= 5 + len(small["files"])
        try:
            checkpoints.diff(_scene(root), a="9999")
        except CheckpointError as exc:
            assert "No checkpoint" in str(exc)
        else:
            raise AssertionError("unknown checkpoint id should fail")


def test_layer_changes_bindings_visibility_arcs_metadata():
    old = Sdf.Layer.CreateAnonymous("old.usda")
    new = Sdf.Layer.CreateAnonymous("new.usda")
    for layer in (old, new):
        prim = Sdf.CreatePrimInLayer(layer, "/World/Chair")
        prim.specifier = Sdf.SpecifierDef
        prim.typeName = "Xform"
        Sdf.CreatePrimInLayer(layer, "/World").specifier = Sdf.SpecifierDef
    old.defaultPrim = "World"
    new.defaultPrim = "Root"
    old_chair, new_chair = old.GetPrimAtPath("/World/Chair"), new.GetPrimAtPath("/World/Chair")
    rel = Sdf.RelationshipSpec(old_chair, "material:binding")
    rel.targetPathList.explicitItems = ["/World/Looks/Wood"]
    rel = Sdf.RelationshipSpec(new_chair, "material:binding")
    rel.targetPathList.explicitItems = ["/World/Looks/Metal"]
    Sdf.AttributeSpec(new_chair, "visibility", Sdf.ValueTypeNames.Token).default = "invisible"
    old_chair.referenceList.Prepend(Sdf.Reference("./chair_a.usda"))
    new_chair.referenceList.Prepend(Sdf.Reference("./chair_b.usda"))
    new_chair.active = False
    texts = [change["text"] for change in checkpoints.layer_changes(old, new)]
    assert "You changed the material on /World/Chair from /World/Looks/Wood to /World/Looks/Metal." in texts
    assert "You hid /World/Chair." in texts
    assert "You added a reference to ./chair_b.usda on /World/Chair." in texts
    assert "You removed the reference to ./chair_a.usda from /World/Chair." in texts
    assert "You changed the default prim from World to Root." in texts
    assert "You deactivated /World/Chair." in texts


# ----------------------------------------------------------------------------------------------------- restore


def test_restore_dry_run_then_restore_with_automatic_safety_checkpoint():
    with _workspace() as root:
        first = checkpoints.create_checkpoint(_scene(root), "good state", dry_run=False)
        original = {r["path"]: r["sha256"] for r in first["manifest"]["files"]}
        _make_user_edits(root)
        edited = _tree(root)
        plan = checkpoints.restore_checkpoint(_scene(root), first["id"])
        assert plan["ok"] and plan["dry_run"] and plan["restored"] == []
        actions = {item["path"]: item["action"] for item in plan["plan"]}
        assert actions == {"scenes/main.usda": "restore", "scenes/lighting.usda": "unchanged",
                           "assets/box.usda": "unchanged", "assets/crate.usdc": "restore"}
        assert plan["left_alone"] == ["scenes/extra.usda"]
        assert checkpoints.APP_RELOAD_WARNING in plan["warnings"]
        assert _tree(root) == edited  # a dry run writes nothing, not even a safety checkpoint
        assert checkpoints.list_checkpoints(_scene(root))["count"] == 1

        done = checkpoints.restore_checkpoint(_scene(root), first["id"], dry_run=False)
        assert done["ok"] and not done["dry_run"]
        assert sorted(done["restored"]) == ["assets/crate.usdc", "scenes/main.usda"]
        for rel, sha in original.items():
            assert _sha(root / "proj" / rel) == sha
        assert (root / "proj" / "scenes" / "extra.usda").exists()  # not in the manifest: left alone
        assert checkpoints.APP_RELOAD_WARNING in done["warnings"]
        listed = checkpoints.list_checkpoints(_scene(root))
        assert listed["count"] == 2
        safety = listed["checkpoints"][-1]
        assert safety["id"] == done["safety_checkpoint"] and safety["kind"] == "safety"
        safety_manifest = json.loads(
            (root / "proj" / ".checkpoints" / "main" / safety["id"] / "manifest.json").read_text(encoding="utf-8"))
        assert safety_manifest["taken_before_restoring"] == first["id"]
        edited_main = edited["proj/scenes/main.usda"][0]
        assert {r["path"]: r["sha256"] for r in safety_manifest["files"]}["scenes/main.usda"] == edited_main
        undo_view = checkpoints.diff(_scene(root), a=safety["id"], b="current")
        assert "You moved /World/Box_01 from x=1.2 to x=0.6." in undo_view["summary"]
        again = checkpoints.restore_checkpoint(_scene(root), first["id"], dry_run=False)
        assert again["ok"] and again["restored"] == [] and "already match" in again["message"]
        undone = checkpoints.restore_checkpoint(_scene(root), safety["id"], dry_run=False)
        assert undone["ok"] and _sha(_scene(root)) == edited_main
        assert not list((root / "proj").rglob("*.tmp"))


def test_restore_refuses_when_a_file_changes_after_the_safety_checkpoint():
    with _workspace() as root:
        first = checkpoints.create_checkpoint(_scene(root), dry_run=False)
        _make_user_edits(root)
        original_create = checkpoints._create
        raced_text = _scene(root).read_text(encoding="utf-8") + "\n# saved by the app at the same moment\n"

        def create_then_app_saves(*args, **kwargs):
            result = original_create(*args, **kwargs)
            _scene(root).write_text(raced_text, encoding="utf-8")
            stat = _scene(root).stat()
            os.utime(_scene(root), ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
            return result

        checkpoints._create = create_then_app_saves
        try:
            refused = checkpoints.restore_checkpoint(_scene(root), first["id"], dry_run=False)
        finally:
            checkpoints._create = original_create
        assert not refused["ok"] and refused["refused"] == "race"
        assert "scenes/main.usda" in refused["error"] and refused["restored"] == []
        assert _scene(root).read_text(encoding="utf-8") == raced_text  # nothing overwritten
        assert refused["safety_checkpoint"].startswith("0002-")
        crate_now = _sha(root / "proj" / "assets" / "crate.usdc")
        assert crate_now != {r["path"]: r["sha256"] for r in first["manifest"]["files"]}["assets/crate.usdc"]


def test_restore_recreates_a_deleted_layer():
    with _workspace() as root:
        first = checkpoints.create_checkpoint(_scene(root), dry_run=False)
        box = root / "proj" / "assets" / "box.usda"
        box.unlink()
        plan = checkpoints.restore_checkpoint(_scene(root), "latest")
        assert {i["path"]: i["action"] for i in plan["plan"]}["assets/box.usda"] == "recreate"
        assert not box.exists()
        done = checkpoints.restore_checkpoint(_scene(root), first["id"], dry_run=False)
        assert done["restored"] == ["assets/box.usda"] and box.read_text(encoding="utf-8") == BOX


def test_restore_refuses_damaged_or_unsafe_manifests():
    with _workspace() as root:
        first = checkpoints.create_checkpoint(_scene(root), dry_run=False)
        folder = Path(first["path"])
        _edit(_scene(root), "(0.6, 0, 0)", "(9, 0, 0)")
        changed = _sha(_scene(root))
        (folder / "files" / "assets" / "box.usda").write_text("#usda 1.0\n# tampered\n", encoding="utf-8")
        damaged = checkpoints.restore_checkpoint(_scene(root), first["id"], dry_run=False)
        assert not damaged["ok"] and damaged["refused"] == "damaged" and "assets/box.usda" in damaged["error"]
        assert _sha(_scene(root)) == changed
        manifest_path = folder / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"].append({"path": "../../escape.usda", "sha256": "0" * 64, "size": 1})
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        try:
            checkpoints.restore_checkpoint(_scene(root), first["id"], dry_run=False)
        except CheckpointError as exc:
            assert "Unsafe path" in str(exc)
        else:
            raise AssertionError("a manifest path leaving the project must be refused")
        assert not (root / "escape.usda").exists() and _sha(_scene(root)) == changed
        assert checkpoints.list_checkpoints(_scene(root))["count"] == 1  # no safety checkpoint was taken


# --------------------------------------------------------------------------------------------- caps and scope


def test_size_caps_refuse_or_skip_and_write_nothing():
    with _workspace() as root:
        before = _tree(root)
        refused = checkpoints.create_checkpoint(_scene(root), dry_run=False, max_total_mb=0.0005)
        assert not refused["ok"] and refused["refused"] == "size" and not refused["created"]
        assert "max_total_mb" in refused["error"]
        assert _tree(root) == before and not (root / "proj" / ".checkpoints").exists()
        skipped = checkpoints.create_checkpoint(_scene(root), dry_run=False, max_file_mb=0.00005)  # 52 bytes
        assert skipped["created"]
        assert [item["path"] for item in skipped["files"]] == ["scenes/main.usda"]  # the root is always copied
        reasons = {item["path"]: item["reason"] for item in skipped["not_copied"]}
        assert reasons["./lighting.usda"] == "78 bytes is over the 52 bytes per-file cap"
        assert "per-file cap" in reasons["../assets/box.usda"] and "per-file cap" in reasons["../assets/crate.usdc"]


def test_scene_outside_the_workspace_or_project_is_refused():
    with _workspace() as root, tempfile.TemporaryDirectory() as other:
        outside = Path(other) / "elsewhere.usda"
        outside.write_text("#usda 1.0\n", encoding="utf-8")
        for call in (
            lambda: checkpoints.create_checkpoint(outside, dry_run=False),
            lambda: checkpoints.list_checkpoints(outside),
            lambda: checkpoints.create_checkpoint(_scene(root), project_dir=other),
            lambda: checkpoints.create_checkpoint(_scene(root), project_dir=root / "outside"),
            lambda: checkpoints.create_checkpoint(root / "proj" / "PROJECT_CONTEXT.md"),
        ):
            try:
                call()
            except CheckpointError:
                pass
            else:
                raise AssertionError("expected CheckpointError")
        assert not (Path(other) / ".checkpoints").exists() and not (root / "outside" / ".checkpoints").exists()


def test_project_folder_falls_back_to_the_scene_folder_and_env_overrides_the_base():
    with _workspace() as root:
        loose = root / "loose" / "main.usda"
        loose.parent.mkdir()
        loose.write_text('#usda 1.0\n\ndef Xform "World"\n{\n}\n', encoding="utf-8")
        plan = checkpoints.create_checkpoint(loose)
        assert plan["project"].endswith("/loose") and plan["scene_relative"] == "main.usda"
        store = root / "store"
        os.environ[checkpoints.ENV_BASE] = str(store)
        a = checkpoints.create_checkpoint(loose, dry_run=False)
        b = checkpoints.create_checkpoint(_scene(root), dry_run=False)
        assert Path(a["path"]).parent.parent == store and Path(b["path"]).parent.parent == store
        assert Path(a["path"]).parent != Path(b["path"]).parent  # same stem, different scenes, different folders
        assert not (root / "loose" / ".checkpoints").exists() and not (root / "proj" / ".checkpoints").exists()
        assert checkpoints.list_checkpoints(loose)["count"] == 1
        assert checkpoints.list_checkpoints(_scene(root))["count"] == 1


# ------------------------------------------------------------------------------------------------------- prune


def test_prune_keeps_the_newest_and_dry_run_deletes_nothing():
    with _workspace() as root:
        ids = [checkpoints.create_checkpoint(_scene(root), f"step {n}", dry_run=False)["id"] for n in range(4)]
        before = _tree(root)
        plan = checkpoints.prune(_scene(root), keep=2)
        assert plan["dry_run"] and plan["would_delete"] == ids[:2] and plan["kept"] == ids[2:]
        assert plan["deleted"] == [] and plan["freed_bytes"] > 0
        assert _tree(root) == before
        done = checkpoints.prune(_scene(root), keep=2, dry_run=False)
        assert done["deleted"] == ids[:2]
        assert [row["id"] for row in checkpoints.list_checkpoints(_scene(root))["checkpoints"]] == ids[2:]
        assert checkpoints.prune(_scene(root), keep=5, dry_run=False)["deleted"] == []
        try:
            checkpoints.prune(_scene(root), keep=0, dry_run=False)
        except CheckpointError:
            pass
        else:
            raise AssertionError("keep=0 must be refused")
        assert checkpoints.list_checkpoints(_scene(root))["count"] == 2


def _link_dir(link, target):
    """A directory symlink, or a junction when symlinks need admin rights; False when neither works."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    try:
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
        return True
    except (ImportError, OSError):
        return False


def test_links_that_leave_the_project_are_not_followed():
    with _workspace() as root:
        assets = root / "proj" / "assets"
        first = checkpoints.create_checkpoint(_scene(root), dry_run=False)
        moved = root / "outside" / "assets"
        assets.rename(moved)
        if not _link_dir(assets, moved):
            assets.rename(root / "proj" / "assets_back")
            return  # neither symlinks nor junctions are available here
        try:
            plan = checkpoints.create_checkpoint(_scene(root))
            reasons = {item["path"]: item["reason"] for item in plan["not_copied"]}
            assert reasons["../assets/box.usda"] == "outside the project folder"
            assert reasons["../assets/crate.usdc"] == "outside the project folder"
            (moved / "box.usda").write_text("#usda 1.0\n# changed outside\n", encoding="utf-8")
            outside_before = _tree(moved)
            try:
                checkpoints.restore_checkpoint(_scene(root), first["id"], dry_run=False)
            except CheckpointError as exc:
                assert "link" in str(exc)
            else:
                raise AssertionError("restore must not write through a link that leaves the project")
            assert _tree(moved) == outside_before
            assert checkpoints.list_checkpoints(_scene(root))["count"] == 1
        finally:
            if os.path.lexists(assets):
                os.rmdir(assets)  # removes only the link, never the folder it points to
