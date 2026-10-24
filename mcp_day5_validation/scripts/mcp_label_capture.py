"""Direct MCP annotation check after the user stops the preceding sensor-test Replicator run."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_operability import PROJECT, unpack
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main():
    report={'calls':[],'started':time.time()}
    stamp=time.strftime('%Y%m%d_%H%M%S')
    output=PROJECT/'docs'/('mcp_labels_'+stamp+'.json')
    def save(): output.write_text(json.dumps(report,indent=2),encoding='utf-8')
    files=[PROJECT/'root.usda',*(PROJECT/'layers').glob('*.usda')]
    def hashes(): return {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    before=hashes()
    async with streamablehttp_client('http://127.0.0.1:9905/mcp',timeout=20) as (read,write,_):
        async with ClientSession(read,write) as s:
            await s.initialize()
            async def call(n,a=None):
                r=unpack(await s.call_tool('router_call_tool',{'name':n,'arguments':a or {}}))
                report['calls'].append({'tool':n,'result':r});save();print(n,r.get('status',r.get('ok')),flush=True)
                return r
            summary=await call('scene_summary')
            assert summary.get('stage_name','').replace('\\','/').endswith('/mcp_day5_validation/root.usda')
            old_labels=await call('semantic_audit')
            labels=await call('semantic_labels',{'mapping':{'/World/Racks/Crate_01/Geom':'crate'},'dry_run':False})
            try:
                args={'project_dir':str(PROJECT/'renders'),'name':'mcp_annotations_'+stamp,
                    'camera':'/World/Cameras/Overview','frames':2,'width':256,'height':256,
                    'annotations':['rgb','semantic_segmentation','bounding_box_2d_tight']}
                await call('sdg_start',args)
                job=await call('sdg_start',{**args,'dry_run':False})
                if job.get('job_id'):
                    for _ in range(180):
                        state=await call('sdg_status',{'job_id':job['job_id']})
                        if state.get('status') in ('completed','failed','cancelled'): break
                        await asyncio.sleep(.5)
                    report['capture']=state
                    if state.get('status')=='completed':
                        report['dataset']=await call('dataset_check',{'directory':state['output_dir'],
                            'expected_frames':2,'width':256,'height':256,
                            'required_modalities':['rgb','semantic_segmentation','bounding_box_2d_tight']})
                else: report['capture']=job
            finally:
                if labels.get('applied'): report['undo']=await call('scene_undo_last',{'dry_run':False})
            after_labels=await call('semantic_audit')
            report['entire_audit_unchanged']=old_labels==after_labels
            semantic_keys=('labelled_prims','class_histogram','unlabelled_geometry','unlabelled_count','ambiguous_geometry')
            report['labels_restored']=all(old_labels.get(k)==after_labels.get(k) for k in semantic_keys)
    report['scene_files_unchanged']=before==hashes()
    report['finished']=time.time();save();print('REPORT',output)


if __name__=='__main__':asyncio.run(main())
