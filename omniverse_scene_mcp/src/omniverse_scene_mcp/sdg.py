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

_JOBS = {}
_TASKS = {}


def plan_sdg(project_dir: str, name: str = 'dataset_run', *, camera: str = '/World/Camera',
             frames: int = 3, width: int = 320, height: int = 240, seed: int = 42,
             annotations=None) -> dict:
    """Plan a small RGB/label dataset in a new folder; existing camera and semantic labels are prerequisites."""
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
        return {'ok': True, 'dry_run': True, 'written': False, 'output_dir': str(destination),
                'config': {'project_dir': project_dir, 'name': name, 'camera': camera, 'frames': frames,
                           'width': width, 'height': height, 'seed': seed, 'annotations': annotations},
                'prerequisites': ['Isaac/Kit Replicator extension already enabled', 'Existing camera on the active stage',
                                  'Semantic class labels for segmentation and bounding boxes', 'Use a dedicated test stage'],
                'note': 'Fixed seed controls Replicator RNG; GPU images are not guaranteed bit-identical. No domain randomization is added.'}
    except (ValueError, OSError, TypeError) as exc:
        return {'ok': False, 'error': str(exc), 'written': False}


def sdg_script(project_dir: str, name: str = 'dataset_run', *, camera: str = '/World/Camera',
               frames: int = 3, width: int = 320, height: int = 240, seed: int = 42, annotations=None) -> dict:
    """Generate a Script Editor example; return text only, with DRY_RUN=True until the user changes it."""
    plan = plan_sdg(project_dir, name, camera=camera, frames=frames, width=width, height=height, seed=seed, annotations=annotations)
    if not plan['ok']: return plan
    script = ('from omniverse_scene_mcp.sdg import start_sdg, sdg_status\n'
              'DRY_RUN = True  # inspect the plan before changing this to False\n'
              f'result = start_sdg(**{plan["config"]!r}, dry_run=DRY_RUN)\nprint(result)\n'
              '# Poll sdg_status(result["job_id"]) later; do not block the Kit main thread.\n')
    return {**plan, 'script': script, 'executed': False}


def start_sdg(project_dir: str, name: str = 'dataset_run', *, camera: str = '/World/Camera',
              frames: int = 3, width: int = 320, height: int = 240, seed: int = 42,
              annotations=None, dry_run: bool = True) -> dict:
    """Preview or schedule at most 100 Replicator frames; detach writer/destroy render product on completion or failure."""
    plan = plan_sdg(project_dir, name, camera=camera, frames=frames, width=width, height=height, seed=seed, annotations=annotations)
    if not plan['ok'] or dry_run is not False: return plan
    try:
        if _TASKS: raise ValueError('An MCP dataset job is already running')
        rep = importlib.import_module('omni.replicator.core')
        usd = importlib.import_module('omni.usd')
        from pxr import UsdGeom
        stage = usd.get_context().get_stage()
        if stage is None or not stage.GetPrimAtPath(camera).IsA(UsdGeom.Camera):
            raise ValueError('camera must identify an existing Camera on the active stage')
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
        _JOBS[job_id] = job
        while len(_JOBS) > 50: _JOBS.pop(next(iter(_JOBS)))

        async def capture():
            writer = product = None
            try:
                job['status'] = 'running'
                rep.set_global_seed(seed)
                product = rep.create.render_product(camera, (width, height))
                writer = rep.WriterRegistry.get('BasicWriter')
                writer.initialize(output_dir=result['directory'], **{a: True for a in plan['config']['annotations']})
                writer.attach([product])
                async with asyncio.timeout(300):
                    for frame in range(frames):
                        if usd.get_context().get_stage() != stage:
                            raise RuntimeError('Active stage changed; stopping this job')
                        await rep.orchestrator.step_async(rt_subframes=2, pause_timeline=True)
                        job['frames_stepped'] = frame + 1
                    await rep.orchestrator.wait_until_complete_async()
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


def sdg_status(job_id: str) -> dict:
    """Read this bridge's bounded job status; completed steps do not replace dataset validation."""
    return dict(_JOBS[job_id]) if job_id in _JOBS else {'ok': False, 'error': 'Unknown job in this bridge process'}


def sdg_cancel(job_id: str, *, dry_run: bool = True) -> dict:
    """Cancel only an MCP-owned Replicator task; preserve any partial output for inspection."""
    if job_id not in _TASKS: return {'ok': False, 'error': 'No active MCP job with this ID'}
    if dry_run is False: _TASKS[job_id].cancel()
    return {'ok': True, 'dry_run': dry_run is not False, 'cancel_requested': dry_run is False}
