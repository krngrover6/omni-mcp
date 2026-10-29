"""Day 5 artifacts, bounded datasets, declarative rubrics and Kit-only job refusal."""
import asyncio
import json
import struct
import tempfile
import tomllib
from pathlib import Path
from unittest.mock import patch
from omniverse_scene_mcp import blender_handoff, dataset, day5_io, extension_starter, features, render_helper, rubric, sdg


def _image(path, size=(32, 24)):
    import zlib
    def chunk(kind, data):
        return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data))
    width,height = size
    path.write_bytes(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,2,0,0,0))+
                     chunk(b'IDAT',zlib.compress((b'\0'+b'\xff\0\0'*width)*height))+chunk(b'IEND',b''))


def _array(path, descriptor='<u4', shape=(24,32), data=None):
    header = repr({'descr':descriptor,'fortran_order':False,'shape':shape}).encode()+b'\n'
    if data is None: data = bytes(24*32*4)
    path.write_bytes(b'\x93NUMPY\x01\x00'+struct.pack('<H',len(header))+header+data)


def test_all_artifact_previews_write_nothing_and_generated_scripts_compile():
    with tempfile.TemporaryDirectory() as folder:
        for flag in (True, None, 0, 'false', [], {}):
            assert extension_starter.extension_create(folder, dry_run=flag)['dry_run']
            assert sdg.start_sdg(folder, dry_run=flag)['dry_run']
            assert render_helper.render_capture(folder, dry_run=flag)['dry_run']
        for result in (sdg.sdg_script(folder), blender_handoff.blender_export_plan(folder)):
            assert result['ok'] and not result['executed']
            compile(result['script'], '<generated>', 'exec')
            assert 'DRY_RUN = True' in result['script']
        assert not list(Path(folder).iterdir())


def test_extension_bundle_is_inert_valid_python_toml_and_never_overwrites():
    with tempfile.TemporaryDirectory() as folder:
        result = extension_starter.extension_create(folder, dry_run=False)
        assert result['written'] and not result['dry_run']
        root = Path(result['directory'])
        config = tomllib.loads((root/'config/extension.toml').read_text())
        assert config['python']['module'][0]['name'] == 'study_extension'
        code = (root/'study_extension/__init__.py').read_text()
        compile(code, '<extension>', 'exec')
        assert 'DefinePrim' not in code and 'Save(' not in code
        before = {str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}
        assert not extension_starter.extension_create(folder, dry_run=False)['ok']
        assert before == {str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_generated_extension_shutdown_hides_retained_window_and_is_repeatable():
    import sys
    from types import ModuleType
    with tempfile.TemporaryDirectory() as folder:
        result=extension_starter.extension_create(folder,dry_run=False)
        code=(Path(result['directory'])/'study_extension/__init__.py').read_text()
        omni=ModuleType('omni');ext=ModuleType('omni.ext');ui=ModuleType('omni.ui');usd=ModuleType('omni.usd')
        ext.IExt=object;omni.ext=ext;omni.ui=ui;omni.usd=usd
        with patch.dict(sys.modules,{'omni':omni,'omni.ext':ext,'omni.ui':ui,'omni.usd':usd}):
            namespace={};exec(compile(code,'<generated extension>','exec'),namespace)
        instance=namespace['StudyExtension']()
        class RetainedWindow:
            visible=True
            destroyed=False
            def destroy(self):
                assert self.visible is False
                self.destroyed=True
        window=RetainedWindow();instance._window=window;instance._label=object()
        instance.on_shutdown()
        assert window.destroyed and not window.visible
        assert instance._window is None and instance._label is None
        instance.on_shutdown()  # safe when already shut down
        namespace['StudyExtension']().on_shutdown()  # safe if startup never finished


def test_output_names_and_numeric_arguments_are_strict_and_reserved_names_refused():
    with tempfile.TemporaryDirectory() as folder:
        for name in ('../escape', 'CON', 'COM1', 'x/y', 'x\\y', 'x\nprint(1)', ''):
            assert not extension_starter.extension_create(folder, name, dry_run=False)['ok'], name
            assert not sdg.plan_sdg(folder, name)['ok'], name
        for field, value in (('frames', True), ('frames', 101), ('width', 10), ('height', '20'), ('seed', -1)):
            assert not sdg.plan_sdg(folder, **{field:value})['ok']
        assert not sdg.plan_sdg(folder, annotations=['rgb', 'unknown'])['ok']
        assert not render_helper.render_plan(folder, width=False)['ok']
        assert not blender_handoff.blender_export_plan(folder, animation='false')['ok']
        assert not list(Path(folder).iterdir())


def test_bundle_rolls_back_only_its_files_after_a_partial_write():
    with tempfile.TemporaryDirectory() as folder:
        original = Path.open
        def fail(path, *args, **kwargs):
            if path.name == 'second.txt': raise OSError('simulated disk error')
            return original(path, *args, **kwargs)
        try:
            with patch.object(Path, 'open', fail):
                day5_io.bundle(folder, 'new_project', {'first.txt':'a', 'second.txt':'b'}, False)
        except OSError: pass
        else: raise AssertionError('Expected write failure')
        assert not list(Path(folder).iterdir())


def test_dataset_detects_valid_rgb_and_missing_or_corrupt_frames_without_writing():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        _image(root/'rgb_0000.png'); _image(root/'rgb_0001.png')
        (root/'labels.json').write_text('{"1":{"class":"box"}}')
        before = {p.name:p.read_bytes() for p in root.iterdir()}
        result = dataset.check_dataset(folder, expected_frames=2, width=32, height=24)
        assert result['ok'] and result['passed'] and result['counts']['rgb'] == 2
        assert before == {p.name:p.read_bytes() for p in root.iterdir()}
        assert not dataset.check_dataset(folder, expected_frames=3)['passed']
        assert not dataset.check_dataset(folder, width=320, height=240)['passed']
        assert not dataset.check_dataset(folder, max_files=1)['passed']
        assert not dataset.check_dataset(folder, required_modalities=['rgb', 'semantic_segmentation'])['passed']
        (root/'rgb_0002.png').write_bytes(b'not png')
        (root/'labels.json').write_text('{broken')
        result = dataset.check_dataset(folder)
        assert result['issue_count'] >= 2 and not result['passed']


def test_dataset_rejects_pickle_arrays_and_truncated_numeric_arrays():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        _image(root/'rgb_0000.png')
        _array(root/'semantic_segmentation_0000.npy')
        args = dict(required_modalities=['rgb', 'semantic_segmentation'], expected_frames=1)
        assert dataset.check_dataset(folder, **args)['passed']
        _array(root/'semantic_segmentation_0000.npy',descriptor='|O',shape=(1,),data=b'pickle refused')
        assert not dataset.check_dataset(folder, **args)['passed']
        _array(root/'semantic_segmentation_0000.npy')
        p = root/'semantic_segmentation_0000.npy'; p.write_bytes(p.read_bytes()[:-1])
        assert not dataset.check_dataset(folder, **args)['passed']


def _stage():
    try:
        from pxr import Usd, UsdGeom
    except ImportError:
        import unittest
        raise unittest.SkipTest('pxr unavailable')
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, '/World'); UsdGeom.Camera.Define(stage, '/World/Camera')
    UsdGeom.Cube.Define(stage, '/World/Box')
    stage.SetMetadata('metersPerUnit', 1.0)
    return stage


def test_custom_rubric_scores_structure_without_authoring_and_rejects_executable_rules():
    stage = _stage(); before = [l.ExportToString() for l in stage.GetLayerStack()]
    spec = rubric.rubric_template()['rubric']
    result = rubric.check_rubric(stage, spec)
    assert result['ok'] and result['passed'] and result['score_percent'] == 100
    stage.RemovePrim('/World/Camera')
    assert rubric.check_rubric(stage, spec)['score_percent'] == 80
    snapshot = [l.ExportToString() for l in stage.GetLayerStack()]
    assert not rubric.check_rubric(stage, {'rules':[{'id':'x','kind':'python','code':'raise Exception()'}]})['ok']
    assert not rubric.check_rubric(stage, {'rules':[{'id':'x','kind':'prim','path':'relative'}]})['ok']
    assert not rubric.check_rubric(stage, spec, max_prims=1)['ok']
    assert snapshot == [l.ExportToString() for l in stage.GetLayerStack()]


def test_day5_tool_registration_profiles_and_natural_language_safety():
    from omniverse_scene_mcp.server import build_server
    from omniverse_scene_mcp import nl_tier8
    names = {'dataset_check','sdg_plan','sdg_script','sdg_start','sdg_status','sdg_cancel','render_plan',
             'render_capture','blender_export_plan','blender_handoff_check','extension_create','rubric_template','rubric_check'}
    assert names <= set(features.FEATURES)
    with patch.dict('os.environ', {'OMNIVERSE_MCP_PROFILE':'all'}): app = build_server()
    assert names <= {t.name for t in app._tool_manager.list_tools()}
    for name in ('sdg_start','sdg_cancel','render_capture','extension_create'):
        assert app._tool_manager.get_tool(name).parameters['properties']['dry_run']['default'] is True
        assert nl_tier8.parse(name + ' {"dry_run":false}')['arguments']['dry_run'] is True
    result = asyncio.run(app._tool_manager.get_tool('rubric_template').run({}))
    assert result['ok'] and result['rubric']['rules']


def test_kit_operations_fail_closed_when_kit_is_unavailable():
    with tempfile.TemporaryDirectory() as folder:
        assert not sdg.start_sdg(folder, dry_run=False)['ok']
        assert not render_helper.render_capture(folder, dry_run=False)['ok']
        assert not list(Path(folder).iterdir())


def test_blender_handoff_inspection_reports_authored_metadata_read_only():
    stage = _stage(); before = stage.GetRootLayer().ExportToString()
    result = blender_handoff.blender_handoff_check(stage)
    assert result['ok'] and result['meters_per_unit'] == 1.0
    assert stage.GetRootLayer().ExportToString() == before


def test_missing_properties_cannot_pass_a_rubric_by_comparing_to_none_or_empty_targets():
    stage = _stage()
    for kind, value in (('attribute',None),('relationship',[])):
        result = rubric.check_rubric(stage, {'rules':[{'id':'x','kind':kind,'path':'/World',
                                                      'name':'missing','equals':value}]})
        assert result['ok'] and not result['passed']


def test_semantic_labels_preview_apply_inherit_and_undo_exactly():
    from omniverse_scene_mcp import semantic_labels, undo
    stage=_stage(); stack=undo.UndoStack(); before=[l.ExportToString() for l in stage.GetLayerStack()]
    for flag in (True,None,0,'false',[],{}):
        result=semantic_labels.add_semantic_labels(stage,{'/World/Box':'crate'},dry_run=flag)
        assert result['ok'] and not result['written'],result
        assert before==[l.ExportToString() for l in stage.GetLayerStack()]
    applied=semantic_labels.add_semantic_labels(stage,{'/World/Box':'crate'},dry_run=False,undo_stack=stack)
    assert applied['ok'] and applied['applied'] and applied['undo']['recorded'],applied
    audit=semantic_labels.semantic_audit(stage)
    assert audit['ok'] and audit['class_histogram']=={'crate':1},audit
    assert stack.undo_last(stage=stage,dry_run=False)['ok']
    assert before==[l.ExportToString() for l in stage.GetLayerStack()]
    assert not semantic_labels.add_semantic_labels(stage,{'/World/Box':'crate','/missing':'bad'},dry_run=False)['ok']
    assert before==[l.ExportToString() for l in stage.GetLayerStack()]


def test_render_sequence_plans_and_pixel_comparison_are_read_only():
    with tempfile.TemporaryDirectory() as folder:
        plan=render_helper.plan_image_sequence(folder,start=2,end=4,fps=24)
        assert plan['ok'] and plan['frames']==[2,3,4] and plan['size']=='viewport size' and plan['width'] is None
        sized=render_helper.plan_image_sequence(folder,start=2,end=4,fps=24,width=320,height=240)
        assert sized['ok'] and sized['uncompressed_rgba_bytes']==3*320*240*4
        assert not render_helper.plan_image_sequence(folder,width=320)['ok']
        assert not render_helper.plan_image_sequence(folder,end=60)['ok']
        assert not render_helper.plan_image_sequence(folder,start=3,end=2)['ok']
        stage=_stage(); before=stage.GetRootLayer().ExportToString()
        turn=render_helper.plan_turntable_render(stage,'/World/Box',folder,seconds=1,fps=24)
        assert turn['ok'],turn
        assert before==stage.GetRootLayer().ExportToString()
        a=Path(folder)/'a.png'; b=Path(folder)/'b.png'
        _image(a); _image(b)
        assert render_helper.compare_screenshots(str(a),str(b))['changed_percent']==0
        assert not render_helper.compare_screenshots(str(a),str(b),tolerance=True)['ok']
        _image(b,(16,16))
        assert not render_helper.compare_screenshots(str(a),str(b))['ok']


def test_capture_sequence_uses_fake_captures_restores_time_and_refuses_running_timeline():
    from types import SimpleNamespace
    from omniverse_scene_mcp import kit_live_extras
    async def run(folder, fail):
        stage=_stage(); times=[7.5]; captured=[]
        def capture(output,**kwargs):
            if fail: return {'ok':False,'error':'fake capture failure'}
            path=str(Path(output)/f'frame_{len(captured)}.png'); captured.append(path)
            return {'ok':True,'path':path}
        async def update(): await asyncio.sleep(0)
        timeline=SimpleNamespace(is_playing=lambda:False,get_current_time=lambda:times[-1],
                                 set_current_time=lambda value:times.append(value))
        modules={'omni.usd':SimpleNamespace(get_context=lambda:SimpleNamespace(get_stage=lambda:stage)),
                 'omni.kit.app':SimpleNamespace(get_app=lambda:SimpleNamespace(next_update_async=update)),
                 'omni.timeline':SimpleNamespace(get_timeline_interface=lambda:timeline)}
        original=render_helper.importlib.import_module
        def load(name): return modules[name] if name in modules else original(name)
        with patch.object(render_helper.importlib,'import_module',load), \
             patch.object(kit_live_extras,'_kit_module',return_value=SimpleNamespace(get_active_viewport=lambda:object())), \
             patch.object(kit_live_extras,'capture_viewport',capture), \
             patch.object(kit_live_extras,'capture_status',lambda path:{'ok':True,'ready':True,'path':path}):
            preview=render_helper.render_sequence(folder)
            assert preview['dry_run'] and times==[7.5] and not list(Path(folder).iterdir())
            result=render_helper.render_sequence(folder,end=1,dry_run=False)
            assert result['ok'],result
            await asyncio.gather(*list(render_helper._TASKS.values()))
        status=render_helper.render_job_status(result['job_id'])
        assert status['status']==('failed' if fail else 'completed'),status
        assert times[-1]==7.5 and not render_helper._TASKS
        assert len(status['captures'])==(0 if fail else 2)
    with tempfile.TemporaryDirectory() as folder: asyncio.run(run(folder,False))
    with tempfile.TemporaryDirectory() as folder: asyncio.run(run(folder,True))


def test_replicator_job_completes_cleans_up_and_reports_failures_with_fake_kit():
    from types import SimpleNamespace
    stage = _stage()
    async def exercise(folder, fail, skip=False):
        calls=[]; count=0; output=None; statuses=['Status.STOPPING','Status.STOPPING']
        class Product:
            def destroy(self): calls.append('destroy')
        class Writer:
            def initialize(self, **kwargs):
                nonlocal output
                output = Path(kwargs['output_dir'])
            def attach(self, products): calls.append('attach')
            def detach(self): calls.append('detach')
        async def step(**kwargs):
            nonlocal count
            if fail: raise RuntimeError('fake render failure')
            if skip: return  # Replicator 'step skipped': nothing reaches the disk
            _image(output/f'rgb_{count:04}.png'); count += 1
        async def wait(): calls.append('flush')
        fake=SimpleNamespace(set_global_seed=lambda seed:calls.append(seed),
            create=SimpleNamespace(render_product=lambda *a:Product()),
            WriterRegistry=SimpleNamespace(get=lambda name:Writer()),
            orchestrator=SimpleNamespace(get_status=lambda:statuses.pop(0) if statuses else 'Status.STOPPED',
                                         step_async=step,wait_until_complete_async=wait))
        async def next_update(): calls.append('frame')
        modules={'omni.replicator.core':fake,'omni.usd':SimpleNamespace(get_context=lambda:SimpleNamespace(get_stage=lambda:stage)),
                 'omni.timeline':SimpleNamespace(get_timeline_interface=lambda:SimpleNamespace(is_playing=lambda:False)),
                 'omni.kit.app':SimpleNamespace(get_app=lambda:SimpleNamespace(next_update_async=next_update))}
        original=sdg.importlib.import_module
        def load(name): return modules[name] if name in modules else original(name)
        with patch.object(sdg.importlib,'import_module',load):
            result=sdg.start_sdg(folder, frames=2, width=32, height=24, dry_run=False)
            assert result['ok'] and result['status']=='pending',result
            assert not sdg.start_sdg(folder,name='second_job',dry_run=False)['ok']
            await asyncio.gather(*list(sdg._TASKS.values()))
        state=sdg.sdg_status(result['job_id'])
        assert state['status']==('failed' if fail or skip else 'completed'),state
        assert 'detach' in calls and 'destroy' in calls and not sdg._TASKS
        assert 'frame' in calls, 'the job must wait while Replicator is still STOPPING'
        if skip: assert 'Only 0 of 2 frames' in state['error'] and state['frames_written']==0, state
        elif not fail:
            assert dataset.check_dataset(state['output_dir'],expected_frames=2)['passed'] and state['frames_written']==2
        else: assert 'fake render failure' in state['error']
    with tempfile.TemporaryDirectory() as folder: asyncio.run(exercise(folder,False))
    with tempfile.TemporaryDirectory() as folder: asyncio.run(exercise(folder,True))
    with tempfile.TemporaryDirectory() as folder: asyncio.run(exercise(folder,False,skip=True))


def test_sdg_names_a_missing_camera_and_lists_the_real_ones():
    # Seen live in Isaac 6.1: a missing camera gave "Accessed invalid null prim" and the preview said ok.
    _require_pxr()
    from types import SimpleNamespace
    from pxr import Usd, UsdGeom
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Camera.Define(stage, '/World/Robot/FrontCam')
    UsdGeom.Xform.Define(stage, '/World/Box')
    assert sdg.camera_problem(stage, '/World/Robot/FrontCam') is None
    missing = sdg.camera_problem(stage, '/World/Camera')
    assert 'does not exist' in missing and '/World/Robot/FrontCam' in missing, missing
    assert 'is not a Camera' in sdg.camera_problem(stage, '/World/Box')
    assert 'no camera' in sdg.camera_problem(Usd.Stage.CreateInMemory(), '/World/Camera')
    with tempfile.TemporaryDirectory() as folder:
        live = SimpleNamespace(get_context=lambda: SimpleNamespace(get_stage=lambda: stage))
        original = sdg.importlib.import_module
        with patch.object(sdg.importlib, 'import_module', lambda n: live if n == 'omni.usd' else original(n)):
            preview = sdg.start_sdg(folder, camera='/World/Camera')
        assert not preview['ok'] and preview['dry_run'] and 'does not exist' in preview['error'], preview


def _require_pxr() -> None:
    """Skip (not fail) a test that needs the USD Python bindings when pxr is missing or does not import."""
    try:
        import pxr  # noqa: F401
    except ImportError:
        import unittest

        raise unittest.SkipTest("USD Python bindings (pxr) are not installed")
