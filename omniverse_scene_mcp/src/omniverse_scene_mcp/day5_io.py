"""Small, bounded helpers for new generated artifacts; never overwrite existing work."""
from __future__ import annotations
import json
import re
from pathlib import Path
from .file_stage import default_workspace_root


def integer(value, label, minimum=1, maximum=1000):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f'{label} must be an integer from {minimum} to {maximum}')
    return value


def new_directory(project_dir, name):
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', name):
        raise ValueError('name must start with a letter and contain up to 64 letters, digits, underscores or hyphens')
    if name.upper() in {'CON', 'PRN', 'AUX', 'NUL'} or re.fullmatch(r'(COM|LPT)[0-9]', name.upper()):
        raise ValueError('Reserved Windows name')
    root = Path(project_dir or default_workspace_root()).resolve(strict=True)
    if not root.is_dir():
        raise ValueError('project_dir must be an existing directory')
    target = root / name
    if target.exists() or target.is_symlink():
        raise ValueError(f'Refusing to overwrite existing path: {target}')
    return target


def bundle(project_dir, name, files, dry_run=True):
    """Preview a bundle, or write exclusively into one new directory, rolling back only our files on failure."""
    target = new_directory(project_dir, name)
    for relative, content in files.items():
        path = Path(relative)
        if path.is_absolute() or '..' in path.parts or '\\' in relative or ':' in relative:
            raise ValueError('Bundle paths must be relative and stay inside the new directory')
        if not isinstance(content, str) or len(content.encode('utf-8')) > 2_000_000:
            raise ValueError('Bundle content must be bounded UTF-8 text')
    result = {'ok': True, 'dry_run': dry_run is not False, 'written': False,
              'directory': str(target), 'files': list(files)}
    if dry_run is not False:
        return result
    target.mkdir()  # exclusive: a racing creator must fail before we own anything
    created = []
    try:
        for relative, content in files.items():
            path = target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('x', encoding='utf-8', newline='\n') as stream:
                created.append(path)
                stream.write(content)
    except Exception:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        # Do not recursively delete anything another process may have placed here.
        for path in sorted(target.rglob('*'), key=lambda p: len(p.parts), reverse=True):
            if path.is_dir() and not path.is_symlink():
                try: path.rmdir()
                except OSError: pass
        try: target.rmdir()
        except OSError: pass
        raise
    return {**result, 'written': True}


def json_text(value):
    return json.dumps(value, indent=2, allow_nan=False) + '\n'
