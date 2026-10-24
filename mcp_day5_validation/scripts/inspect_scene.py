"""Print this project's units, layer stack and prims. Read-only: nothing is changed or saved.

Run it in either of two ways:
* From a terminal with a Python that has USD (pxr):  python scripts/inspect_scene.py
* Inside USD Composer or Isaac Sim: Window > Script Editor, File > Open this file, then Run.
  There it inspects the stage that is open in the app.
"""

from pathlib import Path

from pxr import Sdf, Usd, UsdGeom


def open_stage():
    """Return the stage to inspect."""
    try:
        script = Path(__file__).resolve()
    except NameError:
        # The Script Editor runs code without __file__: use the stage open in the app instead.
        import omni.usd

        return omni.usd.get_context().get_stage()
    # This file lives in <project>/scripts/, so root.usda is one folder up.
    return Usd.Stage.Open(str(script.parent.parent / "root.usda"))


def layer_name(layer):
    """A short name for a layer: its file name, or its identifier for in-memory layers."""
    return Path(layer.realPath).name if layer.realPath else layer.identifier


def main():
    stage = open_stage()
    print("Scene:", layer_name(stage.GetRootLayer()))
    print("Up axis:", UsdGeom.GetStageUpAxis(stage))
    print("Meters per unit:", UsdGeom.GetStageMetersPerUnit(stage))
    print("Default prim:", stage.GetDefaultPrim().GetPath())

    print()
    print("Layer stack, strongest first:")
    for number, layer in enumerate(stage.GetLayerStack(includeSessionLayers=False), start=1):
        print(f"  {number}. {layer_name(layer)}")

    print()
    print("Prims and the layer that defines each one ('def'):")
    for prim in stage.Traverse():
        # GetPrimStack lists every layer with an opinion about this prim, strongest first.
        defining = [layer_name(spec.layer) for spec in prim.GetPrimStack() if spec.specifier == Sdf.SpecifierDef]
        where = defining[0] if defining else "?"
        print(f"  {prim.GetPath()}  ({prim.GetTypeName() or 'no type'})  defined in {where}")


main()
