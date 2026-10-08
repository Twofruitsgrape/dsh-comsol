"""Unit tests for background solve jobs (no COMSOL needed)."""

from __future__ import annotations

import time

import pytest

from comsol_mcp.errors import ComsolMcpError
from comsol_mcp.runtime import Runtime


def make_runtime(monkeypatch, work=None) -> Runtime:
    runtime = Runtime()  # Session setup is light and touches no COMSOL
    monkeypatch.setattr(runtime, "_solve_work", work or (lambda study: {"ok": study}))
    return runtime


def wait_for_state(runtime: Runtime, state: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    status = runtime.solve_status()
    while time.monotonic() < deadline:
        status = runtime.solve_status()
        if status["state"] == state:
            break
        time.sleep(0.05)
    return status


def test_status_is_idle_before_any_job():
    runtime = Runtime()
    status = runtime.solve_status()
    assert status["state"] == "idle"
    assert "comsol_solve_start" in status["note"]


def test_job_runs_to_completion(monkeypatch):
    def slow_work(study):
        time.sleep(0.2)
        return {"ok": study}

    runtime = make_runtime(monkeypatch, slow_work)
    job = runtime.solve_job_start("std1")
    assert job["job"].startswith("solve-")
    assert job["study"] == "std1"

    status = wait_for_state(runtime, "done")
    assert status["state"] == "done"
    assert status["result"] == {"ok": "std1"}
    assert status["elapsed_seconds"] >= 0.1


def test_double_start_is_rejected(monkeypatch):
    def slow_work(study):
        time.sleep(0.5)
        return {}

    runtime = make_runtime(monkeypatch, slow_work)
    runtime.solve_job_start("std1")
    with pytest.raises(ComsolMcpError, match="already running"):
        runtime.solve_job_start("std2")
    wait_for_state(runtime, "done")  # leave the runtime clean


def test_failed_job_reports_error_and_hint(monkeypatch):
    def boom(study):
        raise ComsolMcpError("study exploded", hint="bang")

    runtime = make_runtime(monkeypatch, boom)
    runtime.solve_job_start(None)
    status = wait_for_state(runtime, "failed")
    assert status["state"] == "failed"
    assert "study exploded" in status["error"]
    assert status["hint"] == "bang"


def test_a_finished_job_can_be_replaced(monkeypatch):
    runtime = make_runtime(monkeypatch)
    runtime.solve_job_start("a")
    wait_for_state(runtime, "done")
    job = runtime.solve_job_start("b")  # must not raise
    wait_for_state(runtime, "done")
    assert job["study"] == "b"
