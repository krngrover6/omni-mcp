"""Reuse the existing active-viewport capture pipeline; never silently change camera or renderer settings."""
from __future__ import annotations
from .day5_io import bundle, integer, json_text, new_directory
import asyncio
import importlib
import time
import uuid
import zlib
from pathlib import Path

_JOBS = {}
_TASKS = {}


def render_plan(project_dir: str, name: str = 'capture_run', *, width: int | None = None,
                height: int | None = None) -> dict:
    """Plan a new capture folder. Optional size resizes a copy; it does not increase rendered detail."""
    try:
        target = new_directory(project_dir, name)
        if width is not None: integer(width, 'width', 16, 4096)
        if height is not None: integer(height, 'height', 16, 4096)
        return {'ok': True, 'dry_run': True, 'written': False, 'output_dir': str(target),
                'width': width, 'height': height, 'camera': 'active viewport camera',
                'note': 'Capture keeps renderer, exposure and camera unchanged. Requested dimensions resize an output copy.'}
    except (ValueError, OSError, TypeError) as exc:
        return {'ok': False, 'error': str(exc), 'written': False}


def render_capture(project_dir: str, name: str = 'capture_run', *, width: int | None = None,
                   height: int | None = None, dry_run: bool = True) -> dict:
    """Preview or schedule an active-viewport PNG in a fresh directory using the existing capture helper."""
    plan = render_plan(project_dir, name, width=width, height=height)
    if not plan['ok'] or dry_run is not False: return plan
    from . import kit_live_extras as extras
    # Check availability before creating even an empty output directory.
    utility = extras._kit_module('omni.kit.viewport.utility')
    if utility is None or utility.get_active_viewport() is None:
        return {'ok': False, 'error': 'No active Kit viewport', 'written': False}
    try:
        created = bundle(project_dir, name, {'capture_plan.json': json_text(plan)}, dry_run=False)
        result = extras.capture_viewport(created['directory'], width=width, height=height)
        return {**result, 'dry_run': False, 'output_dir': created['directory'],
                'note': 'Poll scene_capture_status(path); it finishes the optional resized copy.'}
    except (ValueError, OSError) as exc:
        return {'ok': False, 'error': str(exc), 'written': False}


def plan_image_sequence(project_dir: str, name: str = 'sequence_run', *, start: int = 0, end: int = 2,
                        fps: int = 24, width: int = 320, height: int = 240) -> dict:
    """Plan up to 60 viewport captures at frame/fps seconds, into a new directory; no timeline changes."""
    try:
        target=new_directory(project_dir,name)
        integer(start,'start',0,100000); integer(end,'end',0,100000); integer(fps,'fps',1,60)
        integer(width,'width',16,2048); integer(height,'height',16,2048)
        if end<start or end-start+1>60: raise ValueError('Frame range must contain 1 to 60 frames')
        return {'ok':True,'dry_run':True,'written':False,'output_dir':str(target),
                'frames':list(range(start,end+1)),'fps':fps,'width':width,'height':height,
                'uncompressed_rgba_bytes':(end-start+1)*width*height*4,
                'explanation':'Would sample the stopped timeline and capture the current viewport. Restore time afterward; never save scene edits.',
                'warnings':['Changing capture dimensions resizes the output; it does not increase renderer detail.']}
    except (ValueError,OSError,TypeError) as exc:
        return {'ok':False,'error':str(exc),'written':False}


def plan_turntable_render(stage, prim_path: str, project_dir: str, name: str = 'turntable_run', *,
                         seconds: int = 2, fps: int = 24, width: int = 320, height: int = 240) -> dict:
    """Combine an object-spin preview with a sequence plan; does not orbit or modify the viewport camera."""
    try:
        integer(seconds,'seconds',1,60); integer(fps,'fps',1,60)
        plan=plan_image_sequence(project_dir,name,start=0,end=seconds*fps-1,fps=fps,width=width,height=height)
        if not plan['ok']: return plan
        from .animation import turntable
        animation=turntable(stage,prim_path,seconds=seconds,start=0,dry_run=True)
        return {**plan,'ok':bool(animation.get('ok')),'animation_plan':animation,
                'apply_order':['Review and explicitly apply animation_turntable separately',
                               'Call render_sequence with dry_run=False after reviewing its preview'],
                'note':'Object spins about its own pivot; inspect pivot and camera framing first.'}
    except (ValueError,ImportError,RuntimeError) as exc:
        return {'ok':False,'error':str(exc),'written':False}


def render_sequence(project_dir: str, name: str = 'sequence_run', *, start: int = 0, end: int = 2,
                    fps: int = 24, width: int = 320, height: int = 240, dry_run: bool = True) -> dict:
    """Preview or asynchronously capture a bounded frame range, restoring the stopped timeline's original time."""
    plan=plan_image_sequence(project_dir,name,start=start,end=end,fps=fps,width=width,height=height)
    if not plan['ok'] or dry_run is not False: return plan
    try:
        if _TASKS: raise ValueError('An MCP capture sequence is already running')
        from . import kit_live_extras as extras
        usd=importlib.import_module('omni.usd'); app=importlib.import_module('omni.kit.app').get_app()
        timeline=importlib.import_module('omni.timeline').get_timeline_interface()
        stage=usd.get_context().get_stage()
        if stage is None or timeline.is_playing(): raise ValueError('An open stage and stopped/paused timeline are required')
        utility=extras._kit_module('omni.kit.viewport.utility')
        if utility is None or utility.get_active_viewport() is None: raise ValueError('No active viewport')
        try: loop=asyncio.get_running_loop()
        except RuntimeError: loop=asyncio.get_event_loop()  # Kit main-thread update callback, outside a coroutine
        if loop.is_closed(): raise RuntimeError('Kit event loop is closed')
        old_time=timeline.get_current_time()
        created=bundle(project_dir,name,{'sequence_plan.json':json_text(plan)},False)
        job_id=uuid.uuid4().hex
        state={'ok':True,'job_id':job_id,'status':'pending','captures':[],'output_dir':created['directory'],'written':True,'dry_run':False}
        _JOBS[job_id]=state
        while len(_JOBS)>50: _JOBS.pop(next(iter(_JOBS)))
        async def run():
            try:
                state['status']='running'
                async with asyncio.timeout(300):
                    for frame in plan['frames']:
                        if usd.get_context().get_stage()!=stage or timeline.is_playing():
                            raise RuntimeError('Active stage or playback state changed; capture stopped')
                        timeline.set_current_time(frame/fps)
                        await app.next_update_async(); await app.next_update_async()
                        result=extras.capture_viewport(created['directory'],width=width,height=height)
                        if not result.get('ok'): raise RuntimeError(str(result))
                        deadline=time.monotonic()+20
                        while True:
                            await app.next_update_async()
                            ready=extras.capture_status(result['path'])
                            if not ready.get('ok'): raise RuntimeError(str(ready))
                            if ready.get('ready'):
                                state['captures'].append({'frame':frame,'capture':ready}); break
                            if time.monotonic()>deadline: raise TimeoutError('Capture did not complete in 20 seconds')
                state['status']='completed'
            except asyncio.CancelledError: state['status']='cancelled'
            except Exception as exc: state.update(status='failed',error=str(exc)[:600])
            finally:
                if usd.get_context().get_stage()==stage and not timeline.is_playing(): timeline.set_current_time(old_time)
                _TASKS.pop(job_id,None)
        _TASKS[job_id]=loop.create_task(run())
        def finished(task):
            _TASKS.pop(job_id,None)
            if task.cancelled(): state['status']='cancelled'
            elif task.exception() is not None: state.update(status='failed',error=str(task.exception())[:600])
        _TASKS[job_id].add_done_callback(finished)
        return dict(state)
    except (ImportError,ValueError,OSError,RuntimeError) as exc:
        return {'ok':False,'error':str(exc),'written':False}


def render_job_status(job_id: str) -> dict:
    """Read the status of one capture sequence in this bridge process."""
    return dict(_JOBS[job_id]) if job_id in _JOBS else {'ok':False,'error':'Unknown capture job'}


def render_cancel(job_id: str, *, dry_run: bool = True) -> dict:
    """Preview or cancel an MCP-owned sequence; retain completed captures and restore time during cleanup."""
    if job_id not in _TASKS: return {'ok':False,'error':'No active capture job with this ID'}
    if dry_run is False: _TASKS[job_id].cancel()
    return {'ok':True,'dry_run':dry_run is not False,'cancel_requested':dry_run is False}


def compare_screenshots(a: str, b: str, *, tolerance: int = 0) -> dict:
    """Compare at most one megapixel of 8-bit RGB/RGBA PNGs, with pixel-change percent and a 4x4 region summary."""
    try:
        from .dataset import _image
        integer(tolerance,'tolerance',0,255)
        shape,pixels_a=_image(Path(a),decode=True); other,pixels_b=_image(Path(b),decode=True)
        if shape!=other: raise ValueError('Image dimensions must match')
        width,height=shape
        if width*height>1_048_576: raise ValueError('Use images up to one megapixel for exact comparison')
        changed=0; regions=[0]*16; totals=[0]*16
        for pixel in range(width*height):
            x=pixel%width; y=pixel//width; region=min(3,y*4//height)*4+min(3,x*4//width); totals[region]+=1
            if any(abs(pixels_a[pixel*4+c]-pixels_b[pixel*4+c])>tolerance for c in range(4)):
                changed+=1; regions[region]+=1
        return {'ok':True,'read_only':True,'dimensions':shape,'changed_pixels':changed,
                'changed_percent':round(100*changed/(width*height),3),
                'regions':[{'column':i%4,'row':i//4,'changed_percent':round(100*n/totals[i],3)}
                           for i,n in enumerate(regions) if n],
                'note':'Exact pixel difference, not a perceptual-quality or semantic similarity score.'}
    except (ValueError,OSError,TypeError,zlib.error) as exc:
        return {'ok':False,'error':str(exc),'written':False}
