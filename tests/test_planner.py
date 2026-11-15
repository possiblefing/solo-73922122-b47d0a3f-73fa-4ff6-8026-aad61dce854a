"""Tests for validation and the what-if reschedule."""

from __future__ import annotations

import copy

import pytest

from app.planner import inspect_run, validate_and_plan

SAMPLE = {
    "jobs": [
        {"id": "extract_orders", "depends_on": [], "start": 0, "end": 600},
        {"id": "extract_users", "depends_on": [], "start": 0, "end": 420},
        {"id": "extract_goods", "depends_on": [], "start": 0, "end": 300},
        {"id": "clean_orders", "depends_on": ["extract_orders"], "start": 660, "end": 1100},
        {"id": "clean_users", "depends_on": ["extract_users"], "start": 480, "end": 720},
        {"id": "clean_goods", "depends_on": ["extract_goods"], "start": 360, "end": 540},
        {"id": "join_sales", "depends_on": ["clean_orders", "clean_users"], "start": 1160, "end": 1520},
        {"id": "dim_goods", "depends_on": ["clean_goods"], "start": 600, "end": 880},
        {"id": "enrich", "depends_on": ["join_sales", "dim_goods"], "start": 1580, "end": 1820},
        {"id": "agg_daily", "depends_on": ["enrich"], "start": 1880, "end": 2200},
        {"id": "agg_region", "depends_on": ["enrich"], "start": 1880, "end": 2100},
        {"id": "quality_check", "depends_on": ["agg_daily", "agg_region"], "start": 2260, "end": 2400},
        {"id": "export_bi", "depends_on": ["quality_check"], "start": 2460, "end": 2700},
        {"id": "export_ads", "depends_on": ["agg_region"], "start": 2160, "end": 2350},
        {"id": "notify", "depends_on": ["export_bi", "export_ads"], "start": 2760, "end": 2800},
    ]
}


def run(jobs=None):
    return {"jobs": jobs if jobs is not None else copy.deepcopy(SAMPLE["jobs"])}


def analyze(jobs=None, job_id="export_bi", seconds=100):
    return validate_and_plan(run(jobs), job_id, seconds)


def test_sample_is_valid_and_completes_at_2800():
    result, errors = inspect_run(run())
    assert errors == []
    assert result["ok"] is True
    assert result["job_count"] == 15
    assert result["original_completion"] == 2800


def test_shortening_bottleneck_saves_exactly_requested():
    result, errors = analyze(seconds=100)
    assert errors == []
    summary = result["summary"]
    assert summary["original_completion"] == 2800
    assert summary["predicted_completion"] == 2700
    assert summary["saved_seconds"] == 100
    assert summary["fully_realised"] is True
    assert summary["bottleneck_changed"] is False
    assert summary["predicted_bottleneck"] == "notify"


def test_waits_are_preserved_not_recompressed():
    # clean_orders waits 60s after extract_orders; join_sales waits 60s after
    # clean_orders.  Shortening extract_orders by 100 moves its end to 500.
    result, _ = analyze(job_id="extract_orders", seconds=100)
    rows = {row["id"]: row for row in result["jobs"]}
    assert rows["extract_orders"]["predicted_end"] == 500
    assert rows["clean_orders"]["predicted_start"] == 560  # 500 + 60 observed wait
    # join_sales' binding input moves from 1100 to 1000; it keeps its 60s wait.
    assert rows["join_sales"]["predicted_start"] == 1060


def test_parallel_branch_takes_over_and_caps_the_saving():
    # quality_check is bounded by agg_daily (2200) and agg_region (2100).
    # Shortening agg_daily by 300 makes it end at 1900, so agg_region (2100)
    # becomes the new binding input: quality_check starts at 2100 + 60 = 2160,
    # 100s earlier instead of 300s earlier.
    result, _ = analyze(job_id="agg_daily", seconds=300)
    summary = result["summary"]
    rows = {row["id"]: row for row in result["jobs"]}
    assert rows["agg_daily"]["predicted_end"] == 1900
    assert rows["quality_check"]["predicted_start"] == 2160
    assert summary["predicted_completion"] == 2700
    assert summary["saved_seconds"] == 100
    assert summary["fully_realised"] is False


def test_batch_bottleneck_can_move_to_a_parallel_job():
    # Two independent leaves: x ends at 300, y at 280.  Removing 100s from x
    # makes y (280) the new batch bottleneck, so only 20s are realised.
    jobs = [
        {"id": "x", "depends_on": [], "start": 0, "end": 300},
        {"id": "y", "depends_on": [], "start": 0, "end": 280},
    ]
    result, errors = analyze(jobs=jobs, job_id="x", seconds=100)
    assert errors == []
    summary = result["summary"]
    assert summary["bottleneck_changed"] is True
    assert summary["original_bottleneck"] == "x"
    assert summary["predicted_bottleneck"] == "y"
    assert summary["predicted_completion"] == 280
    assert summary["saved_seconds"] == 20
    assert summary["fully_realised"] is False
    assert [row["id"] for row in result["critical_chain"]] == ["y"]


def test_downstream_chain_shift_is_propagated_end_to_end():
    result, _ = analyze(job_id="clean_users", seconds=200)
    rows = {row["id"]: row for row in result["jobs"]}
    # clean_users 480->720 waits 60 after extract_users (420).
    assert rows["clean_users"]["predicted_end"] == 520
    # join_sales waited 60 after 1100 (clean_orders); still 1100. unchanged.
    assert rows["join_sales"]["predicted_start"] == 1160
    assert result["summary"]["saved_seconds"] == 0


def test_critical_chain_is_a_real_dependency_path_ending_at_bottleneck():
    result, _ = analyze(seconds=50)
    chain = result["critical_chain"]
    assert chain[-1]["id"] == result["summary"]["predicted_bottleneck"]
    for upstream, downstream in zip(chain, chain[1:]):
        assert upstream["id"] in downstream["depends_on"]
    assert chain[0]["depends_on"] == []
    assert result["summary"]["chain_includes_target"] is True


def test_shortening_more_than_duration_is_rejected():
    result, errors = analyze(seconds=301)  # export_bi lasts 240s
    assert result is None
    assert errors[0]["field"] == "shorten.seconds"
    assert "超过" in errors[0]["message"]


def test_equal_to_duration_is_allowed_and_zeroes_the_job():
    result, errors = analyze(seconds=240)
    assert errors == []
    rows = {row["id"]: row for row in result["jobs"]}
    assert rows["export_bi"]["predicted_end"] == rows["export_bi"]["predicted_start"]
    assert rows["export_bi"]["predicted_duration"] == 0


@pytest.mark.parametrize("seconds", [0, -5, "100", None, True, float("nan")])
def test_bad_seconds_are_rejected(seconds):
    result, errors = analyze(seconds=seconds)
    assert result is None
    assert errors[0]["field"] == "shorten.seconds"


def test_unknown_target_is_rejected():
    result, errors = analyze(job_id="nope", seconds=10)
    assert result is None
    assert errors[0]["field"] == "shorten.job_id"


def test_unknown_dependency_is_rejected():
    jobs = copy.deepcopy(SAMPLE["jobs"])
    jobs[1]["depends_on"] = ["ghost"]
    result, errors = analyze(jobs=jobs, seconds=10)
    assert result is None
    assert any("不存在" in error["message"] for error in errors)


def test_self_dependency_is_rejected():
    jobs = copy.deepcopy(SAMPLE["jobs"])
    jobs[0]["depends_on"] = ["extract_orders"]
    result, errors = analyze(jobs=jobs, seconds=10)
    assert result is None
    assert any("不能依赖自身" in error["message"] for error in errors)


def test_two_node_cycle_is_rejected_with_the_cycle_named():
    jobs = copy.deepcopy(SAMPLE["jobs"])
    first = next(job for job in jobs if job["id"] == "extract_orders")
    second = next(job for job in jobs if job["id"] == "clean_orders")
    first["depends_on"] = ["clean_orders"]
    second["depends_on"] = ["extract_orders"]
    result, errors = analyze(jobs=jobs, seconds=10)
    assert result is None
    assert any("环" in error["message"] for error in errors)
    message = next(error["message"] for error in errors if "环" in error["message"])
    assert "extract_orders" in message and "clean_orders" in message


def test_longer_cycle_is_rejected():
    jobs = copy.deepcopy(SAMPLE["jobs"])
    jobs[0]["depends_on"] = ["notify"]
    jobs[14]["depends_on"] = ["export_bi", "agg_region", "extract_orders"]
    result, errors = analyze(jobs=jobs, seconds=10)
    assert result is None
    assert any("环" in error["message"] for error in errors)


def test_job_starting_before_upstream_finishes_is_rejected():
    jobs = copy.deepcopy(SAMPLE["jobs"])
    jobs[3]["start"] = 500  # extract_orders ends at 600
    result, errors = analyze(jobs=jobs, seconds=10)
    assert result is None
    assert errors[0]["field"] == "jobs[3].start"
    assert "早于上游" in errors[0]["message"]


def test_negative_start_is_rejected():
    jobs = copy.deepcopy(SAMPLE["jobs"])
    jobs[0]["start"] = -1
    result, errors = analyze(jobs=jobs, seconds=10)
    assert result is None
    assert any("批次起点" in error["message"] for error in errors)


def test_end_not_after_start_is_rejected():
    jobs = copy.deepcopy(SAMPLE["jobs"])
    jobs[0]["end"] = jobs[0]["start"]
    result, errors = analyze(jobs=jobs, seconds=10)
    assert result is None
    assert any("晚于" in error["message"] for error in errors)


@pytest.mark.parametrize("count", [1, 31])
def test_job_count_bounds(count):
    jobs = [
        {"id": f"j{i}", "depends_on": [], "start": 0, "end": 10} for i in range(count)
    ]
    result, errors = analyze(jobs=jobs, job_id="j0", seconds=1)
    assert result is None
    assert any("2～30" in error["message"] for error in errors)


def test_two_jobs_is_the_minimum():
    jobs = [
        {"id": "a", "depends_on": [], "start": 0, "end": 10},
        {"id": "b", "depends_on": ["a"], "start": 12, "end": 20},
    ]
    result, errors = analyze(jobs=jobs, job_id="a", seconds=5)
    assert errors == []
    assert result["summary"]["saved_seconds"] == 5


def test_thirty_jobs_is_allowed():
    jobs = [{"id": "j0", "depends_on": [], "start": 0, "end": 10}]
    for i in range(1, 30):
        jobs.append(
            {"id": f"j{i}", "depends_on": [f"j{i - 1}"], "start": i * 10, "end": i * 10 + 10}
        )
    result, errors = analyze(jobs=jobs, job_id="j0", seconds=4)
    assert errors == []
    assert result["summary"]["saved_seconds"] == 4


def test_duplicate_ids_are_rejected():
    jobs = copy.deepcopy(SAMPLE["jobs"])
    jobs[1]["id"] = jobs[0]["id"]
    result, errors = analyze(jobs=jobs, seconds=10)
    assert result is None
    assert any("重复" in error["message"] for error in errors)


def test_missing_dependency_field_means_no_dependencies():
    jobs = [
        {"id": "a", "start": 0, "end": 10},
        {"id": "b", "start": 20, "end": 30},
    ]
    result, errors = inspect_run({"jobs": jobs})
    assert errors == []
    assert result["jobs"][0]["depends_on"] == []


def test_dependencies_alias_is_accepted():
    jobs = [
        {"id": "a", "start": 0, "end": 10},
        {"id": "b", "dependencies": ["a"], "start": 20, "end": 30},
    ]
    result, errors = inspect_run({"jobs": jobs})
    assert errors == []
    assert result["jobs"][1]["depends_on"] == ["a"]


def test_zero_wait_is_handled():
    jobs = [
        {"id": "a", "depends_on": [], "start": 0, "end": 100},
        {"id": "b", "depends_on": ["a"], "start": 100, "end": 200},
    ]
    result, errors = analyze(jobs=jobs, job_id="a", seconds=10)
    assert errors == []
    assert result["summary"]["saved_seconds"] == 10


def test_fractional_seconds_are_supported():
    jobs = [
        {"id": "a", "depends_on": [], "start": 0, "end": 10.5},
        {"id": "b", "depends_on": ["a"], "start": 11, "end": 20.5},
    ]
    result, errors = analyze(jobs=jobs, job_id="a", seconds=0.5)
    assert errors == []
    assert result["summary"]["saved_seconds"] == 0.5


def test_large_but_sane_timeline():
    jobs = [
        {"id": "a", "depends_on": [], "start": 0, "end": 86400},
        {"id": "b", "depends_on": ["a"], "start": 86410, "end": 90000},
    ]
    result, errors = analyze(jobs=jobs, job_id="a", seconds=600)
    assert errors == []
    assert result["summary"]["saved_seconds"] == 600


def test_non_object_body():
    result, errors = validate_and_plan(["not", "a", "dict"], "a", 1)
    assert result is None
    assert errors[0]["field"] == "run"


def test_jobs_not_a_list():
    result, errors = validate_and_plan({"jobs": "nope"}, "a", 1)
    assert result is None
    assert errors[0]["field"] == "jobs"


def test_boolean_timestamps_are_rejected():
    jobs = [
        {"id": "a", "depends_on": [], "start": False, "end": 10},
        {"id": "b", "depends_on": [], "start": 0, "end": 10},
    ]
    result, errors = validate_and_plan({"jobs": jobs}, "a", 1)
    assert result is None
    assert any(".start" in error["field"] for error in errors)


def test_independent_jobs_shortened_job_does_not_affect_batch():
    # Two independent jobs; the shorter one cannot move the batch end.
    jobs = [
        {"id": "fast", "depends_on": [], "start": 0, "end": 100},
        {"id": "slow", "depends_on": [], "start": 0, "end": 200},
    ]
    result, errors = analyze(jobs=jobs, job_id="fast", seconds=50)
    assert errors == []
    summary = result["summary"]
    assert summary["saved_seconds"] == 0
    assert summary["bottleneck_changed"] is False
    assert summary["chain_includes_target"] is False
    assert [row["id"] for row in result["critical_chain"]] == ["slow"]


def test_prediction_recomputes_every_job_not_just_downstream():
    result, _ = analyze(seconds=40)
    # export_bi lives on the batch-nose path; only it and notify move.
    shifted = {row["id"] for row in result["jobs"] if row["end_shift"] != 0}
    assert shifted == {"export_bi", "notify"}
    assert result["summary"]["changed_job_count"] == 2
