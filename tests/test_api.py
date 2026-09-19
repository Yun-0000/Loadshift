from fastapi.testclient import TestClient

from loadshift.app import app
from loadshift.tasks import Task


client = TestClient(app)


def test_plan_rejects_noncontiguous_forbidden_windows_with_relax_list():
    task = {
        "id": "dishwasher",
        "name": "Dishwasher",
        "power_w": 1800,
        "duration_hours": 2.0,
        "original_start": "16:00",
        "must_finish_by": "24:00",
        "inconvenient_windows": [
            {"start": "00:00", "end": "10:00"},
            {"start": "11:00", "end": "14:00"},
            {"start": "15:00", "end": "24:00"},
        ],
        "status": "pending",
    }
    res = client.post("/api/plan", json={"tasks": [task]})
    assert res.status_code == 409
    detail = res.json()["detail"]
    assert detail["error"] == "infeasible"
    assert detail["relax"]


def test_replan_history_roundtrip_keeps_first_window():
    demo = client.get("/api/demo").json()
    tasks = demo["tasks"]
    first = client.post(
        "/api/replan",
        json={
            "tasks": tasks,
            "task_id": "dishwasher",
            "reason": "doesnt_work",
            "unavailable_start": "10:00",
            "unavailable_end": "12:00",
        },
    )
    assert first.status_code == 200
    body = first.json()
    dish = next(t for t in body["revised_plan"]["when_to_run"] if t["id"] == "dishwasher")
    assert dish["planned_start"] >= "12:00" or dish["planned_end"] <= "10:00"
    second = client.post(
        "/api/replan",
        json={
            "tasks": tasks,
            "task_id": "laundry",
            "reason": "doesnt_work",
            "unavailable_start": "20:00",
            "unavailable_end": "21:00",
            "previous_plan": body["revised_plan"],
            "constraint_history": body["constraint_history"],
        },
    )
    assert second.status_code == 200
    body2 = second.json()
    dish2 = next(t for t in body2["revised_plan"]["when_to_run"] if t["id"] == "dishwasher")
    blocked = set(range(20, 24))  # 10:00-12:00
    assert not set(dish2["steps"]) & blocked
    assert body2["previous_plan"]["totals"]["bill_usd"] == body["revised_plan"]["totals"]["bill_usd"]
