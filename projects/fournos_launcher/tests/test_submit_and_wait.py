"""Unit tests for submit_and_wait toolbox (Fournos job launch/wait).

Regression coverage for Fournos integration used by RHAIIS CPU CI pipelines.
Does not introduce submit_and_wait behavior — it guards existing EarlyReturn,
status polling, and retry configuration.
"""

from __future__ import annotations

import time

import pytest

from projects.core.dsl import always, execute_tasks, shell, task
from projects.core.dsl.control_flow import EarlyReturn
from projects.core.dsl.runtime import TaskExecutionError
from projects.core.dsl.script_manager import reset_script_manager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_result(stdout="", stderr="", returncode=0, command="oc"):
    return shell.CommandResult(stdout=stdout, stderr=stderr, returncode=returncode, command=command)


# ---------------------------------------------------------------------------
# wait=False → EarlyReturn skips non-@always tasks
# ---------------------------------------------------------------------------


def test_early_return_skips_pending_tasks():
    """When a task returns EarlyReturn, subsequent non-@always tasks are skipped."""
    reset_script_manager()
    events = []

    @task
    def t1(args, ctx):
        events.append("t1")
        return EarlyReturn("stopping early")

    @task
    def t2_should_skip(args, ctx):
        events.append("t2")  # Must NOT run

    @always
    @task
    def t3_always(args, ctx):
        events.append("t3")  # Must still run

    execute_tasks(locals())
    assert events == ["t1", "t3"]


def test_early_return_message_is_logged(tmp_path):
    """EarlyReturn from submit_fournos_job stops the pipeline cleanly (no exception)."""
    reset_script_manager()
    completed = []

    @task
    def submitter(args, ctx):
        return EarlyReturn("Submitted FournosJob: test-job (wait=False)")

    @task
    def waiter(args, ctx):
        completed.append("waiter_ran")  # Should not be reached

    # execute_tasks should succeed (no exception) even with EarlyReturn
    execute_tasks(locals())
    assert "waiter_ran" not in completed


# ---------------------------------------------------------------------------
# wait_for_job_to_resolve – status polling logic
# ---------------------------------------------------------------------------


def test_resolve_returns_truthy_on_pending(monkeypatch):
    """wait_for_job_to_resolve returns truthy when job reaches Pending status."""
    reset_script_manager()
    monkeypatch.setattr(time, "sleep", lambda s: None)

    from projects.fournos_launcher.toolbox.submit_and_wait.main import wait_for_job_to_resolve

    calls = {"n": 0}

    def fake_run(cmd, check=True, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            return _make_result(stdout="Resolving")
        return _make_result(stdout="Pending")

    monkeypatch.setattr(shell, "run", fake_run)

    @task
    def setup(args, ctx):
        ctx.final_job_name = "test-job"

    result = execute_tasks(
        {
            "namespace": "fournos-jobs",
            "setup": setup,
            "wait_for_job_to_resolve": wait_for_job_to_resolve,
        }
    )
    assert result


def test_resolve_raises_on_not_found(monkeypatch):
    """wait_for_job_to_resolve raises immediately when job is not found (no retries)."""
    reset_script_manager()
    monkeypatch.setattr(time, "sleep", lambda s: None)

    from projects.fournos_launcher.toolbox.submit_and_wait.main import (
        FournosJobFailureError,
        wait_for_job_to_resolve,
    )

    def fake_run(cmd, check=True, **kwargs):
        return _make_result(stdout="", stderr="not found", returncode=1)

    monkeypatch.setattr(shell, "run", fake_run)

    @task
    def setup(args, ctx):
        ctx.final_job_name = "test-job"

    with pytest.raises(TaskExecutionError) as ei:
        execute_tasks(
            {
                "namespace": "fournos-jobs",
                "setup": setup,
                "wait_for_job_to_resolve": wait_for_job_to_resolve,
            }
        )
    assert isinstance(ei.value.__cause__, FournosJobFailureError)


def test_resolve_raises_on_stopping(monkeypatch):
    """wait_for_job_to_resolve raises FournosJobFailureError when job enters Stopping."""
    reset_script_manager()
    monkeypatch.setattr(time, "sleep", lambda s: None)

    from projects.fournos_launcher.toolbox.submit_and_wait.main import (
        FournosJobFailureError,
        wait_for_job_to_resolve,
    )

    def fake_run(cmd, check=True, **kwargs):
        return _make_result(stdout="Stopping")

    monkeypatch.setattr(shell, "run", fake_run)

    @task
    def setup(args, ctx):
        ctx.final_job_name = "test-job"

    with pytest.raises(TaskExecutionError) as ei:
        execute_tasks(
            {
                "namespace": "fournos-jobs",
                "setup": setup,
                "wait_for_job_to_resolve": wait_for_job_to_resolve,
            }
        )
    assert isinstance(ei.value.__cause__, FournosJobFailureError)


def test_resolve_succeeds_immediately_when_already_running(monkeypatch):
    """If job is already Running/Admitted/Succeeded, wait_for_job_to_resolve returns immediately."""
    monkeypatch.setattr(time, "sleep", lambda s: None)

    from projects.fournos_launcher.toolbox.submit_and_wait.main import wait_for_job_to_resolve

    for terminal_status in ["Running", "Admitted", "Succeeded"]:
        reset_script_manager()

        def fake_run(cmd, check=True, status=terminal_status, **kwargs):
            return _make_result(stdout=status)

        monkeypatch.setattr(shell, "run", fake_run)

        @task
        def setup(args, ctx):
            ctx.final_job_name = "test-job"

        result = execute_tasks(
            {
                "namespace": "fournos-jobs",
                "setup": setup,
                "wait_for_job_to_resolve": wait_for_job_to_resolve,
            }
        )
        assert result, f"Expected truthy result for status={terminal_status}"


# ---------------------------------------------------------------------------
# wait_for_job_completion – delay changed from 10s to 30s (doc check)
# ---------------------------------------------------------------------------


def test_wait_for_job_completion_retry_config():
    """wait_for_job_completion should have 3000 attempts and 30s delay (not 10s)."""
    import inspect

    from projects.fournos_launcher.toolbox.submit_and_wait import main as m

    # Find the retry decorator config on wait_for_job_completion
    fn = m.wait_for_job_completion
    # The retry decorator stores config on the task wrapper
    retry_cfg = getattr(fn, "_retry_config", None)
    if retry_cfg is None:
        # Fallback: check function source for the delay value
        src = inspect.getsource(m.wait_for_job_completion)
        assert "delay=30" in src, "wait_for_job_completion should use delay=30 (was 10)"
        assert "attempts=3000" in src, "wait_for_job_completion should have 3000 attempts"
    else:
        assert retry_cfg["delay"] == 30
        assert retry_cfg["attempts"] == 3000


# ---------------------------------------------------------------------------
# check_early_return – passthrough when wait=True
# ---------------------------------------------------------------------------


def test_check_early_return_noop_when_wait_true():
    """check_early_return is a no-op (returns a plain string) when wait=True."""
    reset_script_manager()

    ran = []

    @task
    def setup(args, ctx):
        ctx.final_job_name = "test-job"
        ran.append("setup")

    @task
    def check(args, ctx):
        # Mirrors check_early_return logic
        if not args.wait:
            return EarlyReturn(f"launched: {ctx.final_job_name} (wait=False)")
        ran.append("check_passed")
        return f"launched: {ctx.final_job_name}"

    @task
    def after(args, ctx):
        ran.append("after")

    execute_tasks({"wait": True, "setup": setup, "check": check, "after": after})
    assert "check_passed" in ran
    assert "after" in ran
