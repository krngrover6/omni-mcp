"""Direct MCP Composer checks; require the dedicated project and preserve saved layers."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import time
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parent))
from mcp_operability import PROJECT,unpack
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main():
    stamp=time.strftime('%Y%m%d_%H%M%S');report={'started':time.time(),'calls':[],'checks':{}}
    output=PROJECT/'docs'/('mcp_composer_'+stamp+'.json')
    files=[PROJECT/'root.usda',*(PROJECT/'layers').glob('*.usda')]
    def hashes():return {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    before_files=hashes()
    def save():output.write_text(json.dumps(report,indent=2),encoding='utf-8')
    async with streamablehttp_client('http://127.0.0.1:9905/mcp',timeout=20) as (read,write,_):
        async with ClientSession(read,write) as session:
            await session.initialize()
            async def call(name,args=None):
                data=unpack(await session.call_tool('router_call_tool',{'name':name,'arguments':args or {}}))
                report['calls'].append({'tool':name,'arguments':args or {},'result':data});save()
                print(name,data.get('ok','returned'),flush=True);return data
            caps=await call('scene_capabilities');summary=await call('scene_summary')
            if caps.get('backend',{}).get('endpoint')!='http://127.0.0.1:9910':
                raise RuntimeError('Router is not pointed at Composer; no writes attempted.')
            if not summary.get('stage_name','').replace('\\','/').endswith('/mcp_day5_validation/root.usda'):
                raise RuntimeError('Dedicated test project is not open; no writes attempted.')
            report['checks']['live_composer']=True
            report['baseline']=summary
            for name,args in [('scene_stage_info',{}),('graph_inspect',{}),('variant_list',{}),
                ('rubric_check',{'rubric':{'rules':[{'id':'world','kind':'prim','path':'/World','type':'Xform'}]}})]:
                data=await call(name,args);report['checks'][name]=data.get('ok',True) is not False and data.get('passed',True)
            labels_before=await call('semantic_audit')
            args={'mapping':{'/World/Racks/Crate_01/Geom':'composer_probe'}}
            preview=await call('semantic_labels',args)
            applied=await call('semantic_labels',{**args,'dry_run':False})
            try:
                observed=await call('semantic_audit')
                report['checks']['label_apply']=applied.get('applied') and 'composer_probe' in str(observed.get('labelled_prims'))
                report['checks']['label_preview']=preview.get('dry_run') and not preview.get('written')
            finally:
                if applied.get('applied'):await call('scene_undo_last',{'dry_run':False})
            report['checks']['label_undo']=labels_before==await call('semantic_audit')
            # Own uniquely named cube and graph; clean up only those two paths even if graph setup fails.
            token=uuid.uuid4().hex[:8];cube='/World/ComposerProbe_'+token;graph='/World/ComposerGraph_'+token
            listener=None
            try:
                await call('scene_create',{'path':cube,'type_name':'Cube'})
                created=await call('scene_create',{'path':cube,'type_name':'Cube','dry_run':False})
                if created.get('ok') is False:raise RuntimeError(str(created))
                variant={'prim_path':cube,'name':'MCPTest','variants':['A','B'],'selection':'A'}
                await call('variant_create_set',variant)
                result=await call('variant_create_set',{**variant,'dry_run':False})
                report['checks']['variant_create']=result.get('ok',False)
                event='mcp_composer_'+token
                args={'name':'event_sets_variant','graph_path':graph,'options':{
                    'event_name':event,'prim':cube,'variant_set':'MCPTest','payload':'colors'}}
                await call('graph_apply',args)
                built=await call('graph_apply',{**args,'dry_run':False})
                report['checks']['graph_creation']=built.get('ok',False)
                listener=await call('event_listen',{'event_name':event,'timeout_s':10,'dry_run':False})
                await call('event_send',{'event_name':event,'payload':{'colors':'B'},'dry_run':False})
                await asyncio.sleep(1)
                if listener.get('listener_id'):
                    received=await call('event_status',{'listener_id':listener['listener_id']})
                    report['checks']['event_delivery']=received.get('received',False)
                selected=await call('scene_variants',{'path':cube})
                report['variant_readout']=selected
                report['checks']['event_driven_variant']=selected.get('selections',{}).get('MCPTest')=='B'
            finally:
                if listener and listener.get('listener_id'):await call('event_stop',{'listener_id':listener['listener_id'],'dry_run':False})
                for path in (graph,cube):
                    exists=await call('scene_inspect',{'path':path})
                    if exists.get('ok') is not False:
                        await call('scene_delete',{'path':path})
                        await call('scene_delete',{'path':path,'dry_run':False})
            cap=await call('render_capture',{'project_dir':str(PROJECT/'renders'),'name':'composer_capture_'+stamp,'dry_run':False})
            if cap.get('path'):
                for _ in range(30):
                    state=await call('scene_capture_status',{'path':cap['path']})
                    if state.get('ready'):break
                    await asyncio.sleep(.4)
                report['checks']['capture_ready']=state.get('ready',False)
            else:report['checks']['capture_ready']=False
            report['final_summary']=await call('scene_summary')
    report['checks']['saved_files_unchanged']=before_files==hashes()
    report['passed']=all(report['checks'].values());report['finished']=time.time();save()
    print('REPORT',output);print(json.dumps(report['checks'],indent=2))


if __name__=='__main__':asyncio.run(main())
