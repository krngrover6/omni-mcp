"""Run manually in Isaac Script Editor; stop the test session's active Replicator run."""
import asyncio
from pathlib import Path
import traceback
import omni.usd
import omni.replicator.core as rep


async def stop_test_run():
    stage = omni.usd.get_context().get_stage()
    expected = Path('C:/Users/krngr/Omniverse/mcp_day5_validation/root.usda').resolve()
    if stage is None or Path(stage.GetRootLayer().realPath or '').resolve() != expected:
        raise RuntimeError('Dedicated test project is not open; nothing stopped.')
    print('[MCP test] Stopping the sensor test Replicator run...')
    await asyncio.wait_for(rep.orchestrator.stop_async(), 30)
    print(f'[MCP test] Replicator ready: {rep.orchestrator.get_status()}')


task = asyncio.ensure_future(stop_test_run())
def finished(job):
    if not job.cancelled() and job.exception() is not None:
        traceback.print_exception(job.exception())
task.add_done_callback(finished)
