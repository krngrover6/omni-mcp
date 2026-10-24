"""Run manually in Composer's Script Editor; temporarily enable only the generated test extension.

Click Inspect stage in the USD Study window when prompted. The extension is disabled after
the click (or timeout), its temporary search path removed, and no scene layer is saved.
"""
import asyncio
import gc
import hashlib
import json
from pathlib import Path
import time
import traceback

PROJECT = Path('C:/Users/krngr/Omniverse/mcp_day5_validation')
EXTENSION = PROJECT/'mcp_verified_extension_20261024_225558'


async def test_extension():
    import omni.ext
    import omni.kit.app
    import omni.ui as ui
    import omni.usd
    app=omni.kit.app.get_app(); manager=app.get_extension_manager()
    stage=omni.usd.get_context().get_stage()
    if stage is None or Path(stage.GetRootLayer().realPath or '').resolve()!=(PROJECT/'root.usda').resolve():
        raise RuntimeError('Open the dedicated test project in Composer first; no extension changed.')
    if not (EXTENSION/'config/extension.toml').is_file():
        raise RuntimeError('Generated extension folder is missing; nothing changed.')
    files=[PROJECT/'root.usda',*(PROJECT/'layers').glob('*.usda'),
           EXTENSION/'config/extension.toml',EXTENSION/'study_extension/__init__.py']
    def hashes(): return {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    original=hashes(); enabled_before={row['id'] for row in manager.get_extensions() if row.get('enabled')}
    # This uniquely generated folder belongs to the validation project. A previous
    # interrupted test may have left it registered; clean up that registration too.
    def matching_ids():
        result=[]
        for row in manager.get_extensions():
            try:
                if Path(manager.get_extension_path(row['id'])).resolve()==EXTENSION.resolve(): result.append(row['id'])
            except (OSError,ValueError): pass
        return result
    registered_before=matching_ids()
    if any(manager.is_extension_enabled(i) for i in registered_before):
        raise RuntimeError('Test extension is already enabled; nothing changed.')
    def visible_window():
        window=ui.Workspace.get_window('USD Study')
        try:return window is not None and window.visible
        except RuntimeError:return False  # destroyed UI handle retained by Workspace
    if visible_window():
        if not registered_before:
            raise RuntimeError('An unrelated visible USD Study window exists; close it before testing.')
        # Our earlier interrupted test disabled this registered extension but left its UI visible.
        stale=ui.Workspace.get_window('USD Study')
        stale.visible=False
        stale.destroy()
        for _ in range(3):await app.next_update_async()
    report={'started':time.time(),'extension_path':str(EXTENSION),'checks':{}}
    output=PROJECT/'docs'/('extension_live_'+time.strftime('%Y%m%d_%H%M%S')+'.json')
    def save(): output.write_text(json.dumps(report,indent=2),encoding='utf-8')
    added_path=False; ext_id=None; instance=None
    try:
        if not registered_before:
            manager.add_path(str(EXTENSION),omni.ext.ExtensionPathType.DIRECT_PATH)
            added_path=True
            for _ in range(5): await app.next_update_async()
        ids=matching_ids()
        if len(ids)!=1: raise RuntimeError(f'Expected one extension from the test folder, found {len(ids)}')
        ext_id=ids[0]; report['extension_id']=ext_id
        if not manager.set_extension_enabled_immediate(ext_id,True):
            raise RuntimeError('Kit refused to enable the generated extension')
        for _ in range(5): await app.next_update_async()
        report['checks']['enabled']=manager.is_extension_enabled(ext_id)
        report['checks']['window_created']=visible_window()
        # Observe this exact Python extension instance; never inspect unrelated application objects.
        matches=[o for o in gc.get_objects() if type(o).__module__=='study_extension'
                 and type(o).__name__=='StudyExtension' and getattr(o,'_window',None) is not None]
        if len(matches)!=1: raise RuntimeError(f'Cannot observe a unique test extension instance ({len(matches)})')
        instance=matches[0]
        expected=stage.GetRootLayer().identifier
        report['initial_label']=instance._label.text
        save()
        print('[MCP extension] Click Inspect stage in the USD Study window now. Waiting up to 120 seconds...')
        deadline=time.monotonic()+120
        while time.monotonic()<deadline:
            if omni.usd.get_context().get_stage()!=stage:
                raise RuntimeError('Active stage changed; extension test stopped')
            if instance._label.text==expected: break
            await app.next_update_async()
        report['label_after_click']=instance._label.text
        report['checks']['actual_button_click']=instance._label.text==expected
        if not report['checks']['actual_button_click']: report['error']='No successful Inspect stage click before timeout'
        save()  # preserve the button result even if shutdown encounters an error
    except Exception as exc:
        report['error']=f'{type(exc).__name__}: {exc}'
        traceback.print_exc()
    finally:
        cleanup_errors=[]
        try:
            if ext_id and manager.is_extension_enabled(ext_id):
                manager.set_extension_enabled_immediate(ext_id,False)
                for _ in range(3): await app.next_update_async()
            report['checks']['disabled']=not ext_id or not manager.is_extension_enabled(ext_id)
            report['checks']['window_destroyed']=not visible_window()
            if instance is not None:
                # Kit may clear the Python instance dictionary after on_shutdown.
                report['checks']['widget_references_cleared']=getattr(instance,'_window',None) is None and getattr(instance,'_label',None) is None
        except Exception as exc:
            cleanup_errors.append(f'disable/check: {type(exc).__name__}: {exc}')
        try:
            if added_path or registered_before:
                manager.remove_path(str(EXTENSION))
                for _ in range(3): await app.next_update_async()
            report['checks']['test_path_removed']=not any(
                Path(folder.get('path','')).resolve()==EXTENSION.resolve() for folder in manager.get_folders())
        except Exception as exc:
            cleanup_errors.append(f'remove test path: {type(exc).__name__}: {exc}')
        if cleanup_errors:report['cleanup_errors']=cleanup_errors
        enabled_after={row['id'] for row in manager.get_extensions() if row.get('enabled')}
        report['checks']['enabled_extensions_restored']=enabled_before==enabled_after
        report['checks']['scene_and_extension_files_unchanged']=original==hashes()
        report['passed']=not report.get('error') and not cleanup_errors and all(report['checks'].values())
        report['finished']=time.time();save()
        print(f'[MCP extension] Passed: {report["passed"]}. Full results: {output}')


print('[MCP extension] Starting the temporary extension check...')
task=asyncio.ensure_future(test_extension())
def finished(job):
    if not job.cancelled() and job.exception() is not None:traceback.print_exception(job.exception())
task.add_done_callback(finished)
