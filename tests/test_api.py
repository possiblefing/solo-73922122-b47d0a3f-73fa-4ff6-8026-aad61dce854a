"""HTTP-level tests for the planner API."""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

JOBS = [
    {"id": "extract", "depends_on": [], "start": 0, "end": 600},
    {"id": "clean", "depends_on": ["extract"], "start": 660, "end": 1000},
    {"id": "load", "depends_on": ["clean"], "start": 1060, "end": 1300},
]


def test_health():
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_index_page_is_served():
    response = client.get("/")
    assert response.status_code == 200
    assert "夜间入仓作业加速测算" in response.text
    assert "/static/app.js" in response.text


def test_static_assets_are_served():
    for path in ("/static/app.js", "/static/styles.css"):
        response = client.get(path)
        assert response.status_code == 200
        assert len(response.content) > 200


def test_inspect_returns_jobs_and_completion():
    response = client.post("/api/inspect", json={"run": {"jobs": JOBS}})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["job_count"] == 3
    assert body["original_completion"] == 1300
    assert [job["id"] for job in body["jobs"]] == ["extract", "clean", "load"]
    assert body["jobs"][1]["depends_on"] == ["extract"]


def test_analyze_returns_prediction():
    response = client.post(
        "/api/analyze",
        json={"run": {"jobs": JOBS}, "job_id": "extract", "seconds": 100},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["summary"]["original_completion"] == 1300
    assert body["summary"]["predicted_completion"] == 1200
    assert body["summary"]["saved_seconds"] == 100
    assert len(body["critical_chain"]) == 3


def test_analyze_invalid_record_has_no_prediction():
    jobs = copy.deepcopy(JOBS)
    jobs[1]["depends_on"] = ["missing"]
    response = client.post(
        "/api/analyze",
        json={"run": {"jobs": jobs}, "job_id": "extract", "seconds": 10},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["ok"] is False
    assert "prediction" not in body
    assert "summary" not in body
    assert any("不存在" in error["message"] for error in body["errors"])


def test_inspect_rejects_cycle():
    jobs = copy.deepcopy(JOBS)
    jobs[0]["depends_on"] = ["load"]
    response = client.post("/api/inspect", json={"run": {"jobs": jobs}})
    assert response.status_code == 422
    body = response.json()
    assert body["ok"] is False
    assert any("环" in error["message"] for error in body["errors"])


def test_analyze_rejects_too_much_shortening():
    response = client.post(
        "/api/analyze",
        json={"run": {"jobs": JOBS}, "job_id": "clean", "seconds": 500},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["errors"][0]["field"] == "shorten.seconds"


def test_analyze_rejects_malformed_json():
    response = client.post(
        "/api/analyze",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["ok"] is False


def test_analyze_rejects_non_object_body():
    response = client.post("/api/analyze", json=["nope"])
    assert response.status_code == 422
    assert response.json()["ok"] is False


def test_inspect_rejects_bad_timing():
    jobs = copy.deepcopy(JOBS)
    jobs[1]["start"] = 500  # before extract ends at 600
    response = client.post("/api/inspect", json={"run": {"jobs": jobs}})
    assert response.status_code == 422
    assert response.json()["errors"][0]["field"] == "jobs[1].start"


@pytest.mark.parametrize("seconds", [0, -1, 1e300])
def test_analyze_rejects_out_of_range_seconds(seconds):
    response = client.post(
        "/api/analyze",
        json={"run": {"jobs": JOBS}, "job_id": "clean", "seconds": seconds},
    )
    assert response.status_code == 422
    assert response.json()["ok"] is False
