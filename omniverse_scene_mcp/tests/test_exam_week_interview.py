"""Exam week planning and interview coaching are pure functions; nothing is saved."""
import json

from omniverse_scene_mcp.exam import exam_week, interview


def _data(today="2026-10-29", exam="2026-11-20", accuracy=None, mocks=0, best=None):
    accuracy = accuracy or {}
    weights = {"Composition": 23, "Content Aggregation": 10, "Customizing USD": 6, "Data Exchange": 15,
               "Data Modeling": 13, "Debugging and Troubleshooting": 11, "Pipeline Development": 14, "Visualization": 8}
    return {"today": today, "target_pct": 80.0, "exam": {"time_limit_minutes": 120, "questions": "60-70"},
            "plan": {"exam_date": exam}, "due_reviews": 0, "totals": {"weighted_accuracy_pct": None},
            "mocks": {"finished_count": mocks, "best_pct": best},
            "domains": [{"name": name, "weight": weight, "accuracy_pct": accuracy.get(name), "in_blueprint": True}
                        for name, weight in weights.items()]}


def test_exam_week_before_the_week_points_to_its_start_and_plans_eight_days():
    week = exam_week.exam_week(_data())
    assert week["ok"] and week["days_left"] == 22 and "starts on 2026-11-13" in week["where_you_are"], week
    assert [day["date"] for day in week["schedule"]][0] == "2026-11-13" and week["schedule"][-1]["title"] == "Exam day"
    assert [day["title"] for day in week["schedule"]].count("Full mock exam #1") == 1
    assert not week["readiness"]["ready"] and any("No full mock" in r for r in week["readiness"]["reasons"])
    json.dumps(week, allow_nan=False)


def test_exam_week_targets_the_domains_with_most_marks_at_risk_and_marks_today():
    accuracy = {name: 95.0 for name in ("Composition", "Content Aggregation", "Customizing USD", "Data Modeling",
                                        "Debugging and Troubleshooting", "Visualization")}
    accuracy.update({"Data Exchange": 40.0, "Pipeline Development": 50.0})
    week = exam_week.exam_week(_data(today="2026-11-14", accuracy=accuracy, mocks=1, best=72.0))
    assert [row["name"] for row in week["focus_domains"]][:2] == ["Data Exchange", "Pipeline Development"]
    today = [day for day in week["schedule"] if day["is_today"]]
    assert len(today) == 1 and today[0]["title"] == "Review mock #1" and "Data Exchange" in " ".join(today[0]["steps"])
    assert "in exam week" in week["where_you_are"]
    reasons = " ".join(week["readiness"]["reasons"])
    assert "72.0% is under" in reasons and "Data Exchange 40%" in reasons


def test_readiness_passes_only_when_mock_and_every_domain_clear_the_bar():
    accuracy = {row["name"]: 85.0 for row in _data()["domains"]}
    assert exam_week.readiness(_data(accuracy=accuracy, mocks=2, best=84.0))["ready"]
    assert not exam_week.exam_week(_data(exam=None))["ok"]
    assert not exam_week.exam_week(_data(), days=30)["ok"]
    assert "has passed" in exam_week.exam_week(_data(today="2026-12-01"))["where_you_are"]


def test_interview_questions_withhold_answers_and_filter():
    listed = interview.questions(topic="composition", count=3, seed=1)
    assert listed["ok"] and listed["questions"] and all(q["topic"] == "composition" for q in listed["questions"])
    assert all("model_answer" not in q and "key_points" not in q for q in listed["questions"])
    assert not interview.questions(topic="cooking")["ok"] and not interview.questions(level="senior")["ok"]
    assert interview.questions(seed=3)["questions"] == interview.questions(seed=3)["questions"]
    assert len({q["id"] for q in interview.QUESTIONS}) == len(interview.QUESTIONS)


def test_interview_grading_names_covered_and_missed_points():
    strong = interview.grade("ref-vs-payload", (
        "Both a reference and a payload bring in another file. The difference is that a payload can be unloaded, "
        "so for example a heavy warehouse asset stays out of memory until I load it. Payloads are also weaker than "
        "references in LIVRPS strength order."))
    assert strong["ok"] and strong["score_pct"] == 100 and strong["rating"] == "strong" and not strong["missed"]
    weak = interview.grade("ref-vs-payload", "A payload is lazy.")
    assert weak["rating"] == "needs work" and len(weak["missed"]) == 3 and any("words" in t for t in weak["tips"])
    assert weak["model_answer"] and weak["follow_up"]
    assert not interview.grade("nope", "x")["ok"] and not interview.grade("livrps", "  ")["ok"]
