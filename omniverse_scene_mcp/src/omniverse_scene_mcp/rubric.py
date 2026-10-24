"""Declarative custom USD rubrics: bounded reads, no Python expressions or arbitrary callbacks."""
from __future__ import annotations
import json
import math
import time


def rubric_template() -> dict:
    """Return an editable example rubric; structural checks do not certify visual appearance or exam grading."""
    return {'ok': True, 'rubric': {'title': 'Mini inspection project', 'rules': [
        {'id': 'world', 'kind': 'prim', 'path': '/World', 'type': 'Xform', 'weight': 1},
        {'id': 'camera', 'kind': 'prim', 'path': '/World/Camera', 'type': 'Camera', 'weight': 1},
        {'id': 'units', 'kind': 'metadata', 'name': 'metersPerUnit', 'equals': 1.0, 'weight': 1},
        {'id': 'cubes', 'kind': 'count', 'path': '/World', 'type': 'Cube', 'gte': 1, 'weight': 2},
    ]}, 'note': 'Supported comparisons: equals, gte, lte. Rubrics contain data, never executable code.'}


def _plain(value):
    if value is None or isinstance(value, (str, bool, int, float)): return value
    try: return [_plain(v) for v in value]
    except TypeError: return str(value)


def check_rubric(stage, rubric: dict, *, max_prims: int = 20000) -> dict:
    """Score at most 100 declarative rules on a USD stage, with a traversal cap and time budget; never author."""
    try:
        from pxr import Sdf, Usd
        from .day5_io import integer
        integer(max_prims, 'max_prims', 1, 100000)
        if not isinstance(rubric, dict) or set(rubric)-{'title', 'rules'}:
            raise ValueError('rubric must contain title/rules only')
        if len(json.dumps(rubric, allow_nan=False)) > 100000: raise ValueError('Rubric exceeds 100 KB')
        rules = rubric.get('rules')
        if not isinstance(rules, list) or not 1 <= len(rules) <= 100: raise ValueError('Supply 1 to 100 rules')
        ids = set()
        for rule in rules:
            if not isinstance(rule, dict) or set(rule)-{'id', 'kind', 'path', 'type', 'name', 'equals', 'gte', 'lte', 'weight'}:
                raise ValueError('Unknown rubric rule fields; executable expressions are not supported')
            key = rule.get('id'); kind = rule.get('kind')
            if not isinstance(key, str) or not key or key in ids: raise ValueError('Rule IDs must be unique strings')
            ids.add(key)
            if kind not in {'prim', 'attribute', 'relationship', 'metadata', 'count'}: raise ValueError('Unknown rule kind')
            if kind != 'metadata':
                path = Sdf.Path(rule.get('path', ''))
                if not path.IsAbsolutePath() or not path.IsPrimPath(): raise ValueError('Rule path must be an absolute prim path')
            if kind in {'attribute', 'relationship', 'metadata'} and not isinstance(rule.get('name'), str):
                raise ValueError('This rule needs a property or metadata name')
            if 'type' in rule and not isinstance(rule['type'], str): raise ValueError('type must be a USD type name')
            weight = rule.get('weight', 1)
            if type(weight) not in (int,float) or not math.isfinite(weight) or not 0 < weight <= 100:
                raise ValueError('Weights must be finite positive numbers up to 100')
            if kind != 'prim' and not any(k in rule for k in ('equals','gte','lte')):
                raise ValueError('Rule needs an explicit comparison')
            for op in ('gte','lte'):
                if op in rule and (type(rule[op]) not in (int,float) or not math.isfinite(rule[op])):
                    raise ValueError('Numeric comparisons require finite numbers')
        # Traverse once only if count rules need it, including inactive/undefined prims consistently.
        prims = []; started = time.monotonic()
        if any(r['kind'] == 'count' for r in rules):
            for prim in stage.Traverse():
                if len(prims) >= max_prims or time.monotonic()-started > 3:
                    return {'ok': False, 'passed': False, 'incomplete': True, 'error': 'Rubric traversal limit reached'}
                prims.append(prim)
        rows = []; earned = possible = 0
        for rule in rules:
            exists = True
            kind = rule['kind']; prim = stage.GetPrimAtPath(rule.get('path', '/'))
            if kind == 'prim':
                actual = bool(prim) and (not rule.get('type') or prim.GetTypeName() == rule['type'])
            elif kind == 'count':
                actual = sum(1 for p in prims if p.GetPath().HasPrefix(rule['path']) and
                             (not rule.get('type') or p.GetTypeName() == rule['type']))
            elif kind == 'metadata': actual = _plain(stage.GetMetadata(rule['name']))
            elif kind == 'attribute':
                prop = prim.GetAttribute(rule['name']) if prim else None
                exists = bool(prop); actual = _plain(prop.Get()) if prop else None
            else:
                prop = prim.GetRelationship(rule['name']) if prim else None
                exists = bool(prop); actual = [str(p) for p in prop.GetTargets()] if prop else None
            passed = actual is True if kind == 'prim' else exists
            for op in ('equals','gte','lte'):
                if op not in rule: continue
                if op == 'equals': passed = passed and actual == rule[op]
                else:
                    numeric = type(actual) in (int,float) and math.isfinite(actual)
                    passed = passed and numeric and (actual >= rule[op] if op == 'gte' else actual <= rule[op])
            weight = rule.get('weight', 1); possible += weight
            if passed: earned += weight
            rows.append({'id': rule['id'], 'passed': bool(passed), 'actual': actual, 'weight': weight})
        return {'ok': True, 'passed': all(r['passed'] for r in rows), 'read_only': True,
                'score_percent': round(100*earned/possible, 2), 'earned': earned, 'possible': possible, 'rules': rows,
                'note': 'Checks declared USD structure only; visual quality and physical behavior require separate review.'}
    except (ImportError, ValueError, TypeError, RuntimeError) as exc:
        return {'ok': False, 'passed': False, 'error': str(exc), 'written': False}
