"""Validation and what-if analysis for a single nightly batch run.

Model
-----
Every job in the uploaded run has an observed start and end on the batch
timeline (0 = batch origin).  For each job we derive its *observed readiness*
(max end of its dependencies) and *observed wait* (start - readiness).

When one job is shortened:

* its own inputs do not change, so its start stays put and only its end moves
  earlier;
* every job's new start is ``new readiness + observed wait``;
* readiness is recomputed in dependency order, so the effect propagates the
  way the real run behaved;
* the whole batch is rescheduled and the saving is recomputed from the new
  batch completion.  If a parallel branch takes over as the bottleneck, the
  realised saving is smaller than the requested shortening.

A record that fails validation never produces a prediction.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from typing import Any

MIN_JOBS = 2
MAX_JOBS = 30
MAX_TIME = 1e12  # seconds; keeps arithmetic far away from overflow
EPS = 1e-6

DEPENDENCY_KEYS = ("depends_on", "dependencies", "deps")


@dataclass
class Job:
    index: int
    id: str
    start: float
    end: float
    dep_ids: list[str] = field(default_factory=list)
    deps: list[int] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class ParsedRun:
    jobs: list[Job]
    order: list[int]  # topological order, ties broken by input position
    by_id: dict[str, Job]
    readiness: dict[int, float]
    waits: dict[int, float]


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _clean(value: float) -> int | float:
    value = round(float(value), 6)
    if abs(value) < 1e-9:
        value = 0.0
    if value.is_integer():
        return int(value)
    return value


def _error(field_path: str, message: str) -> dict[str, str]:
    return {"field": field_path, "message": message}


def _find_cycle(n: int, deps: list[list[int]]) -> list[int] | None:
    """Return one concrete cycle as e.g. ``[a, b, a]``, where ``a`` waits on ``b``."""
    state = [0] * n  # 0 = unvisited, 1 = on stack, 2 = done
    stack: list[int] = []
    position: dict[int, int] = {}

    def dfs(node: int) -> list[int] | None:
        state[node] = 1
        position[node] = len(stack)
        stack.append(node)
        for dep in deps[node]:
            if state[dep] == 0:
                found = dfs(dep)
                if found is not None:
                    return found
            elif state[dep] == 1:
                return stack[position[dep] :] + [dep]
        stack.pop()
        position.pop(node, None)
        state[node] = 2
        return None

    for node in range(n):
        if state[node] == 0:
            found = dfs(node)
            if found is not None:
                return found
    return None


def parse_run(run: Any) -> tuple[ParsedRun | None, list[dict[str, str]]]:
    """Validate the uploaded run and derive observed readiness/waits."""
    if not isinstance(run, dict):
        return None, [_error("run", "上传的记录必须是一个 JSON 对象。")]

    raw_jobs = run.get("jobs")
    if not isinstance(raw_jobs, list):
        return None, [_error("jobs", "缺少 jobs 数组，需提供 2～30 个作业。")]

    errors: list[dict[str, str]] = []
    if not MIN_JOBS <= len(raw_jobs) <= MAX_JOBS:
        errors.append(
            _error(
                "jobs",
                f"作业数量为 {len(raw_jobs)} 个，必须在 {MIN_JOBS}～{MAX_JOBS} 个之间。",
            )
        )

    jobs: list[Job] = []
    seen: dict[str, int] = {}

    for index, raw in enumerate(raw_jobs):
        prefix = f"jobs[{index}]"
        if not isinstance(raw, dict):
            errors.append(_error(prefix, "每条作业记录必须是 JSON 对象。"))
            continue

        raw_id = raw.get("id")
        id_ok = isinstance(raw_id, str) and raw_id.strip() != ""
        if not id_ok:
            errors.append(_error(f"{prefix}.id", "作业 ID 必须是非空字符串。"))
        elif raw_id in seen:
            errors.append(
                _error(
                    f"{prefix}.id",
                    f"作业 ID “{raw_id}” 重复（已在 jobs[{seen[raw_id]}] 出现）。",
                )
            )
            id_ok = False
        else:
            seen[raw_id] = index

        dep_key = next((key for key in DEPENDENCY_KEYS if key in raw), None)
        raw_deps = raw[dep_key] if dep_key is not None else []
        deps_ok = True
        dep_ids: list[str] = []
        if not isinstance(raw_deps, list):
            errors.append(
                _error(f"{prefix}.{dep_key}", "依赖必须是作业 ID 的字符串数组。")
            )
            deps_ok = False
        else:
            for dep_pos, dep_id in enumerate(raw_deps):
                if not isinstance(dep_id, str) or dep_id.strip() == "":
                    errors.append(
                        _error(
                            f"{prefix}.{dep_key}[{dep_pos}]",
                            "依赖项必须是非空的作业 ID 字符串。",
                        )
                    )
                    deps_ok = False
                elif dep_id not in dep_ids:  # a repeated edge is harmless
                    dep_ids.append(dep_id)

        raw_start, raw_end = raw.get("start"), raw.get("end")
        start_ok = _is_number(raw_start) and abs(float(raw_start)) <= MAX_TIME
        end_ok = _is_number(raw_end) and abs(float(raw_end)) <= MAX_TIME
        if not start_ok:
            errors.append(
                _error(
                    f"{prefix}.start",
                    "开始时刻必须是相对批次起点（0 秒）的有限数值。",
                )
            )
        if not end_ok:
            errors.append(
                _error(
                    f"{prefix}.end",
                    "结束时刻必须是相对批次起点（0 秒）的有限数值。",
                )
            )
        if start_ok and float(raw_start) < -EPS:
            errors.append(
                _error(f"{prefix}.start", "开始时刻不能早于批次起点（0 秒）。")
            )
            start_ok = False
        if start_ok and end_ok and float(raw_end) <= float(raw_start) + EPS:
            errors.append(
                _error(f"{prefix}.end", "结束时刻必须晚于开始时刻（原耗时需大于 0）。")
            )
            end_ok = False

        if id_ok and deps_ok and start_ok and end_ok:
            jobs.append(
                Job(
                    index=index,
                    id=raw_id,
                    start=float(raw_start),
                    end=float(raw_end),
                    dep_ids=dep_ids,
                )
            )

    if errors:
        return None, errors

    by_id = {job.id: job for job in jobs}

    for job in jobs:
        for dep_id in job.dep_ids:
            if dep_id == job.id:
                errors.append(
                    _error(
                        f"jobs[{job.index}].depends_on",
                        f"作业 “{job.id}” 不能依赖自身。",
                    )
                )
                continue
            dep_job = by_id.get(dep_id)
            if dep_job is None:
                errors.append(
                    _error(
                        f"jobs[{job.index}].depends_on",
                        f"依赖 “{dep_id}” 不存在。",
                    )
                )
            else:
                job.deps.append(dep_job.index)
    if errors:
        return None, errors

    # Kahn's algorithm: a topological order, and cycle detection for free.
    n = len(raw_jobs)
    indegree = [0] * n
    dependents: list[list[int]] = [[] for _ in range(n)]
    for job in jobs:
        indegree[job.index] = len(job.deps)
        for dep in job.deps:
            dependents[dep].append(job.index)
    ready_nodes = [i for i in range(n) if indegree[i] == 0]
    heapq.heapify(ready_nodes)
    order: list[int] = []
    while ready_nodes:
        node = heapq.heappop(ready_nodes)
        order.append(node)
        for nxt in dependents[node]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                heapq.heappush(ready_nodes, nxt)
    if len(order) != n:
        job_by_index = {job.index: job for job in jobs}
        deps_by_node: list[list[int]] = [[] for _ in range(n)]
        for job in jobs:
            deps_by_node[job.index] = job.deps
        cycle = _find_cycle(n, deps_by_node)
        if cycle:
            names = " → ".join(job_by_index[c].id for c in cycle)
            errors.append(_error("jobs", f"依赖存在环：{names}（箭头表示“依赖于”）。"))
        else:  # defensive: only reachable with a malformed graph
            errors.append(_error("jobs", "依赖存在环，无法确定作业顺序。"))
        return None, errors

    job_by_index = {job.index: job for job in jobs}
    readiness: dict[int, float] = {}
    waits: dict[int, float] = {}
    for node in order:
        job = job_by_index[node]
        ready_at = max((job_by_index[dep].end for dep in job.deps), default=0.0)
        readiness[node] = ready_at
        wait = job.start - ready_at
        if wait < -EPS:
            blocker = max(
                (job_by_index[dep] for dep in job.deps),
                key=lambda dep: (dep.end, -dep.index),
            )
            errors.append(
                _error(
                    f"jobs[{job.index}].start",
                    f"作业 “{job.id}” 于 {_clean(job.start)} 秒开始，早于上游 "
                    f"“{blocker.id}” 的完成时刻 {_clean(blocker.end)} 秒。",
                )
            )
        waits[node] = max(0.0, wait)

    if errors:
        return None, errors

    return ParsedRun(jobs=jobs, order=order, by_id=by_id, readiness=readiness, waits=waits), []


def validate_and_plan(
    run: Any, job_id: Any, seconds: Any
) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    """Validate a run plus a proposed shortening; build a prediction only if valid.

    Returns ``(prediction, errors)``. ``prediction`` is ``None`` whenever the
    run or the shortening request is invalid.
    """
    parsed, errors = parse_run(run)
    if parsed is None:
        return None, errors

    if not isinstance(job_id, str) or job_id.strip() == "":
        return None, [_error("shorten.job_id", "必须选择要缩短的作业。")]
    target = parsed.by_id.get(job_id)
    if target is None:
        return None, [_error("shorten.job_id", f"目标作业 “{job_id}” 不存在。")]

    if not _is_number(seconds) or abs(float(seconds)) > MAX_TIME:
        return None, [_error("shorten.seconds", "缩短秒数必须是有限数值。")]
    seconds = float(seconds)
    if seconds <= EPS:
        return None, [_error("shorten.seconds", "缩短秒数必须大于 0。")]
    if seconds > target.duration + EPS:
        return None, [
            _error(
                "shorten.seconds",
                f"缩短量 {_clean(seconds)} 秒超过作业 “{target.id}” 的原耗时 "
                f"{_clean(target.duration)} 秒。",
            )
        ]

    # ---- Reschedule the whole batch ---------------------------------------
    jobs = parsed.jobs
    job_by_index = {job.index: job for job in jobs}
    new_durations = {job.index: job.duration for job in jobs}
    new_durations[target.index] = max(0.0, target.duration - seconds)

    pred_ready: dict[int, float] = {}
    pred_start: dict[int, float] = {}
    pred_end: dict[int, float] = {}
    for node in parsed.order:
        job = job_by_index[node]
        ready_at = max((pred_end[dep] for dep in job.deps), default=0.0)
        pred_ready[node] = ready_at
        pred_start[node] = ready_at + parsed.waits[node]
        pred_end[node] = pred_start[node] + new_durations[node]

    original_completion = max(job.end for job in jobs)
    predicted_completion = max(pred_end.values())
    original_bottleneck = max(job_by_index, key=lambda i: (job_by_index[i].end, -i))
    predicted_bottleneck = max(pred_end, key=lambda i: (pred_end[i], -i))
    saved = max(0.0, original_completion - predicted_completion)

    # Where does the requested saving stop propagating?  Walk downstream of the
    # target in execution order and find the first job that did not start the
    # full requested amount earlier; its limiting input is the sibling that
    # took over.  This is the precise "parallel branch took over" point.
    dependents: list[list[int]] = [[] for _ in jobs]
    for job in jobs:
        for dep in job.deps:
            dependents[dep].append(job.index)
    downstream: set[int] = set()
    frontier = [target.index]
    while frontier:
        node = frontier.pop()
        for child in dependents[node]:
            if child not in downstream:
                downstream.add(child)
                frontier.append(child)
    capped_at: dict[str, Any] | None = None
    for node in parsed.order:
        if node not in downstream:
            continue
        node_job = job_by_index[node]
        reduction = node_job.start - pred_start[node]
        if reduction < seconds - EPS:
            limiting = max(node_job.deps, key=lambda dep: (pred_end[dep], -dep))
            capped_at = {
                "job": node_job.id,
                "reduction": _clean(reduction),
                "limiting_dependency": job_by_index[limiting].id,
                "limiting_end": _clean(pred_end[limiting]),
            }
            break

    # One dependency chain that determines the predicted completion: walk back
    # from the predicted bottleneck through the binding input at every step.
    chain: list[int] = []
    cursor = predicted_bottleneck
    for _ in range(len(jobs)):
        chain.append(cursor)
        job = job_by_index[cursor]
        if not job.deps:
            break
        ready_at = pred_ready[cursor]
        binding = [dep for dep in job.deps if abs(pred_end[dep] - ready_at) <= EPS]
        cursor = min(binding) if binding else max(job.deps, key=lambda d: pred_end[d])
    chain.reverse()
    chain_set = set(chain)

    rows: list[dict[str, Any]] = []
    for job in sorted(jobs, key=lambda j: j.index):
        rows.append(
            {
                "id": job.id,
                "depends_on": [job_by_index[dep].id for dep in job.deps],
                "original_start": _clean(job.start),
                "original_end": _clean(job.end),
                "original_duration": _clean(job.duration),
                "original_ready": _clean(parsed.readiness[job.index]),
                "observed_wait": _clean(parsed.waits[job.index]),
                "predicted_start": _clean(pred_start[job.index]),
                "predicted_end": _clean(pred_end[job.index]),
                "predicted_duration": _clean(new_durations[job.index]),
                "start_shift": _clean(pred_start[job.index] - job.start),
                "end_shift": _clean(pred_end[job.index] - job.end),
                "is_target": job.index == target.index,
                "on_critical_chain": job.index in chain_set,
            }
        )
    row_by_id = {row["id"]: row for row in rows}

    changed_count = sum(
        1
        for job in jobs
        if abs(pred_start[job.index] - job.start) > EPS
        or abs(pred_end[job.index] - job.end) > EPS
    )

    prediction = {
        "ok": True,
        "summary": {
            "job_count": len(jobs),
            "target_job": target.id,
            "target_original_duration": _clean(target.duration),
            "target_new_duration": _clean(new_durations[target.index]),
            "requested_seconds": _clean(seconds),
            "original_completion": _clean(original_completion),
            "predicted_completion": _clean(predicted_completion),
            "saved_seconds": _clean(saved),
            "fully_realised": saved >= seconds - EPS,
            "bottleneck_changed": predicted_bottleneck != original_bottleneck,
            "original_bottleneck": job_by_index[original_bottleneck].id,
            "predicted_bottleneck": job_by_index[predicted_bottleneck].id,
            "changed_job_count": changed_count,
            "chain_includes_target": target.index in chain_set,
            "saving_capped_at": capped_at,
        },
        "jobs": rows,
        "critical_chain": [row_by_id[job_by_index[node].id] for node in chain],
        "model": {
            "waits_preserved": True,
            "rule": (
                "下游新开始时刻 = 新上游就绪时刻 + 本次运行实测的就绪后等待时长；"
                "目标作业开始时刻不变、仅结束时刻提前；整批重新推算完成时刻。"
            ),
        },
    }
    return prediction, []


def inspect_run(run: Any) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    """Validate a run on its own and describe its jobs (no prediction)."""
    parsed, errors = parse_run(run)
    if parsed is None:
        return None, errors
    job_by_index = {job.index: job for job in parsed.jobs}
    jobs = [
        {
            "id": job.id,
            "depends_on": [job_by_index[dep].id for dep in job.deps],
            "start": _clean(job.start),
            "end": _clean(job.end),
            "duration": _clean(job.duration),
            "observed_wait": _clean(parsed.waits[job.index]),
        }
        for job in sorted(parsed.jobs, key=lambda j: j.index)
    ]
    return (
        {
            "ok": True,
            "job_count": len(jobs),
            "original_completion": _clean(max(job.end for job in parsed.jobs)),
            "jobs": jobs,
        },
        [],
    )
