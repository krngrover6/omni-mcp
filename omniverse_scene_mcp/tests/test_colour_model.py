"""Colour detector training from SDG frames (stdlib PNG decoding), tested on held-out frames, used by color_sort."""
from __future__ import annotations

import json
import struct
import tempfile
import zlib
from pathlib import Path

from omniverse_scene_mcp import colour_model as cm, features, omnigraph_helper as ogh

W, H = 40, 30
BLUE, GREEN = (60, 110, 230), (70, 200, 100)


def _png(path, pixels, filters=(0,)):
    """Write an 8-bit RGBA PNG; rows use the given filter types in turn (0 none, 1 sub, 2 up)."""
    rows, previous = b"", bytes(W * 4)
    for y in range(H):
        line = b"".join(bytes(pixels[y * W + x]) for x in range(W))
        kind = filters[y % len(filters)]
        if kind == 1:
            encoded = bytes((line[i] - (line[i - 4] if i >= 4 else 0)) & 255 for i in range(len(line)))
        elif kind == 2:
            encoded = bytes((line[i] - previous[i]) & 255 for i in range(len(line)))
        else:
            encoded = line
        rows += bytes([kind]) + encoded
        previous = line

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def _dataset(folder, frames=4):
    for f in range(frames):
        shade = 0.8 + 0.1 * f  # lighting changes per frame
        rgb, seg = [], []
        for y in range(H):
            for x in range(W):
                if x < 15:
                    colour, label = BLUE, (10, 20, 200, 255)
                elif x < 30:
                    colour, label = GREEN, (30, 200, 40, 255)
                else:
                    colour, label = (150, 150, 150), (0, 0, 0, 0)
                rgb.append(tuple(min(255, int(c * shade)) for c in colour) + (255,))
                seg.append(label)
        index = f"{f:04d}"
        _png(folder / f"rgb_{index}.png", rgb, filters=(0, 1, 2))
        _png(folder / f"semantic_segmentation_{index}.png", seg, filters=(2, 0))
        (folder / f"semantic_segmentation_labels_{index}.json").write_text(json.dumps({
            "(0, 0, 0, 0)": {"class": "BACKGROUND"}, "(10, 20, 200, 255)": {"class": "blue_box"},
            "(30, 200, 40, 255)": {"class": "green_box"}}), encoding="utf-8")


def test_png_reader_round_trips_every_filter():
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "x.png"
        pixels = [((x * 7) % 256, (y * 11) % 256, (x + y) % 256, 255) for y in range(H) for x in range(W)]
        _png(path, pixels, filters=(0, 1, 2))
        width, height, channels, raw = cm.read_png(path)
        assert (width, height, channels) == (W, H, 4)
        assert [tuple(raw[i * 4:i * 4 + 4]) for i in range(W * H)] == pixels


def test_training_learns_each_class_scores_unseen_frames_and_feeds_the_sorter():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        _dataset(root)
        preview = cm.train_colour_model(folder)
        assert preview["ok"] and preview["dry_run"] and not (root / "colour_model.json").exists(), preview
        assert preview["classes"] == ["blue_box", "green_box"] and preview["test_accuracy_pct"] == 100.0
        assert preview["frames"] == {"train": 2, "test": 2}
        blue = preview["centroids"]["blue_box"]
        assert blue[2] > blue[1] > blue[0]  # learned in linear colour: blue strongest
        saved = cm.train_colour_model(folder, dry_run=False)
        assert Path(saved["saved"]).is_file()
        palette = cm.load_palette(saved["saved"])
        assert cm.nearest([0.05, 0.15, 0.8], palette) == "blue_box"
        plan = ogh.plan_recipe("color_sort", stage=None, bins={"blue_box": "/World/BinA", "green_box": "/World/BinB"},
                               zone_min=[-1, -1, 0], zone_max=[1, 1, 1], palette=saved["saved"])
        assert plan["ok"], plan
        value = next(v["value"] for v in plan["values"] if v["attribute"] == "Sorter.inputs:palette")
        assert json.loads(value) == palette
        wrong = ogh.plan_recipe("color_sort", stage=None, bins={"red": "/World/BinA"}, zone_min=[-1, -1, 0],
                                zone_max=[1, 1, 1], palette=saved["saved"])
        assert not wrong["ok"] and "red" in wrong["error"]
    assert not cm.train_colour_model(tempfile.mkdtemp())["ok"]
    assert features.FEATURES["sdg_train_colour_model"].mutates
