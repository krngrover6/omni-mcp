"""click_snapshot / click_changes (what a click wrote, explained) and personal flashcards (Leitner boxes)."""
from __future__ import annotations

try:
    from pxr import Sdf, Usd, UsdGeom
except ImportError:  # pragma: no cover
    import unittest

    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

import datetime as dt
from unittest import mock

from omniverse_scene_mcp import click_watch, features, study_memory


def test_click_changes_shows_and_explains_what_was_written():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/World")
    assert not click_watch.click_changes(stage)["ok"]  # no snapshot yet
    snap = click_watch.click_snapshot(stage)
    assert snap["ok"] and snap["read_only"]
    assert click_watch.click_changes(stage)["changed_layers"] == 0
    UsdGeom.Cube.Define(stage, "/World/Box").GetSizeAttr().Set(50.0)  # what Create > Shape > Cube + typing does
    result = click_watch.click_changes(stage)
    root = result["changes"][0]
    assert root["status"] == "changed" and root["added"] >= 3 and root["removed"] == 0
    added = {e["text"].strip(): e for e in root["lines"] if e["change"] == "added"}
    assert "`/World/Box`" in added['def Cube "Box"']["means"]
    assert "fallback is 2.0" in added["double size = 50"]["means"]
    sub = Sdf.Layer.CreateAnonymous("lighting.usda")
    stage.GetRootLayer().subLayerPaths.append(sub.identifier)
    statuses = [c["status"] for c in click_watch.click_changes(stage)["changes"]]
    assert "added since the snapshot" in statuses
    assert features.FEATURES["click_changes"].location == "stage" and not features.FEATURES["click_changes"].mutates


def test_session_layer_is_watched_only_when_asked():
    stage = Usd.Stage.CreateInMemory()
    click_watch.click_snapshot(stage)
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        UsdGeom.Xform.Define(stage, "/Temp")
    assert click_watch.click_changes(stage)["changed_layers"] == 0
    click_watch.click_snapshot(stage, include_session=True)
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        UsdGeom.Xform.Define(stage, "/Temp2")
    changed = click_watch.click_changes(stage)["changes"]
    assert changed and changed[0]["layer"].startswith("session layer")


def test_cards_move_through_boxes_and_hide_the_back():
    plan = study_memory.add_card("What does uniform mean?", "One value for all time: it cannot be animated.",
                                 tags="day4, attributes", source="Day 4 tool quiz")
    assert plan["dry_run"] and study_memory.due_cards()["due_count"] == 0
    card = study_memory.add_card("What does uniform mean?", "One value for all time: it cannot be animated.",
                                 tags="day4", dry_run=False)["card"]
    due = study_memory.due_cards()
    assert due["due_count"] == 1 and "back" not in due["cards"][0]
    right = study_memory.answer_card(card["id"], True)
    assert right["box"] == 2 and right["back"].startswith("One value")
    assert study_memory.due_cards()["due_count"] == 0  # next due in 2 days
    later = dt.date.today() + dt.timedelta(days=2)
    with mock.patch.object(study_memory, "_today", return_value=later):
        assert study_memory.due_cards()["due_count"] == 1
        wrong = study_memory.answer_card(card["id"], False)
        assert wrong["box"] == 1 and wrong["due"] == (later + dt.timedelta(days=1)).isoformat()
    assert not study_memory.answer_card(999, True)["ok"]
    assert not study_memory.answer_card(card["id"], "yes")["ok"]
    assert features.FEATURES["card_add"].mutates and not features.FEATURES["card_due"].mutates
