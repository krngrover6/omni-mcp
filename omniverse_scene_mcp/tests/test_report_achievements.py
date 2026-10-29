"""Project report achievements: datasets, trained models and recordings found on disk, shown in the report."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from omniverse_scene_mcp import project_report as pr


def test_achievements_are_collected_and_rendered():
    workspace = Path(tempfile.mkdtemp())
    project = workspace / "cell"
    (project / "dr_run").mkdir(parents=True)
    for i in range(3):
        (project / "dr_run" / f"rgb_{i:04d}.png").write_bytes(b"png")
    (project / "dr_run" / "dataset_manifest.json").write_text(json.dumps({"randomize": {"prims": ["/A"]}}))
    (project / "dr_run" / "colour_model.json").write_text(json.dumps({"centroids": {"red": [1, 0, 0]},
                                                                       "test_accuracy_pct": 97.5}))
    (project / "empty_run").mkdir()
    rec = workspace / "isaac_study" / "recordings"
    rec.mkdir(parents=True)
    (rec / "pick.json").write_text(json.dumps({"robot_root": "/World/Franka", "samples": [{"t": 0}, {"t": 44.6}]}))
    found = pr.collect_achievements(project, workspace)
    text = " | ".join(found)
    assert "3 rendered frame(s), with domain randomization" in text and "97.5% on held-out frames" in text
    assert "1 robot motion run(s) of /World/Franka (longest 45 s)" in text and "empty_run" not in text
    report = pr.build_report(project_dir=project, achievements=found, title="Factory cell")
    assert report["ok"] and "What I built and proved" in report["markdown"] and "97.5%" in report["html"]
    assert pr.collect_achievements(None, tempfile.mkdtemp()) == []
