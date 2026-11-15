"""FastAPI application for the nightly batch acceleration planner."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .planner import inspect_run, validate_and_plan

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(
    title="夜间入仓作业加速测算",
    description="上传一次运行的作业时间线，测算缩短单个作业后整批数据能提前多久交付。",
    version="1.0.0",
)


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    # Keep the API JSON-shaped even when something unexpected happens, so the
    # page can always show an error instead of choking on an HTML error page.
    return JSONResponse(
        status_code=500,
        content={
            "ok": False,
            "errors": [{"field": "server", "message": "测算失败，请检查记录后重试。"}],
        },
    )


async def _read_json_object(request: Request) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "errors": [{"field": "body", "message": "请求体不是合法的 JSON。"}],
            },
        )
    if not isinstance(payload, dict):
        return None, JSONResponse(
            status_code=422,
            content={
                "ok": False,
                "errors": [{"field": "body", "message": "请求体必须是一个 JSON 对象。"}],
            },
        )
    return payload, None


@app.post("/api/inspect")
async def inspect(request: Request) -> JSONResponse:
    """Validate the uploaded run and return its jobs for the picker.

    The record is validated exactly as it will be for a prediction, so an
    invalid upload is rejected before the user can request a forecast.
    """
    payload, failure = await _read_json_object(request)
    if failure is not None:
        return failure
    result, errors = inspect_run(payload.get("run"))
    if result is None:
        return JSONResponse(
            status_code=422, content={"ok": False, "errors": errors}
        )
    return JSONResponse(status_code=200, content=result)


@app.post("/api/analyze")
async def analyze(request: Request) -> JSONResponse:
    """Predict the new batch completion for shortening one job.

    An invalid run or shortening request never produces a prediction: the
    response carries field-level errors and no forecast.
    """
    payload, failure = await _read_json_object(request)
    if failure is not None:
        return failure
    prediction, errors = validate_and_plan(
        payload.get("run"), payload.get("job_id"), payload.get("seconds")
    )
    if prediction is None:
        return JSONResponse(
            status_code=422, content={"ok": False, "errors": errors}
        )
    return JSONResponse(status_code=200, content=prediction)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")