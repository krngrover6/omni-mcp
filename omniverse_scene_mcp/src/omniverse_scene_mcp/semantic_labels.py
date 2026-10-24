"""OpenUSD class labels, using the schema present in both supported USD builds.

Verified against Isaac's omni.replicator.core-1.13.36 tests/test_semantic_updates.py
and scripts/functional/modify.py: UsdSemantics.LabelsAPI.Apply(prim, 'class').
"""
from __future__ import annotations
import time


def semantic_audit(stage, *, max_prims: int = 10000) -> dict:
    """Read class labels and unlabelled geometry, including inherited parent labels; bounded, no authoring."""
    try:
        from pxr import UsdGeom, UsdSemantics
        from .day5_io import integer
        integer(max_prims, 'max_prims', 1, 100000)
        rows=[]; histogram={}; unlabelled=[]; ambiguous=[]; started=time.monotonic(); count=0; truncated=False
        for prim in stage.Traverse():
            if count >= max_prims or time.monotonic()-started > 3:
                truncated=True; break
            count+=1
            own=UsdSemantics.LabelsAPI.Get(prim,'class').GetLabelsAttr().Get() or []
            if own: rows.append({'path':str(prim.GetPath()),'labels':list(own)})
            if not prim.IsA(UsdGeom.Gprim): continue
            labels=set(own); parent=prim.GetParent(); depth=0
            while parent and not parent.IsPseudoRoot() and depth<128:
                labels.update(UsdSemantics.LabelsAPI.Get(parent,'class').GetLabelsAttr().Get() or [])
                parent=parent.GetParent(); depth+=1
            if not labels: unlabelled.append(str(prim.GetPath()))
            if len(labels)>1: ambiguous.append({'path':str(prim.GetPath()),'classes':sorted(labels)})
            for label in labels: histogram[label]=histogram.get(label,0)+1
        return {'ok':True,'read_only':True,'scanned':count,'truncated':truncated,'labelled_prims':rows[:1000],
                'class_histogram':histogram,'unlabelled_geometry':unlabelled[:1000],'unlabelled_count':len(unlabelled),
                'ambiguous_geometry':ambiguous[:1000],
                'note':'Multiple classes may be intentional; audit does not decide training-label correctness.'}
    except (ImportError,ValueError,RuntimeError) as exc:
        return {'ok':False,'error':str(exc),'written':False}


def add_semantic_labels(stage, mapping: dict, *, dry_run: bool = True, undo_stack=None) -> dict:
    """Preview or apply class labels in one snapshot transaction with rollback and optional MCP undo."""
    try:
        from pxr import Sdf, UsdSemantics
        from . import authoring
        from .variant_manager import _apply
        authoring._check_stage(stage)
        if not isinstance(mapping,dict) or not 1<=len(mapping)<=100: raise ValueError('Supply 1 to 100 prim-to-class entries')
        target=stage.GetEditTarget(); layer=target.GetLayer(); values=[]; spec_paths=[]
        for path,label in mapping.items():
            sdf=Sdf.Path(path)
            if not sdf.IsAbsolutePath() or not sdf.IsPrimPath(): raise ValueError('Labels need absolute prim paths')
            prim=stage.GetPrimAtPath(path)
            if not prim: raise ValueError(f'No prim at {path}')
            if not isinstance(label,str) or not label.strip() or len(label)>128 or any(ord(c)<32 for c in label):
                raise ValueError('Each class must be a nonempty string of up to 128 characters')
            mapped=target.MapToSpecPath(sdf)
            if not mapped: raise ValueError('Edit target cannot map a prim path')
            spec_paths.append(str(mapped)); values.append((prim,label))
        plan={'mapping':dict(mapping),'schema':'SemanticsLabelsAPI:class'}
        if dry_run is not False:
            return {'ok':True,'dry_run':True,'applied':False,'written':False,'plan':plan,
                    'explanation':'Would author class labels in the current edit layer; nothing has changed.'}
        def mutate():
            for prim,label in values:
                api=UsdSemantics.LabelsAPI.Apply(prim,'class')
                if not api.CreateLabelsAttr().Set([label]): raise RuntimeError('Could not author class label')
        def verify():
            for prim,label in values:
                if list(UsdSemantics.LabelsAPI.Get(prim,'class').GetLabelsAttr().Get() or []) != [label]:
                    return 'Authored labels differ from plan',{}
            return None,{}
        return _apply('semantic_labels',stage,[(layer,[],spec_paths)],mutate,verify,undo_stack,
                      'Set semantic class labels',[], 'Authored semantic class labels.',{'plan':plan,'dry_run':False})
    except (ImportError,ValueError,RuntimeError,TypeError) as exc:
        return {'ok':False,'error':str(exc),'written':False}
