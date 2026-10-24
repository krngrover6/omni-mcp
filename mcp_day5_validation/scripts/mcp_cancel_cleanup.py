"""Cancel an owned SDG job through MCP, then prove a new job can run and list runtime path changes."""
import asyncio
import json
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parent))
from mcp_operability import PROJECT,unpack
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main():
    report={'calls':[]};stamp=time.strftime('%Y%m%d_%H%M%S')
    async with streamablehttp_client('http://127.0.0.1:9905/mcp',timeout=20) as (read,write,_):
        async with ClientSession(read,write) as s:
            await s.initialize()
            async def call(n,a=None):
                r=unpack(await s.call_tool('router_call_tool',{'name':n,'arguments':a or {}}))
                report['calls'].append({'tool':n,'result':r});return r
            summary=await call('scene_summary')
            assert summary.get('stage_name','').replace('\\','/').endswith('/mcp_day5_validation/root.usda')
            async def paths(root):
                queue=[root];found=[]
                while queue and len(found)<100:
                    path=queue.pop();row=await call('scene_children',{'path':path})
                    if row.get('ok') is False: continue
                    found.append(path);queue.extend(row.get('children',[]))
                return sorted(found)
            before={p:await paths(p) for p in ['/World','/Render','/Replicator','/Orchestrator']}
            args={'project_dir':str(PROJECT/'renders'),'name':'mcp_cancel_'+stamp,
                'camera':'/World/Cameras/Overview','frames':100,'width':64,'height':64}
            await call('sdg_start',args)
            job=await call('sdg_start',{**args,'dry_run':False})
            if job.get('job_id'):
                preview=await call('sdg_cancel',{'job_id':job['job_id']})
                cancelled=await call('sdg_cancel',{'job_id':job['job_id'],'dry_run':False})
                for _ in range(50):
                    state=await call('sdg_status',{'job_id':job['job_id']})
                    if state.get('status') in ('cancelled','failed','completed'): break
                    await asyncio.sleep(.2)
                report['cancel_preview']=preview;report['cancel_request']=cancelled;report['cancel_result']=state
                report['cancel_passed']=state.get('status')=='cancelled'
                follow=await call('sdg_start',{**args,'name':'mcp_after_cancel_'+stamp,'frames':1,'dry_run':False})
                if follow.get('job_id'):
                    for _ in range(90):
                        result=await call('sdg_status',{'job_id':follow['job_id']})
                        if result.get('status') in ('completed','failed','cancelled'):break
                        await asyncio.sleep(.3)
                    report['follow_up']=result
                    if result.get('status')=='completed':
                        report['follow_up_dataset']=await call('dataset_check',{'directory':result['output_dir'],'expected_frames':1,'width':64,'height':64})
                else:report['follow_up']=follow
            else:report['start_failed']=job
            after={p:await paths(p) for p in before}
            report['paths_before']=before;report['paths_after']=after
            report['added_paths']={p:sorted(set(after[p])-set(before[p])) for p in before}
            report['removed_paths']={p:sorted(set(before[p])-set(after[p])) for p in before}
            report['world_paths_restored']=before['/World']==after['/World']
    output=PROJECT/'docs'/('mcp_cancel_cleanup_'+stamp+'.json')
    output.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print('REPORT',output);print(json.dumps({k:v for k,v in report.items() if k not in ('calls','paths_before','paths_after')},indent=2))


if __name__=='__main__':asyncio.run(main())
