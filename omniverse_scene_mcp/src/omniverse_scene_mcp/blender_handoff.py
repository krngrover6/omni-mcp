"""Generate a selection-only Blender export script and inspect its USD handoff read-only."""
from __future__ import annotations
from .day5_io import new_directory


def blender_export_plan(project_dir: str, name: str = 'blender_export', *, animation: bool = False) -> dict:
    """Return a Blender script for a new export folder; never execute Blender or alter selected objects."""
    try:
        target = new_directory(project_dir, name)
        if not isinstance(animation, bool): raise ValueError('animation must be a boolean')
        script = f'''# Run in Blender's Text Editor after reviewing the plan.
from pathlib import Path
import bpy

DRY_RUN = True
directory = Path({str(target)!r})
destination = directory / 'asset.usdc'
if not bpy.context.selected_objects:
    raise RuntimeError('Select the intended export objects first')
if directory.exists():
    raise RuntimeError('Refusing to overwrite an existing export directory')
options = dict(filepath=str(destination), selected_objects_only=True,
               export_animation={animation!r}, export_materials=True,
               generate_preview_surface=True, relative_paths=True,
               overwrite_textures=False, root_prim_path='/Asset')
properties = bpy.ops.wm.usd_export.get_rna_type().properties.keys()
if not all(key in properties for key in options):
    raise RuntimeError('This Blender USD exporter lacks a required option; no export started')
if 'export_textures_mode' in properties:
    options['export_textures_mode'] = 'NEW'
elif 'export_textures' in properties:
    options['export_textures'] = True
print('USD export plan:', options)
if DRY_RUN is False:
    directory.mkdir()
    result = bpy.ops.wm.usd_export(**options)
    if 'FINISHED' not in result:
        raise RuntimeError('USD export did not finish; inspect the new output directory')
'''
        return {'ok': True, 'dry_run': True, 'written': False, 'executed': False,
                'output_dir': str(target), 'script': script,
                'next_step': 'Review/run in Blender with DRY_RUN=True, then explicitly apply and inspect the exported USD.'}
    except (OSError, ValueError, TypeError) as exc:
        return {'ok': False, 'error': str(exc), 'written': False}


def blender_handoff_check(stage, *, max_prims: int = 10000) -> dict:
    """Inspect exported stage metadata and dependencies; do not assume Blender's authored axes or units."""
    from .asset_health import asset_health
    from .day5_io import integer
    try:
        from pxr import UsdGeom
        integer(max_prims, 'max_prims', 1, 50000)
        health = asset_health(stage, max_prims=max_prims)
        default = stage.GetDefaultPrim()
        return {'ok': True, 'read_only': True, 'default_prim': str(default.GetPath()) if default else None,
                'up_axis': str(UsdGeom.GetStageUpAxis(stage)), 'meters_per_unit': UsdGeom.GetStageMetersPerUnit(stage),
                'health': health, 'warnings': [] if default else ['No default prim; reference an explicit prim path.'],
                'note': 'Preview Surface approximates Blender materials; compare appearance in the target renderer.'}
    except (ImportError, ValueError, RuntimeError) as exc:
        return {'ok': False, 'error': str(exc), 'written': False}
