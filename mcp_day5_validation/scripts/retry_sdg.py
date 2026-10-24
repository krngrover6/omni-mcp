"""Script Editor: authorized Replicator stop and two-frame retry in this test project only."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import time
import traceback
import uuid

PROJECT = Path('C:/Users/krngr/Omniverse/mcp_day5_validation')
SOURCE = PROJECT.parent / 'omniverse_scene_mcp/src'
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))


async def retry():
    import omni.usd
    import omni.kit.app
    import omni.timeline
    import omni.replicator.core as rep
    from omniverse_scene_mcp import sdg, dataset
    stage = omni.usd.get_context().get_stage()
    if stage is None or Path(stage.GetRootLayer().realPath or '').resolve() != (PROJECT/'root.usda').resolve():
        raise RuntimeError('Open mcp_day5_validation/root.usda first; nothing was stopped or changed.')
    if sdg._TASKS:
        raise RuntimeError('An MCP dataset job is still running; wait for it before retrying.')
    app = omni.kit.app.get_app()
    stamp = time.strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:6]
    output = PROJECT/'docs'/('sdg_retry_'+stamp+'.json')
    layers = [PROJECT/'root.usda', *(PROJECT/'layers').glob('*.usda')]
    def hashes():
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in layers}
    before = hashes()
    report = {'started': time.time(), 'initial_status': str(rep.orchestrator.get_status())}
    job = None
    try:
        # User explicitly authorized stopping the active Replicator run for this retry.
        print('[MCP SDG] Stopping Replicator for the authorized retry...')
        await asyncio.wait_for(rep.orchestrator.stop_async(), timeout=30)
        omni.timeline.get_timeline_interface().stop()
        for _ in range(5):
            await app.next_update_async()
        if omni.usd.get_context().get_stage() != stage:
            raise RuntimeError('Active stage changed; retry cancelled.')
        report['stopped_status'] = str(rep.orchestrator.get_status())
        renders = PROJECT/'renders'
        renders.mkdir(exist_ok=True)
        args = dict(project_dir=str(renders), name='sdg_retry_'+stamp,
                    camera='/World/Cameras/Overview', frames=2, width=64, height=64)
        report['preview'] = sdg.start_sdg(**args)
        if not report['preview'].get('ok'):
            raise RuntimeError(str(report['preview']))
        job = sdg.start_sdg(**args, dry_run=False)
        report['start'] = job
        if not job.get('job_id'):
            raise RuntimeError(str(job))
        print('[MCP SDG] Capturing two frames...')
        deadline = time.monotonic()+320
        while time.monotonic() < deadline:
            await app.next_update_async()
            state = sdg.sdg_status(job['job_id'])
            if state.get('status') in ('completed', 'failed', 'cancelled'):
                break
        else:
            sdg.sdg_cancel(job['job_id'], dry_run=False)
            raise RuntimeError('Capture timed out; cancellation requested for this retry job.')
        report['capture'] = state
        if state.get('status') == 'completed':
            report['dataset'] = dataset.check_dataset(state['output_dir'], expected_frames=2, width=64, height=64)
        report['passed'] = bool(report.get('dataset', {}).get('passed'))
    except Exception as exc:
        report.update(passed=False, error=f'{type(exc).__name__}: {exc}')
        traceback.print_exc()
    finally:
        report['scene_files_unchanged'] = hashes() == before
        report['passed'] = report.get('passed', False) and report['scene_files_unchanged']
        report['finished'] = time.time()
        output.write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(f'[MCP SDG] Passed: {report["passed"]}. Full results: {output}')


print('[MCP SDG] Starting retry; existing scene files will not be saved.')
task = asyncio.ensure_future(retry())
def finished(job):
    if not job.cancelled() and job.exception() is not None:
        traceback.print_exception(job.exception())
task.add_done_callback(finished)
