"""External Python client: verify tools through MCP, never by directly calling their implementation."""
import asyncio
import json
from pathlib import Path
import sys
import time
import hashlib
import uuid

PROJECT = Path(__file__).resolve().parents[1]
WORKSPACE = PROJECT.parent
packages = WORKSPACE/'bowerbot-venv/Lib/site-packages'
sys.path[:0] = [str(packages/p) for p in ('', 'win32', 'win32/lib', 'pywin32_system32')]
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


def unpack(result):
    if result.structuredContent is not None:
        return result.structuredContent
    texts = [c.text for c in result.content if hasattr(c, 'text')]
    return json.loads(texts[0]) if len(texts) == 1 else {'error': texts}


async def main():
    report = {'started': time.time(), 'transport': 'MCP streamable HTTP on 9905', 'checks': []}
    output = PROJECT/'docs'/('mcp_operability_'+time.strftime('%Y%m%d_%H%M%S')+'.json')
    def save():
        output.write_text(json.dumps(report, indent=2, default=str), encoding='utf-8')
    async with streamablehttp_client('http://127.0.0.1:9905/mcp', timeout=15) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            report['visible_tools'] = len((await session.list_tools()).tools)
            async def call(name, args=None, verify=None):
                try:
                    result = await session.call_tool(name if name.startswith('router_') else 'router_call_tool',
                        (args or {}) if name.startswith('router_') else {'name':name, 'arguments':args or {}})
                    data = unpack(result)
                    passed = not result.isError and data.get('ok', True) is not False
                    if verify is not None: passed = passed and bool(verify(data))
                    report['checks'].append({'tool':name, 'arguments':args or {}, 'passed':passed, 'result':data})
                except Exception as exc:
                    data = {'ok':False, 'error':f'{type(exc).__name__}: {exc}'}
                    report['checks'].append({'tool':name, 'arguments':args or {}, 'passed':False, 'result':data})
                save()
                print(name, 'PASS' if report['checks'][-1]['passed'] else 'FAIL', flush=True)
                return data
            catalog = await call('router_list_tools', verify=lambda d:len(d['tools'])>=249)
            report['total_tools'] = len(catalog.get('tools', []))
            summary = await call('scene_summary')
            await call('scene_capabilities')
            await call('rubric_template')
            await call('robot_catalog')
            await call('sensor_catalog')
            await call('graph_recipes')
            await call('asset_import_formats')
            await call('ros2_environment', {'probe_cli':False})
            run = PROJECT/'renders/sdg_retry_20261023_011555_a055a9'
            await call('dataset_check', {'directory':str(run),'expected_frames':2,'width':64,'height':64}, lambda d:d.get('passed'))
            await call('sdg_plan', {'project_dir':str(PROJECT/'renders'),'name':'mcp_audit_preview','camera':'/World/Cameras/Overview','frames':2})
            await call('extension_create', {'project_dir':str(PROJECT),'name':'mcp_audit_extension'})
            stage = summary.get('stage_name', '')
            if stage.replace('\\','/').endswith('/mcp_day5_validation/root.usda'):
                scene_files = [PROJECT/'root.usda', *(PROJECT/'layers').glob('*.usda')]
                def hashes():
                    return {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in scene_files}
                files_before = hashes()
                for name,args in [
                    ('semantic_audit', {}), ('variant_list', {}), ('graph_inspect', {}),
                    ('sensor_list', {}), ('animation_info', {}), ('scene_frame_stats', {}),
                    ('rubric_check', {'rubric':{'rules':[{'id':'world','kind':'prim','path':'/World','type':'Xform'}]}}),
                    ('semantic_labels', {'mapping':{'/World/Racks/Crate_01/Geom':'crate'}}),
                    ('render_sequence_plan', {'project_dir':str(PROJECT/'renders'),'name':'mcp_sequence_preview','end':1}),
                ]:
                    await call(name,args)
                # Capture is authorized within the independent test project; it does not author scene edits.
                cap = await call('render_capture', {'project_dir':str(PROJECT/'renders'),
                    'name':'mcp_capture_'+time.strftime('%Y%m%d_%H%M%S'),'dry_run':False})
                if cap.get('path'):
                    for _ in range(20):
                        state = await call('scene_capture_status', {'path':cap['path']})
                        if state.get('ready'): break
                        await asyncio.sleep(1)
                    report['capture_ready'] = state.get('ready', False)
                # Apply and undo one actual label edit through MCP, then verify the readout.
                labels_before = await call('semantic_audit')
                applied = await call('semantic_labels', {'mapping':{'/World/Racks/Crate_01/Geom':'mcp_operability_probe'},'dry_run':False})
                try:
                    if applied.get('applied'):
                        await call('semantic_audit', verify=lambda d:'mcp_operability_probe' in str(d.get('labelled_prims')))
                finally:
                    if applied.get('applied'):
                        await call('scene_undo_last', {'dry_run':False})
                labels_after = await call('semantic_audit')
                report['labels_restored'] = labels_before == labels_after
                event = 'mcp_operability_'+uuid.uuid4().hex
                listener = await call('event_listen', {'event_name':event,'timeout_s':10,'dry_run':False})
                try:
                    if listener.get('listener_id'):
                        await call('event_send', {'event_name':event,'payload':{'probe':42},'dry_run':False})
                        await asyncio.sleep(.5)
                        await call('event_status', {'listener_id':listener['listener_id']}, lambda d:d.get('received') and d.get('payload',{}).get('probe')==42)
                finally:
                    if listener.get('listener_id'):
                        await call('event_stop', {'listener_id':listener['listener_id'],'dry_run':False})
                seq = await call('render_sequence', {'project_dir':str(PROJECT/'renders'),
                    'name':'mcp_sequence_'+time.strftime('%Y%m%d_%H%M%S'),'end':1,'width':64,'height':64,'dry_run':False})
                if seq.get('job_id'):
                    for _ in range(60):
                        state = await call('render_job_status', {'job_id':seq['job_id']})
                        if state.get('status') in ('completed','failed','cancelled'): break
                        await asyncio.sleep(1)
                    report['sequence_completed'] = state.get('status')=='completed' and len(state.get('captures',[]))==2
                    if report['sequence_completed']:
                        first = state['captures'][0]['capture']['path']
                        await call('render_compare', {'a':first,'b':first}, lambda d:d.get('changed_pixels')==0)
                job = await call('sdg_start', {'project_dir':str(PROJECT/'renders'),
                    'name':'mcp_sdg_'+time.strftime('%Y%m%d_%H%M%S'),'camera':'/World/Cameras/Overview',
                    'frames':2,'width':64,'height':64,'dry_run':False})
                if job.get('job_id'):
                    for _ in range(90):
                        state = await call('sdg_status', {'job_id':job['job_id']})
                        if state.get('status') in ('completed','failed','cancelled'): break
                        await asyncio.sleep(1)
                    report['sdg_completed'] = state.get('status')=='completed'
                    if report['sdg_completed']:
                        await call('dataset_check', {'directory':state['output_dir'],'expected_frames':2,'width':64,'height':64}, lambda d:d.get('passed'))
                report['scene_files_unchanged'] = hashes()==files_before
                after = await call('scene_summary')
                report['summary_unchanged'] = summary == after
            else:
                report['live_tests_blocked'] = 'Dedicated test stage is not open; no live writes attempted.'
            report['finished'] = time.time()
            report['passed_checks'] = sum(c['passed'] for c in report['checks'])
            report['failed_checks'] = sum(not c['passed'] for c in report['checks'])
            save()
            print('REPORT', output)
            print('COUNTS', report['passed_checks'], report['failed_checks'])


if __name__ == '__main__':
    asyncio.run(main())
