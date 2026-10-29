"""Bounded Replicator BasicWriter jobs. Planning is offline; execution is Kit-only and explicitly authorized.

Installed API source: Isaac 6.1 extscache/omni.replicator.core-1.13.36+110.3.0.wx64.r.cp312/
omni/replicator/core/scripts/{create,orchestrator}.py and writers_default/basicwriter.py.
"""
from __future__ import annotations
import asyncio
import importlib
import time
import uuid
from .day5_io import bundle, integer, json_text, new_directory
from . import sdg_randomize

_JOBS = {}
_TASKS = {}


def plan_sdg(project_dir: str, name: str = 'dataset_run', *, camera: str = '/World/Camera',
             frames: int = 3, width: int = 320, height: int = 240, seed: int = 42,
             annotations=None, randomize=None) -> dict:
    """Plan a small RGB/label dataset in a new folder; existing camera and semantic labels are prerequisites.
    randomize (optional) changes prims/lights every frame, e.g. {'prims': [...], 'position': {'min': [..],
    'max': [..]}, 'rotate_z': True, 'colors': 'any', 'lights': ['/World/DomeLight'], 'intensity': [500, 2000]};
    changes go to the session layer and are put back when the job ends."""
    try:
        destination = new_directory(project_dir, name)
        integer(frames, 'frames', 1, 100); integer(width, 'width', 16, 2048); integer(height, 'height', 16, 2048)
        integer(seed, 'seed', 0, 2**31-1)
        if not isinstance(camera, str) or not camera.startswith('/') or any(c in camera for c in '\n\r'):
            raise ValueError('camera must be an absolute USD prim path')
        annotations = ['rgb'] if annotations is None else annotations
        if not isinstance(annotations, list) or not annotations or len(set(annotations)) != len(annotations) or any(
            a not in ('rgb', 'semantic_segmentation', 'bounding_box_2d_tight') for a in annotations):
            raise ValueError('Choose RGB, semantic segmentation and/or tight 2D boxes')
        spec = sdg_randomize.validate(randomize)
        return {'ok': True, 'dry_run': True, 'written': False, 'output_dir': str(destination),
                'config': {'project_dir': project_dir, 'name': name, 'camera': camera, 'frames': frames,
                           'width': width, 'height': height, 'seed': seed, 'annotations': annotations,
                           'randomize': randomize if spec else None},
                'prerequisites': ['Isaac/Kit Replicator extension already enabled', 'Existing camera on the active stage',
                                  'Semantic class labels for segmentation and bounding boxes', 'Use a dedicated test stage'],
                'note': 'Fixed seed controls Replicator RNG; GPU images are not guaranteed bit-identical. ' + (
                    'Domain randomization: every frame moves/turns/recolours the chosen prims and changes light '
                    'brightness (same seed, same sequence); all of it is undone when the job ends.' if spec else
                    'No domain randomization (pass randomize={...} to vary prims and lights per frame).')}
    except (ValueError, OSError, TypeError) as exc:
        return {'ok': False, 'error': str(exc), 'written': False}


def sdg_script(project_dir: str, name: str = 'dataset_run', *, camera: str = '/World/Camera',
               frames: int = 3, width: int = 320, height: int = 240, seed: int = 42, annotations=None,
               randomize=None) -> dict:
    """Generate a Script Editor example; return text only, with DRY_RUN=True until the user changes it."""
    plan = plan_sdg(project_dir, name, camera=camera, frames=frames, width=width, height=height, seed=seed,
                    annotations=annotations, randomize=randomize)
    if not plan['ok']: return plan
    script = ('from omniverse_scene_mcp.sdg import start_sdg, sdg_status\n'
              'DRY_RUN = True  # inspect the plan before changing this to False\n'
              f'result = start_sdg(**{plan["config"]!r}, dry_run=DRY_RUN)\nprint(result)\n'
              '# Poll sdg_status(result["job_id"]) later; do not block the Kit main thread.\n')
    return {**plan, 'script': script, 'executed': False}


def start_sdg(project_dir: str, name: str = 'dataset_run', *, camera: str = '/World/Camera',
              frames: int = 3, width: int = 320, height: int = 240, seed: int = 42,
              annotations=None, randomize=None, dry_run: bool = True) -> dict:
    """Preview or schedule at most 100 Replicator frames; detach writer/destroy render product on completion or failure."""
    plan = plan_sdg(project_dir, name, camera=camera, frames=frames, width=width, height=height, seed=seed,
                    annotations=annotations, randomize=randomize)
    if not plan['ok']: return plan
    if dry_run is not False:
        problem = _live_camera_problem(camera)
        return {**plan, 'ok': False, 'error': problem} if problem else plan
    try:
        if _TASKS: raise ValueError('An MCP dataset job is already running')
        rep = importlib.import_module('omni.replicator.core')
        usd = importlib.import_module('omni.usd')
        stage = usd.get_context().get_stage()
        problem = camera_problem(stage, camera)
        if problem: raise ValueError(problem)
        spec = sdg_randomize.validate(randomize)
        randomizer = sdg_randomize.Randomizer(stage, spec, seed) if spec else None
        timeline = importlib.import_module('omni.timeline').get_timeline_interface()
        if timeline.is_playing(): raise ValueError('Stop simulation before starting a dataset job')
        status = str(rep.orchestrator.get_status()).lower()
        if 'started' in status or 'running' in status:
            raise ValueError('Another Replicator run is active')
        try: loop = asyncio.get_running_loop()
        except RuntimeError: loop = asyncio.get_event_loop()  # Kit main-thread RPC/update callbacks
        if loop.is_closed(): raise RuntimeError('Kit event loop is closed')
        result = bundle(project_dir, name, {'dataset_manifest.json': json_text({
            'schema_version': 1, **plan['config'], 'manifest_type': 'requested_configuration',
            'note': 'Inspect job status and run dataset_check before accepting outputs.'})}, dry_run=False)
        job_id = uuid.uuid4().hex
        job = {'ok': True, 'job_id': job_id, 'status': 'pending', 'output_dir': result['directory'],
               'frames_stepped': 0, 'requested_frames': frames, 'dry_run': False, 'written': True}
        if randomizer is not None:
            job['randomize_warnings'] = randomizer.warnings
            job['randomized_frames'] = []
        _JOBS[job_id] = job
        while len(_JOBS) > 50: _JOBS.pop(next(iter(_JOBS)))

        async def capture():
            writer = product = None
            try:
                job['status'] = 'running'
                # Seen live in Isaac 6.1: after a camera read and Stop, Replicator sat in STOPPING and skipped every
                # step ("in the process of stopping, step skipped"), yet the job said completed with no images.
                app = importlib.import_module('omni.kit.app').get_app()
                for _ in range(600):
                    if 'stopping' not in str(rep.orchestrator.get_status()).lower(): break
                    await app.next_update_async()
                else:
                    raise RuntimeError('Replicator stayed in STOPPING for 600 frames; try again in a moment')
                rep.set_global_seed(seed)
                product = rep.create.render_product(camera, (width, height))
                writer = rep.WriterRegistry.get('BasicWriter')
                writer.initialize(output_dir=result['directory'], **{a: True for a in plan['config']['annotations']})
                writer.attach([product])
                async with asyncio.timeout(300):
                    for frame in range(frames):
                        if usd.get_context().get_stage() != stage:
                            raise RuntimeError('Active stage changed; stopping this job')
                        if randomizer is not None:  # DOMAIN RANDOMIZATION: a new variation for this frame
                            changes = randomizer.apply()
                            if frame < 20: job['randomized_frames'].append(changes)
                        await rep.orchestrator.step_async(rt_subframes=2, pause_timeline=True)
                        job['frames_stepped'] = frame + 1
                    await rep.orchestrator.wait_until_complete_async()
                written = frames_written(result['directory'], plan['config']['annotations'])
                job['frames_written'] = written
                if written < frames:
                    raise RuntimeError(f'Only {written} of {frames} frames reached the disk; Replicator skipped '
                                       'steps (see kit_logs). Nothing else is wrong with the scene; run it again.')
                job['status'] = 'completed'
            except asyncio.CancelledError:
                job['status'] = 'cancelled'
            except Exception as exc:
                job.update(status='failed', error=f'{type(exc).__name__}: {exc}'[:600])
            finally:
                cleanup = []
                for obj, method in ((writer, 'detach'), (product, 'destroy')):
                    if obj is not None:
                        try: getattr(obj, method)()
                        except Exception as exc: cleanup.append(str(exc)[:200])
                if randomizer is not None:
                    try: job['restored_values'] = randomizer.restore()
                    except Exception as exc: cleanup.append(f'restore: {exc}'[:200])
                if cleanup: job['cleanup_warnings'] = cleanup
                job['finished_at'] = time.time()
                _TASKS.pop(job_id, None)
        _TASKS[job_id] = loop.create_task(capture())
        def finished(task):
            _TASKS.pop(job_id, None)
            if task.cancelled(): job['status'] = 'cancelled'
            elif task.exception() is not None: job.update(status='failed', error=str(task.exception())[:600])
        _TASKS[job_id].add_done_callback(finished)
        return dict(job)
    except (ImportError, ValueError, OSError, RuntimeError) as exc:
        return {'ok': False, 'error': str(exc), 'written': False}


def frames_written(directory, annotations) -> int:
    """How many frames BasicWriter actually wrote: the smallest file count over the requested annotations."""
    from pathlib import Path
    patterns = {'rgb': 'rgb_*.png', 'semantic_segmentation': 'semantic_segmentation_[0-9]*.png',
                'bounding_box_2d_tight': 'bounding_box_2d_tight_[0-9]*.npy'}
    folder = Path(directory)
    return min(len(list(folder.glob(patterns[a]))) for a in annotations) if annotations else 0


def camera_problem(stage, camera: str) -> str | None:
    """Why `camera` cannot be used on `stage` (missing or not a Camera, with the cameras that exist), else None.

    Seen live in Isaac 6.1: IsA() on a missing prim raised "Accessed invalid null prim" instead of a clear message,
    and the preview said ok for a camera that did not exist.
    """
    from pxr import UsdGeom
    if stage is None:
        return 'No stage is open.'
    prim = stage.GetPrimAtPath(camera)
    if prim and prim.IsValid() and prim.IsA(UsdGeom.Camera):
        return None
    found = [str(p.GetPath()) for p in stage.Traverse() if p.IsA(UsdGeom.Camera)
             and not str(p.GetPath()).startswith('/OmniverseKit_')][:5]
    what = 'is not a Camera' if prim and prim.IsValid() else 'does not exist'
    hint = f"Cameras in this stage: {', '.join(found)}." if found else 'This stage has no camera: add one with scene_create_camera or sensor_create.'
    return f'{camera} {what}. {hint}'


def _live_camera_problem(camera: str) -> str | None:
    """camera_problem on Kit's open stage; None outside Kit (planning stays offline there)."""
    try:
        stage = importlib.import_module('omni.usd').get_context().get_stage()
    except Exception:
        return None
    return camera_problem(stage, camera)


def sdg_status(job_id: str) -> dict:
    """Read this bridge's bounded job status; completed steps do not replace dataset validation."""
    return dict(_JOBS[job_id]) if job_id in _JOBS else {'ok': False, 'error': 'Unknown job in this bridge process'}


def sdg_cancel(job_id: str, *, dry_run: bool = True) -> dict:
    """Cancel only an MCP-owned Replicator task; preserve any partial output for inspection."""
    if job_id not in _TASKS: return {'ok': False, 'error': 'No active MCP job with this ID'}
    if dry_run is False: _TASKS[job_id].cancel()
    return {'ok': True, 'dry_run': dry_run is not False, 'cancel_requested': dry_run is False}
