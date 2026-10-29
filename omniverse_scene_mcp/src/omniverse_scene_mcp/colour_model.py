"""Train a tiny colour detector from your own synthetic (SDG) pictures, then let the colour sorter use it.

Words: TRAINING means learning from examples. Here the examples are SDG frames: the RGB picture plus the semantic
segmentation picture, which says which pixels belong to which labelled class ("blue_package", ...). For every
class the trainer averages the colour of its pixels (in linear colour, skipping grey/white pixels); that average
is the class's CENTROID. To classify a new pixel patch, pick the nearest centroid (by hue): a nearest-centroid
classifier, one of the simplest real machine-learning models. It is TESTED on frames it did not learn from
(every other frame is held out), so the accuracy it reports is honest.

The saved model (colour_model.json in the dataset folder) plugs into graph_recipe('color_sort', palette=...): the
sorter then compares camera pixels with colours learned from your scene's own lighting, not hand-typed ones.
Standard library only (PNG decoding with zlib), so no numpy/Pillow install is needed.
"""

from __future__ import annotations

import ast
import json
import math
import re
import struct
import zlib
from pathlib import Path
from typing import Any

MAX_FRAMES = 200
MAX_PIXELS_PER_CLASS = 20000
_FRAME = re.compile(r"^rgb_(\d+)\.png$")
_SKIP_CLASSES = {"BACKGROUND", "UNLABELLED"}


def read_png(path: Path) -> tuple[int, int, int, bytes]:
    """(width, height, channels, raw rows) for an 8-bit RGB/RGBA, non-interlaced PNG (what BasicWriter writes)."""
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path.name} is not a PNG")
    pos, idat, header = 8, [], None
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        chunk = data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", chunk)
        elif kind == b"IDAT":
            idat.append(chunk)
        elif kind == b"IEND":
            break
        pos += 12 + length
    if header is None:
        raise ValueError(f"{path.name} has no header")
    width, height, depth, colour_type, _, _, interlace = header
    channels = {2: 3, 6: 4}.get(colour_type)
    if depth != 8 or channels is None or interlace:
        raise ValueError(f"{path.name}: only 8-bit RGB/RGBA non-interlaced PNGs are supported")
    raw = zlib.decompress(b"".join(idat))
    stride = width * channels
    out = bytearray(height * stride)
    previous = bytearray(stride)
    for row in range(height):
        start = row * (stride + 1)
        kind, line = raw[start], bytearray(raw[start + 1:start + 1 + stride])
        for i in range(stride):
            left = line[i - channels] if i >= channels else 0
            up = previous[i]
            if kind == 1:
                line[i] = (line[i] + left) & 255
            elif kind == 2:
                line[i] = (line[i] + up) & 255
            elif kind == 3:
                line[i] = (line[i] + ((left + up) >> 1)) & 255
            elif kind == 4:
                corner = previous[i - channels] if i >= channels else 0
                p = left + up - corner
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - corner)
                line[i] = (line[i] + (left if pa <= pb and pa <= pc else up if pb <= pc else corner)) & 255
        out[row * stride:(row + 1) * stride] = line
        previous = line
    return width, height, channels, bytes(out)


def _linear(value: int) -> float:
    c = value / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


_LINEAR = [_linear(v) for v in range(256)]


def _colourful(r: float, g: float, b: float) -> bool:
    top = max(r, g, b)
    return top > 0.02 and (top - min(r, g, b)) / top > 0.4


def _direction(rgb: list[float]) -> list[float]:
    length = math.sqrt(sum(v * v for v in rgb)) or 1.0
    return [v / length for v in rgb]


def nearest(rgb: list[float], centroids: dict[str, list[float]]) -> str | None:
    """The class whose centroid points the most the same way (hue), as the colour sorter does."""
    direction = _direction(rgb)
    best, score = None, -2.0
    for name, centroid in centroids.items():
        similarity = sum(a * b for a, b in zip(direction, _direction(centroid)))
        if similarity > score:
            best, score = name, similarity
    return best


def _frame_pixels(folder: Path, index: str) -> dict[str, list[list[float]]]:
    """class -> list of linear colourful RGB pixels in one frame."""
    w, h, c, rgb = read_png(folder / f"rgb_{index}.png")
    sw, sh, sc, seg = read_png(folder / f"semantic_segmentation_{index}.png")
    if (w, h) != (sw, sh):
        raise ValueError(f"frame {index}: rgb and segmentation sizes differ")
    labels = json.loads((folder / f"semantic_segmentation_labels_{index}.json").read_text(encoding="utf-8"))
    by_colour = {}
    for key, value in labels.items():
        name = (value or {}).get("class") if isinstance(value, dict) else None
        if name and name not in _SKIP_CLASSES:
            rgba = tuple(int(v) for v in ast.literal_eval(key))
            by_colour[rgba[:sc]] = name
    pixels: dict[str, list[list[float]]] = {}
    for i in range(w * h):
        name = by_colour.get(tuple(seg[i * sc:(i + 1) * sc]))
        if name is None:
            continue
        r, g, b = (_LINEAR[rgb[i * c + k]] for k in range(3))
        if _colourful(r, g, b):
            bucket = pixels.setdefault(name, [])
            if len(bucket) < MAX_PIXELS_PER_CLASS:
                bucket.append([r, g, b])
    return pixels


def train_colour_model(dataset_dir: str, *, dry_run: bool = True) -> dict[str, Any]:
    """Learn one colour per labelled class from an SDG dataset (rgb + semantic_segmentation), test it on held-out
    frames, and (dry_run=False) save colour_model.json in the dataset folder for color_sort's palette."""
    folder = Path(dataset_dir)
    if not folder.is_dir():
        return {"ok": False, "error": f"No folder {dataset_dir}."}
    frames = sorted(m.group(1) for p in folder.iterdir() if (m := _FRAME.match(p.name)))[:MAX_FRAMES]
    usable = [f for f in frames if (folder / f"semantic_segmentation_{f}.png").is_file()
              and (folder / f"semantic_segmentation_labels_{f}.json").is_file()]
    if not usable:
        return {"ok": False, "error": "No frame has rgb + semantic_segmentation + labels. Make the dataset with "
                "sdg_start(annotations=['rgb', 'semantic_segmentation']) and semantic labels on the objects."}
    train = usable[::2]
    test = usable[1::2] or usable[:1]
    sums: dict[str, list[float]] = {}
    counts: dict[str, int] = {}
    try:
        for index in train:
            for name, pixels in _frame_pixels(folder, index).items():
                total = sums.setdefault(name, [0.0, 0.0, 0.0])
                for pixel in pixels:
                    for k in range(3):
                        total[k] += pixel[k]
                counts[name] = counts.get(name, 0) + len(pixels)
        if not counts:
            return {"ok": False, "error": "The labelled objects have no coloured pixels (all grey/white?), so there "
                    "is nothing to learn. Give the objects colours, or label coloured objects."}
        centroids = {name: [round(v / counts[name], 5) for v in sums[name]] for name in sums}
        right = wrong = 0
        confusion: dict[str, dict[str, int]] = {}
        for index in test:
            for name, pixels in _frame_pixels(folder, index).items():
                for start in range(0, len(pixels) - 24, 25):  # judge 25-pixel patches, as the sorter does
                    patch = pixels[start:start + 25]
                    guess = nearest([sum(p[k] for p in patch) / 25 for k in range(3)], centroids)
                    confusion.setdefault(name, {}).setdefault(str(guess), 0)
                    confusion[name][str(guess)] += 1
                    right += guess == name
                    wrong += guess != name
    except (OSError, ValueError, SyntaxError) as exc:
        return {"ok": False, "error": f"Could not read the dataset: {exc}"}
    accuracy = round(100 * right / (right + wrong), 1) if right + wrong else None
    model = {"version": 1, "kind": "nearest_centroid_linear_rgb", "centroids": centroids,
             "pixels_per_class": counts, "trained_on_frames": train, "tested_on_frames": test,
             "test_accuracy_pct": accuracy}
    result = {"ok": True, "dry_run": dry_run is not False, "classes": sorted(centroids), "centroids": centroids,
              "pixels_per_class": counts, "test_accuracy_pct": accuracy, "confusion": confusion,
              "frames": {"train": len(train), "test": len(test)},
              "explain": "Each class's centroid is the average linear colour of its pixels in the training frames. "
                         "Accuracy is measured on the OTHER frames, in 5x5-pixel patches like the sorter's."}
    if accuracy is not None and accuracy < 80:
        result["warning"] = ("Under 80% on unseen frames: classes look alike in this lighting. Add domain "
                             "randomization (sdg randomize=...) or more distinct colours, and train again.")
    if result["dry_run"]:
        result["next_step"] = "Dry run: nothing saved. Call again with dry_run=False to write colour_model.json."
        return result
    path = folder / "colour_model.json"
    path.write_text(json.dumps(model, indent=1), encoding="utf-8")
    result["saved"] = str(path)
    result["next_step"] = ("graph_recipe('color_sort', ..., palette=<this file>) makes the sorter use these learned "
                           "colours; the route names must match the class names.")
    return result


def load_palette(value: Any) -> dict[str, list[float]]:
    """A palette from a colour_model.json path, or a {name: [r, g, b]} dict (linear colours)."""
    if isinstance(value, str):
        data = json.loads(Path(value).read_text(encoding="utf-8"))
        value = data.get("centroids") if isinstance(data, dict) else None
    if (not isinstance(value, dict) or not value or not all(
            isinstance(k, str) and isinstance(v, (list, tuple)) and len(v) == 3 for k, v in value.items())):
        raise ValueError("palette must be a colour_model.json path or {name: [r, g, b]}")
    return {k: [float(x) for x in v] for k, v in value.items()}
