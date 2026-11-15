"""Smoke test against a running deployment (Docker Compose or local uvicorn).

Usage::

    BASE_URL=http://localhost:8000 python scripts/smoke.py
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000").rstrip("/")
SAMPLE = Path(__file__).resolve().parent.parent / "samples" / "nightly-run.json"

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(("PASS  " if condition else "FAIL  ") + name + (("  →  " + detail) if not condition and detail else ""))
    if not condition:
        failures.append(name)


def post(path: str, body: object) -> tuple[int, object]:
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def main() -> int:
    with urllib.request.urlopen(BASE + "/api/health", timeout=10) as response:
        check("health endpoint", json.loads(response.read()) == {"status": "ok"})

    with urllib.request.urlopen(BASE + "/", timeout=10) as response:
        html = response.read().decode()
    check("page served", "夜间入仓作业加速测算" in html and "/static/app.js" in html)

    run = json.loads(SAMPLE.read_text(encoding="utf-8"))

    status, inspected = post("/api/inspect", {"run": run})
    check("sample upload validates", status == 200 and inspected["ok"] is True, str(status))
    check("sample has 15 jobs", inspected.get("job_count") == 15, str(inspected.get("job_count")))
    check("sample completes at 2800s", inspected.get("original_completion") == 2800)

    status, prediction = post("/api/analyze", {"run": run, "job_id": "export_bi", "seconds": 150})
    summary = prediction.get("summary", {})
    check("analyze succeeds", status == 200 and prediction.get("ok") is True, str(status))
    check("original completion 2800", summary.get("original_completion") == 2800)
    check("predicted completion 2650", summary.get("predicted_completion") == 2650)
    check("saved 150s", summary.get("saved_seconds") == 150)
    check("chain ends at bottleneck", prediction["critical_chain"][-1]["id"] == summary.get("predicted_bottleneck"))
    check("chain is a real dependency path", all(
        upstream["id"] in downstream["depends_on"]
        for upstream, downstream in zip(prediction["critical_chain"], prediction["critical_chain"][1:])
    ))

    status, capped = post("/api/analyze", {"run": run, "job_id": "agg_daily", "seconds": 300})
    capped_summary = capped.get("summary", {})
    check("parallel branch caps the saving", status == 200 and capped_summary.get("saved_seconds") == 100)
    check("shortfall is reported", capped_summary.get("fully_realised") is False)
    check(
        "takeover point named",
        (capped_summary.get("saving_capped_at") or {}).get("limiting_dependency") == "agg_region",
    )

    bad = json.loads(SAMPLE.read_text(encoding="utf-8"))
    bad["jobs"][3]["start"] = 500
    status, rejected = post("/api/analyze", {"run": bad, "job_id": "export_bi", "seconds": 10})
    check("invalid timing rejected", status == 422 and rejected["ok"] is False)
    check("no prediction returned when invalid", "summary" not in rejected and "jobs" not in rejected)

    cycled = json.loads(SAMPLE.read_text(encoding="utf-8"))
    cycled["jobs"][0]["depends_on"] = ["notify"]
    status, rejected = post("/api/inspect", {"run": cycled})
    check("cycle rejected", status == 422 and any("环" in e["message"] for e in rejected["errors"]))

    status, rejected = post("/api/analyze", {"run": run, "job_id": "notify", "seconds": 61})
    check("oversized shortening rejected", status == 422 and rejected["errors"][0]["field"] == "shorten.seconds")

    print()
    if failures:
        print(f"{len(failures)} SMOKE CHECK(S) FAILED")
        return 1
    print("ALL SMOKE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
