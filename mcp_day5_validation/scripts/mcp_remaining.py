"""Direct MCP checks for remaining Isaac workflows; no bridge reloads or service changes."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_operability import PROJECT, WORKSPACE, unpack
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


def build_robot():
    from pxr import Gf, Usd, UsdGeom, UsdPhysics
    path = PROJECT/'assets/mcp_probe_robot.usda'
    if path.exists(): return path
    stage = Usd.Stage.CreateNew(str(path))
    UsdGeom.SetStageUpAxis(stage, 'Z'); UsdGeom.SetStageMetersPerUnit(stage, 1)
    robot = UsdGeom.Xform.Define(stage, '/Robot'); stage.SetDefaultPrim(robot.GetPrim())
    for name, pos in [('Base', (0,0,2)), ('Link',(.3,0,2))]:
        body = UsdGeom.Cube.Define(stage, '/Robot/'+name); body.CreateSizeAttr(.15)
        UsdGeom.Xformable(body).AddTranslateOp().Set(Gf.Vec3d(*pos))
        UsdPhysics.RigidBodyAPI.Apply(body.GetPrim()); UsdPhysics.CollisionAPI.Apply(body.GetPrim())
        UsdPhysics.MassAPI.Apply(body.GetPrim()).CreateMassAttr(1)
    fixed = UsdPhysics.FixedJoint.Define(stage, '/Robot/Fixed')
    fixed.CreateBody1Rel().SetTargets(['/Robot/Base'])
    fixed.CreateLocalPos0Attr(Gf.Vec3f(0,0,2))
    UsdPhysics.ArticulationRootAPI.Apply(fixed.GetPrim())
    hinge = UsdPhysics.RevoluteJoint.Define(stage, '/Robot/Hinge')
    hinge.CreateBody0Rel().SetTargets(['/Robot/Base']); hinge.CreateBody1Rel().SetTargets(['/Robot/Link'])
    hinge.CreateAxisAttr('Z'); hinge.CreateLowerLimitAttr(-90); hinge.CreateUpperLimitAttr(90)
    hinge.CreateLocalPos0Attr(Gf.Vec3f(.15,0,0)); hinge.CreateLocalPos1Attr(Gf.Vec3f(-.15,0,0))
    drive = UsdPhysics.DriveAPI.Apply(hinge.GetPrim(), 'angular')
    drive.CreateStiffnessAttr(100); drive.CreateDampingAttr(20)
    drive.CreateMaxForceAttr(100); drive.CreateTargetPositionAttr(0)
    stage.GetRootLayer().Save()
    return path


async def main():
    stamp = time.strftime('%Y%m%d_%H%M%S')
    report = {'started':time.time(), 'checks':[], 'observations':{}}
    output = PROJECT/'docs'/('mcp_remaining_'+stamp+'.json')
    scene_files = [PROJECT/'root.usda', *(PROJECT/'layers').glob('*.usda')]
    def hashes(): return {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in scene_files}
    original_files = hashes()
    def save(): output.write_text(json.dumps(report, indent=2, default=str), encoding='utf-8')
    async with streamablehttp_client('http://127.0.0.1:9905/mcp', timeout=20) as (read,write,_):
        async with ClientSession(read,write) as session:
            await session.initialize()
            async def call(name,args=None):
                result = unpack(await session.call_tool('router_call_tool',{'name':name,'arguments':args or {}}))
                report['checks'].append({'tool':name,'arguments':args or {},'result':result})
                save(); print(name, result.get('status',result.get('ok','returned')),flush=True)
                return result
            async def poll(tool, job, limit=120):
                deadline=time.monotonic()+limit
                while time.monotonic()<deadline:
                    state=await call(tool,{'job_id':job})
                    if state.get('status') in ('completed','failed','cancelled','succeeded'): return state
                    await asyncio.sleep(.4)
                return {'ok':False,'error':'poll timeout','job_id':job}
            summary=await call('scene_summary')
            if not summary.get('stage_name','').replace('\\','/').endswith('/mcp_day5_validation/root.usda'):
                raise RuntimeError('Dedicated test project is not open; no writes attempted.')
            sim=await call('sim_status')
            if sim.get('playing') or sim.get('is_playing') or sim.get('state')=='playing':
                raise RuntimeError('Existing simulation is playing; no writes attempted.')
            # Existing own fixture converted through the MCP importer, without placing it in the stage.
            args={'source_path':str(PROJECT/'assets/triangle.obj'),'project_dir':str(PROJECT),
                  'name':'mcp_import_'+stamp,'as_reference':False}
            await call('asset_import',args)
            imported=await call('asset_import',{**args,'dry_run':False})
            if imported.get('job_id'):
                report['observations']['mesh_conversion']=await poll('asset_import_status',imported['job_id'])
            # Full robot motion path: author one reference, observe PhysX, command it, observe motion, undo.
            path='/World/MCPProbeRobot_'+uuid.uuid4().hex[:6]
            fixture=build_robot()
            args={'prim_path':path,'asset_path':str(fixture)}
            await call('scene_add_asset',args)
            added=await call('scene_add_asset',{**args,'dry_run':False})
            try:
                if added.get('ok') is not False:
                    await call('robot_joint_table',{'robot_root':path})
                    await call('sim_play'); await asyncio.sleep(1)
                    before=await call('robot_joint_state',{'robot_root':path+'/Fixed'})
                    await call('robot_live_move',{'robot_root':path+'/Fixed','targets':{'Hinge':25}})
                    move=await call('robot_live_move',{'robot_root':path+'/Fixed','targets':{'Hinge':25},'dry_run':False})
                    await asyncio.sleep(2)
                    after=await call('robot_joint_state',{'robot_root':path+'/Fixed'})
                    report['observations']['robot_motion']={'before':before,'after':after,'command':move,
                        'passed':bool(before.get('joints') and after.get('joints')) and
                        abs(after['joints'][0]['position']-before['joints'][0]['position'])>1}
                    # Read real camera data through MCP while playing.
                    sensor={}
                    for _ in range(30):
                        sensor=await call('sensor_read',{'prim_path':'/World/Cameras/Overview','resolution':[64,64]})
                        if sensor.get('ready') or sensor.get('ok') is False: break
                        await asyncio.sleep(.3)
                    report['observations']['camera_ready']=sensor.get('ready',False)
                    await call('sensor_release',{'prim_path':'/World/Cameras/Overview','dry_run':False})
            finally:
                await call('sim_stop')
                if added.get('ok') is not False:
                    report['observations']['robot_undo']=await call('scene_undo_last',{'dry_run':False})
                    if report['observations']['robot_undo'].get('ok') is False:
                        # PhysX may author runtime opinions after creation. Never weaken the stale-undo guard.
                        # This unique path belongs to this run, so delete only that fixture explicitly.
                        await call('scene_delete',{'path':path})
                        report['observations']['robot_explicit_cleanup']=await call('scene_delete',{'path':path,'dry_run':False})
            # RGB and annotations: restore label via undo after the owned job ends.
            labels=await call('semantic_labels',{'mapping':{'/World/Racks/Crate_01/Geom':'crate'},'dry_run':False})
            try:
                args={'project_dir':str(PROJECT/'renders'),'name':'mcp_labels_'+stamp,
                      'camera':'/World/Cameras/Overview','frames':2,'width':128,'height':128,
                      'annotations':['rgb','semantic_segmentation','bounding_box_2d_tight']}
                await call('sdg_start',args)
                job=await call('sdg_start',{**args,'dry_run':False})
                if job.get('job_id'):
                    state=await poll('sdg_status',job['job_id'])
                    report['observations']['label_capture']=state
                    if state.get('status')=='completed':
                        report['observations']['label_dataset']=await call('dataset_check',{
                            'directory':state['output_dir'],'expected_frames':2,'width':128,'height':128,
                            'required_modalities':['rgb','semantic_segmentation','bounding_box_2d_tight']})
                else: report['observations']['label_capture']=job
            finally:
                if labels.get('applied'): await call('scene_undo_last',{'dry_run':False})
            report['observations']['final_summary']=await call('scene_summary')
    report['scene_files_unchanged']=hashes()==original_files
    report['finished']=time.time(); save(); print('REPORT',output,flush=True)


if __name__=='__main__': asyncio.run(main())
