"""Read-only validation of BasicWriter RGB/segmentation/bounding-box datasets."""
from __future__ import annotations
import ast
import json
import math
import re
import struct
import zlib
from pathlib import Path
from .day5_io import integer

MODALITIES = ('rgb', 'semantic_segmentation', 'bounding_box_2d_tight')
_FILE = re.compile(r'^(rgb|semantic_segmentation|bounding_box_2d_tight)_(\d+)\.(png|npy)$')


def _image(path, decode=False):
    """Check PNG chunks, CRCs and bounded decompression without adding an image-library dependency."""
    if path.stat().st_size > 128_000_000: raise ValueError('PNG exceeds 128 MB limit')
    with path.open('rb') as stream:
        if stream.read(8) != b'\x89PNG\r\n\x1a\n': raise ValueError('Invalid PNG signature')
        width = height = row = expected = None; decoder = zlib.decompressobj(); decoded = bytearray()
        ended = False; palette = False; color = None; compressed = False
        for index in range(10000):
            header = stream.read(8)
            if len(header) != 8: raise ValueError('Truncated PNG chunk')
            length, kind = struct.unpack('>I4s', header)
            if length > 64_000_000: raise ValueError('PNG chunk exceeds limit')
            data = stream.read(length); crc = stream.read(4)
            if len(data) != length or len(crc) != 4 or zlib.crc32(kind+data) != int.from_bytes(crc,'big'):
                raise ValueError('Truncated PNG or invalid chunk CRC')
            if index == 0:
                if kind != b'IHDR' or length != 13: raise ValueError('PNG must begin with IHDR')
                width,height,bits,color,compression,filtering,interlace = struct.unpack('>IIBBBBB',data)
                channels = {0:1,2:3,3:1,4:2,6:4}.get(color)
                legal = (1,2,4,8,16) if color == 0 else ((1,2,4,8) if color == 3 else (8,16))
                if not channels or bits not in legal or not width or not height or width*height > 16_777_216:
                    raise ValueError('Unsupported or oversized PNG geometry/bit depth')
                if decode and width*height>1_048_576: raise ValueError('Exact comparison is limited to one megapixel')
                if compression or filtering or interlace:
                    raise ValueError('Unsupported PNG encoding (non-interlaced BasicWriter PNGs expected)')
                row = (width*channels*bits+7)//8 + 1; expected = row*height
                if expected > 64_000_000: raise ValueError('Decoded PNG exceeds 64 MB')
            elif kind == b'IHDR': raise ValueError('Duplicate PNG header')
            elif kind == b'PLTE': palette = True
            elif kind == b'IDAT':
                compressed = True
                decoded.extend(decoder.decompress(data, expected+1-len(decoded)))
                if len(decoded) > expected or decoder.unconsumed_tail:
                    raise ValueError('PNG expands beyond declared dimensions')
            elif kind == b'IEND':
                if length or stream.read(1): raise ValueError('Invalid PNG ending')
                ended = True; break
        if not ended or not compressed or not decoder.eof or decoder.unused_data or len(decoded) != expected:
            raise ValueError('Incomplete PNG image stream')
        if color == 3 and not palette: raise ValueError('Indexed PNG has no palette')
        if any(decoded[offset] > 4 for offset in range(0,len(decoded),row)):
            raise ValueError('Invalid PNG row filter')
    if not decode: return [width, height]
    if bits != 8 or color not in (2,6): raise ValueError('Pixel comparison supports 8-bit RGB/RGBA PNGs only')
    channels=3 if color==2 else 4; previous=bytearray(row-1); pixels=bytearray()
    for offset in range(0,len(decoded),row):
        filter_type=decoded[offset]; current=bytearray(decoded[offset+1:offset+row])
        for index in range(len(current)):
            left=current[index-channels] if index>=channels else 0
            up=previous[index]; corner=previous[index-channels] if index>=channels else 0
            if filter_type==0: prediction=0
            elif filter_type==1: prediction=left
            elif filter_type==2: prediction=up
            elif filter_type==3: prediction=(left+up)//2
            else:
                p=left+up-corner; distances=(abs(p-left),abs(p-up),abs(p-corner))
                prediction=(left,up,corner)[distances.index(min(distances))]
            current[index]=(current[index]+prediction)&255
        for index in range(0,len(current),channels):
            pixels.extend(current[index:index+3]); pixels.append(current[index+3] if channels==4 else 255)
        previous=current
    return [width,height],pixels


def _dtype_size(descriptor, depth=0):
    if depth > 8: raise ValueError('NPY dtype nesting exceeds limit')
    if isinstance(descriptor,str):
        match = re.fullmatch(r'[<>=|]?([?bBiufcSUVmM])(\d+)(?:\[[A-Za-z0-9]+\])?',descriptor)
        if not match: raise ValueError('Unsupported or object/pickle NPY dtype')
        size = int(match[2]) * (4 if match[1] == 'U' else 1)
        if not 0 < size <= 1_000_000: raise ValueError('Invalid NPY dtype size')
        return size
    if isinstance(descriptor,list) and len(descriptor) <= 1000:
        size = 0
        for field in descriptor:
            if not isinstance(field,tuple) or len(field) not in (2,3): raise ValueError('Invalid structured dtype')
            count = 1
            if len(field) == 3:
                shape = field[2]
                if not isinstance(shape,tuple) or any(type(n) is not int or n < 0 for n in shape):
                    raise ValueError('Invalid NPY field shape')
                count = math.prod(shape)
            size += _dtype_size(field[1],depth+1)*count
        return size
    raise ValueError('Unsupported NPY dtype')


def _array(path):
    """Validate a bounded NPY header and data size without loading arrays or unpickling objects."""
    with path.open('rb') as stream:
        if stream.read(6) != b'\x93NUMPY':
            raise ValueError('Invalid NPY signature')
        version = tuple(stream.read(2))
        if version not in ((1, 0), (2, 0), (3, 0)):
            raise ValueError('Unsupported NPY version')
        count = 2 if version == (1, 0) else 4
        length = int.from_bytes(stream.read(count), 'little')
        if not 1 <= length <= 65536:
            raise ValueError('NPY header exceeds limit')
        header = ast.literal_eval(stream.read(length).decode('utf-8' if version == (3, 0) else 'latin1'))
        shape = header.get('shape')
        if not isinstance(shape, tuple) or len(shape) > 8 or any(type(n) is not int or n < 0 for n in shape):
            raise ValueError('Invalid NPY shape')
        if math.prod(shape) * _dtype_size(header['descr']) != path.stat().st_size - stream.tell():
            raise ValueError('NPY data size does not match its header')
    return list(shape)


def check_dataset(directory: str, *, expected_frames: int | None = None, required_modalities=None,
                  width: int | None = None, height: int | None = None, max_files: int = 10000) -> dict:
    """Check frame coverage, PNG integrity, safe NPY arrays and label JSON; never modify data or load pickle."""
    try:
        root = Path(directory).resolve(strict=True)
        if not root.is_dir(): raise ValueError('directory must be a dataset folder')
        limit = integer(max_files, 'max_files', 1, 100000)
        if expected_frames is not None: integer(expected_frames, 'expected_frames', 1, 100000)
        if (width is None) != (height is None): raise ValueError('Specify both width and height')
        if width is not None:
            integer(width, 'width', 1, 4096); integer(height, 'height', 1, 4096)
        modalities = ['rgb'] if required_modalities is None else required_modalities
        if not isinstance(modalities, list) or not modalities or len(set(modalities)) != len(modalities) or any(m not in MODALITIES for m in modalities):
            raise ValueError('required_modalities must be a nonempty unique list of supported names')
        found = {m: set() for m in MODALITIES}; issues = []; scanned = 0; truncated = False
        # Do not follow links/junctions into unrelated datasets or network paths.
        import os
        for parent, folders, files in os.walk(root, followlinks=False):
            folders[:] = sorted(n for n in folders if not (Path(parent)/n).is_symlink()
                                and not (hasattr(Path(parent)/n, 'is_junction') and (Path(parent)/n).is_junction()))
            for filename in sorted(files):
                if scanned >= limit:
                    truncated = True; break
                scanned += 1
                path = Path(parent)/filename
                if path.is_symlink() or not path.resolve().is_relative_to(root):
                    issues.append({'file': str(path.relative_to(root)), 'error': 'Linked file refused'}); continue
                match = _FILE.fullmatch(filename)
                try:
                    if match:
                        kind, frame, suffix = match.groups(); key = (str(path.parent.relative_to(root)), int(frame))
                        if key in found[kind]: raise ValueError('Duplicate frame ID in one render product')
                        shape = _image(path) if suffix == 'png' else _array(path)
                        if kind == 'rgb' and suffix != 'png': raise ValueError('RGB must be PNG')
                        if width is not None and kind == 'rgb' and shape != [width, height]:
                            raise ValueError(f'Expected {width}x{height}, found {shape}')
                        found[kind].add(key)
                    elif path.suffix.lower() == '.json':
                        if path.stat().st_size > 1_000_000: raise ValueError('JSON exceeds 1 MB')
                        json.loads(path.read_text(encoding='utf-8'))
                except (ValueError, OSError, ImportError, SyntaxError, KeyError, TypeError, zlib.error, RecursionError) as exc:
                    issues.append({'file': str(path.relative_to(root)), 'error': str(exc)[:300]})
            if truncated: break
        for kind in modalities:
            if not found[kind]: issues.append({'modality': kind, 'error': 'No valid frames found'})
            if expected_frames is not None and len(found[kind]) != expected_frames:
                issues.append({'modality': kind, 'error': f'Expected {expected_frames} frames, found {len(found[kind])}'})
        if found[modalities[0]]:
            for kind in modalities[1:]:
                # BasicWriter may put modalities in different subfolders: match IDs independent of their folder.
                if {f for _, f in found[kind]} != {f for _, f in found[modalities[0]]}:
                    issues.append({'modality': kind, 'error': 'Frame IDs do not match required modalities'})
        if truncated: issues.append({'error': 'Scan limit reached; validation incomplete'})
        return {'ok': True, 'passed': not issues, 'read_only': True, 'directory': str(root),
                'scanned': scanned, 'truncated': truncated, 'counts': {k: len(v) for k,v in found.items()},
                'issues': issues[:100], 'issue_count': len(issues),
                'limitations': 'Checks files and frame coverage; it does not assess label accuracy or dataset bias.'}
    except (ValueError, OSError, TypeError) as exc:
        return {'ok': False, 'passed': False, 'error': str(exc), 'written': False}
