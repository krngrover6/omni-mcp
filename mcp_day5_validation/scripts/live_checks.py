"""Run in Isaac/Composer Script Editor ONLY with this project's root.usda already open.

No bridge reload, router restart, installation, stage switch or scene save occurs.
Edits go into a disposable session sublayer. Results and captures stay in this project.
"""
from __future__ import annotations
import asyncio
import hashlib
import importlib
import json
import math
from pathlib import Path
import sys
import time
import uuid

PROJECT = Path(__file__).resolve().parents[1]
# Kit's Script Editor executes a temporary copy, so __file__ may point into Temp.
if not (PROJECT / 'root.usda').is_file():
    PROJECT = Path('C:/Users/krngr/Omniverse/mcp_day5_validation')
WORKSPACE = PROJECT.parent
SOURCE = WORKSPACE / 'omniverse_scene_mcp/src'
if str(SOURCE) not in sys.path: sys.path.insert(0,str(SOURCE))
importlib.invalidate_caches()


def hashes():
    return {str(p.relative_to(PROJECT)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [PROJECT/'root.usda',*(PROJECT/'layers').glob('*.usda')]}


async def run_checks():
    import omni.kit.app
    import omni.usd
    import omni.timeline
    from pxr import Gf, Sdf, UsdGeom, UsdPhysics
    from omniverse_scene_mcp import (dataset, import_helper, joint_control, message_bus, omnigraph_helper,
                                    render_helper, ros2_check, rubric, sdg, semantic_labels, sensors)
    app=omni.kit.app.get_app(); ctx=omni.usd.get_context(); stage=ctx.get_stage()
    report={'started':time.time(),'results':[],'project':str(PROJECT)}
    stamp=time.strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:6]
    output=PROJECT/'docs'/f'live_checks_{stamp}.json'
    def record(name,status,evidence):
        report['results'].append({'name':name,'status':status,'evidence':evidence})
        output.write_text(json.dumps(report,indent=2,default=str),encoding='utf-8')
        print(f'[MCP validation] {name}: {status}')
    if stage is None or Path(stage.GetRootLayer().realPath or '').resolve()!=(PROJECT/'root.usda').resolve():
        record('stage_guard','blocked','Open this project root.usda first; current stage was left untouched.')
        return
    timeline=omni.timeline.get_timeline_interface()
    if not timeline.is_stopped():
        record('timeline_guard','blocked','Stop playback first; no tests ran.')
        return
    before=hashes(); original_target=stage.GetEditTarget(); session=stage.GetSessionLayer()
    original_sublayers=list(session.subLayerPaths); transient=Sdf.Layer.CreateAnonymous('mcp_validation_live.usda')
    session.subLayerPaths=[transient.identifier]+original_sublayers
    stage.SetEditTarget(transient)
    prefix='/World/MCPValidation'
    sensor_path='/World/Cameras/Overview'
    async def updates(count):
        for _ in range(count): await app.next_update_async()
    try:
        # Structural rules are evaluated on the actual open stage, with a byte-identical preview proof.
        spec={'title':'Warehouse smoke check','rules':[
            {'id':'world','kind':'prim','path':'/World','type':'Xform'},
            {'id':'camera','kind':'prim','path':sensor_path,'type':'Camera'},
            {'id':'units','kind':'metadata','name':'metersPerUnit','equals':1.0},
            {'id':'crate','kind':'count','path':'/World','type':'Cube','gte':1}]}
        result=rubric.check_rubric(stage,spec)
        record('live_rubric','pass' if result.get('passed') else 'fail',result)
        crate='/World/Racks/Crate_01/Geom'; snapshot=transient.ExportToString()
        preview=semantic_labels.add_semantic_labels(stage,{crate:'crate'})
        assert transient.ExportToString()==snapshot and preview['dry_run']
        labels=semantic_labels.add_semantic_labels(stage,{crate:'crate'},dry_run=False)
        record('semantic_labels','pass' if labels.get('ok') else 'fail',labels)

        # The event listener and graph use a unique event name, never a user's event.
        listener_id=None
        try:
            test=UsdGeom.Cube.Define(stage,prefix+'/VariantCube').GetPrim()
            variants=test.GetVariantSets().AddVariantSet('Test')
            variants.AddVariant('A'); variants.AddVariant('B'); variants.SetVariantSelection('A')
            event='mcp_validation_'+uuid.uuid4().hex
            graph=omnigraph_helper.apply_recipe(stage,'event_sets_variant',graph_path=prefix+'/Graph',
                        event_name=event,prim=str(test.GetPath()),variant_set='Test',payload='colors',dry_run=False)
            record('action_graph_creation','pass' if graph.get('ok') else 'fail',graph)
            listener=message_bus.start_listener(event,timeout_s=10)
            if not listener.get('ok'): raise RuntimeError(str(listener))
            listener_id=listener['listener_id']
            await updates(5)
            sent=message_bus.send_event(event,payload={'colors':'B'},dry_run=False)
            await updates(15)
            received=message_bus.listener_status(listener_id)
            record('message_bus_delivery','pass' if received.get('received') else 'fail',{'sent':sent,'received':received})
            selected=variants.GetVariantSelection()
            record('action_graph_evaluation','pass' if selected=='B' else 'fail',{'actual':selected,'expected':'B'})
        except Exception as exc: record('action_graph_and_events','fail',str(exc))
        finally:
            if listener_id: message_bus.stop_listener(listener_id)

        # Verify the installed asset converter on a tiny, locally authored OBJ.
        try:
            converted=import_helper.import_asset(str(PROJECT/'assets/triangle.obj'),project_dir=str(PROJECT),
                           name='triangle_'+stamp,as_reference=False,dry_run=False)
            deadline=time.monotonic()+120
            while converted.get('status')=='running' and time.monotonic()<deadline:
                await updates(2)
                converted=import_helper.import_status(converted['job_id'])
            record('mesh_conversion','pass' if converted.get('status')=='succeeded' else 'fail',converted)
            record('proprietary_cad_conversion','blocked','No licensed CAD fixture supplied; OBJ conversion is a separate check.')
        except Exception as exc: record('mesh_conversion','fail',str(exc))

        # A minimal articulation with one driven revolute DOF; no downloaded robot or user stage is needed.
        try:
            physics=UsdPhysics.Scene.Define(stage,prefix+'/Physics')
            physics.CreateGravityMagnitudeAttr().Set(0.0)
            robot=prefix+'/Robot'; UsdGeom.Xform.Define(stage,robot)
            for name,pos in (('Base',(0,0,1)),('Link',(.3,0,1))):
                body=UsdGeom.Cube.Define(stage,robot+'/'+name)
                body.CreateSizeAttr().Set(.15)
                UsdGeom.Xformable(body).AddTranslateOp().Set(Gf.Vec3d(*pos))
                UsdPhysics.RigidBodyAPI.Apply(body.GetPrim()); UsdPhysics.CollisionAPI.Apply(body.GetPrim())
                UsdPhysics.MassAPI.Apply(body.GetPrim()).CreateMassAttr().Set(1.0)
            fixed=UsdPhysics.FixedJoint.Define(stage,robot+'/Fixed')
            fixed.CreateBody1Rel().SetTargets([robot+'/Base'])
            UsdPhysics.ArticulationRootAPI.Apply(fixed.GetPrim())
            hinge=UsdPhysics.RevoluteJoint.Define(stage,robot+'/Hinge')
            hinge.CreateBody0Rel().SetTargets([robot+'/Base']); hinge.CreateBody1Rel().SetTargets([robot+'/Link'])
            hinge.CreateAxisAttr().Set('Z'); hinge.CreateLowerLimitAttr().Set(-90); hinge.CreateUpperLimitAttr().Set(90)
            hinge.CreateLocalPos0Attr().Set(Gf.Vec3f(.15,0,0)); hinge.CreateLocalPos1Attr().Set(Gf.Vec3f(-.15,0,0))
            drive=UsdPhysics.DriveAPI.Apply(hinge.GetPrim(),'angular')
            drive.CreateStiffnessAttr().Set(100); drive.CreateDampingAttr().Set(20)
            drive.CreateMaxForceAttr().Set(100); drive.CreateTargetPositionAttr().Set(0)
            timeline.play(); await updates(45)
            root=robot+'/Fixed'
            initial=joint_control.live_joint_state(root)
            if not initial.get('ok'): raise RuntimeError(str(initial))
            dry=joint_control.live_move(root,{'Hinge':25},dry_run=True)
            moved=joint_control.live_move(root,{'Hinge':25},dry_run=False)
            await updates(90)
            final=joint_control.live_joint_state(root)
            observed=bool(final.get('joints')) and abs(final['joints'][0]['position']-initial['joints'][0]['position'])>1
            record('robot_motion','pass' if moved.get('ok') and observed else 'fail',
                   {'preview':dry,'command':moved,'before':initial,'after':final})
        except Exception as exc: record('robot_motion','fail',str(exc))

        # Actual camera samples must be ready and contain image statistics; API construction alone is not a pass.
        try:
            if not timeline.is_playing(): timeline.play()
            sample={}; deadline=time.monotonic()+30
            while time.monotonic()<deadline:
                sample=sensors.read_sensor_live(sensor_path,resolution=[64,64])
                if sample.get('ready'): break
                if not sample.get('ok'): break
                await updates(5)
            record('camera_sensor_reading','pass' if sample.get('ok') and sample.get('ready') else 'fail',sample)
        except Exception as exc: record('camera_sensor_reading','fail',str(exc))
        finally:
            sensors.release_sensor_live(sensor_path)
            timeline.stop(); await updates(5)

        # Capture an actual viewport image, then run a tiny Replicator writer job and validate its files.
        try:
            capture=render_helper.render_capture(str(PROJECT/'renders'),'capture_'+stamp,dry_run=False)
            from omniverse_scene_mcp.kit_live_extras import capture_status
            deadline=time.monotonic()+30; state=capture
            if capture.get('ok'):
                while time.monotonic()<deadline:
                    await updates(2); state=capture_status(capture['path'])
                    if state.get('ready'): break
            record('viewport_capture','pass' if state.get('ready') else 'fail',state)
        except Exception as exc: record('viewport_capture','fail',str(exc))
        try:
            job=sdg.start_sdg(str(PROJECT/'renders'),'sdg_'+stamp,camera=sensor_path,frames=2,width=64,height=64,dry_run=False)
            deadline=time.monotonic()+120; state=job
            while job.get('job_id') and time.monotonic()<deadline:
                await updates(2); state=sdg.sdg_status(job['job_id'])
                if state.get('status') in ('completed','failed','cancelled'): break
            record('replicator_capture','pass' if state.get('status')=='completed' else 'fail',state)
            if state.get('status')=='completed':
                checked=dataset.check_dataset(state['output_dir'],expected_frames=2,width=64,height=64)
                record('generated_dataset_validation','pass' if checked.get('passed') else 'fail',checked)
        except Exception as exc: record('replicator_capture','fail',str(exc))

        # Readiness is not connectivity. No ROS install, environment change or arbitrary shell command is attempted.
        ros=ros2_check.environment_check(probe_cli=True,workspace=WORKSPACE)
        record('ros2_readiness','observed',ros)
        record('ros2_connectivity','blocked','A local ROS publisher/subscriber fixture has not been configured; readiness is not proof of delivery.')
    finally:
        timeline.stop()
        stage.SetEditTarget(original_target)
        session.subLayerPaths=original_sublayers
        report['scene_files_unchanged']=hashes()==before
        report['finished']=time.time()
        report['passed']=report['scene_files_unchanged'] and all(r['status']=='pass' for r in report['results'])
        output.write_text(json.dumps(report,indent=2,default=str),encoding='utf-8')
        print(f'[MCP validation] Full results: {output}')


if __name__=='__main__' or 'omni.kit.app' in sys.modules:
    # Script Editor is already on Kit's running event loop; never use asyncio.run here.
    print(f'[MCP validation] Starting checks for {PROJECT}')
    task=asyncio.ensure_future(run_checks())
    def _finished(job):
        if not job.cancelled() and job.exception() is not None:
            import traceback
            print('[MCP validation] ERROR:')
            traceback.print_exception(job.exception())
    task.add_done_callback(_finished)
