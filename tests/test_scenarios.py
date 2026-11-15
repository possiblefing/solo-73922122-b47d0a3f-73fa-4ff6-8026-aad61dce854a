import json

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def jobs(*rows):
    return {"jobs": [dict(id=i, depends_on=list(d), start=s, end=e) for i, d, s, e in rows]}


def test_ten_second_chain_reports_full_saving():
    run = jobs(
        ("a", [], 0, 100),
        ("b", ["a"], 110, 200),
        ("c", ["b"], 210, 300),
    )
    body = client.post(
        "/api/analyze", json={"run": run, "job_id": "b", "seconds": 90}
    ).json()
    assert body["summary"]["original_completion"] == 300
    assert body["summary"]["predicted_completion"] == 210
    assert body["summary"]["saved_seconds"] == 90
    assert body["summary"]["fully_realised"] is True
    assert [row["id"] for row in body["critical_chain"]] == ["a", "b", "c"]


def test_waits_shift_downstream_but_not_beyond_parallel_branch():
    run = jobs(
        ("a", [], 0, 100),
        ("b", [], 0, 90),
        ("c", ["a", "b"], 130, 200),
    )
    body = client.post(
        "/api/analyze", json={"run": run, "job_id": "a", "seconds": 50}
    ).json()
    summary = body["summary"]
    rows = {row["id"]: row for row in body["jobs"]}
    # c waited 30s after max(a=100, b=90); after a drops to 50, its readiness
    # is b's 90, so it starts at 120 and saves only 10s -- not the 50 requested.
    assert rows["c"]["predicted_start"] == 120
    assert summary["predicted_completion"] == 190
    assert summary["saved_seconds"] == 10
    assert summary["saving_capped_at"]["job"] == "c"
    assert summary["saving_capped_at"]["limiting_dependency"] == "b"


def test_target_duration_is_echoed():
    run = jobs(("a", [], 0, 100), ("b", ["a"], 100, 160))
    body = client.post(
        "/api/analyze", json={"run": run, "job_id": "a", "seconds": 25}
    ).json()
    assert body["summary"]["target_original_duration"] == 100
    assert body["summary"]["target_new_duration"] == 75
    assert body["summary"]["requested_seconds"] == 25


def test_error_fields_point_at_the_problem():
    run = jobs(("a", [], 0, 100), ("b", ["ghost"], 100, 160))
    body = client.post(
        "/api/analyze", json={"run": run, "job_id": "a", "seconds": 25}
    ).json()
    assert body["errors"][0]["field"] == "jobs[1].depends_on"


def test_missing_shrink_fields_error_cleanly():
    run = jobs(("a", [], 0, 100), ("b", ["a"], 100, 160))
    body = client.post("/api/inspect", json={"run": run}).json()
    assert body["ok"] is True
    response = client.post("/api/analyze", json={"run": run, "job_id": "a"})
    assert response.status_code == 422
    assert response.json()["errors"][0]["field"] == "shorten.seconds"


def test_job_ids_with_unicode_and_spaces():
    run = json.loads(
        '{"jobs": ['
        '{"id": "抽取 订单", "depends_on": [], "start": 0, "end": 100},'
        '{"id": "清洗/订单", "depends_on": ["抽取 订单"], "start": 120, "end": 200}'
        "]}"
    )
    body = client.post(
        "/api/analyze", json={"run": run, "job_id": "抽取 订单", "seconds": 20}
    ).json()
    assert body["summary"]["saved_seconds"] == 20


def test_diamond_dependency():
    run = jobs(
        ("root", [], 0, 60),
        ("left", ["root"], 60, 200),
        ("right", ["root"], 60, 150),
        ("join", ["left", "right"], 200, 260),
    )
    # right is not the binding input at join, so shortening it saves nothing.
    body = client.post(
        "/api/analyze", json={"run": run, "job_id": "right", "seconds": 90}
    ).json()
    assert body["summary"]["saved_seconds"] == 0
    assert [row["id"] for row in body["critical_chain"]] == ["root", "left", "join"]
    # Shortening left by 100 lets right (150) take over; join keeps waiting 0s
    # after readiness, starting at 150 instead of 200 -> 50s realised.
    body = client.post(
        "/api/analyze", json={"run": run, "job_id": "left", "seconds": 100}
    ).json()
    assert body["summary"]["saved_seconds"] == 50
    assert body["summary"]["predicted_bottleneck"] == "join"
    assert body["summary"]["saving_capped_at"]["limiting_dependency"] == "right"


def test_chain_never_contains_duplicate_nodes():
    run = jobs(
        ("a", [], 0, 10),
        ("b", ["a"], 10, 20),
        ("c", ["a"], 10, 20),
        ("d", ["b", "c"], 20, 30),
    )
    body = client.post(
        "/api/analyze", json={"run": run, "job_id": "a", "seconds": 5}
    ).json()
    ids = [row["id"] for row in body["critical_chain"]]
    assert len(ids) == len(set(ids)) == 3
    assert ids[-1] == body["summary"]["predicted_bottleneck"]
