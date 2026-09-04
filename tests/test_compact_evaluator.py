import json
from pathlib import Path

import pytest

from scripts import evaluate_compact_prompt as evaluator


def entry(case="case-a", run=1, **overrides):
    value = {
        "case": case,
        "run": run,
        "seed_sha256": evaluator.cell_seed(case, run),
        "passed": False,
        "generation_completed": False,
        "reason": "transient_overloaded",
        "status": "incomplete",
        "attempts": 1,
    }
    value.update(overrides)
    return value


def test_corrupt_checkpoint_and_interrupted_atomic_write_fail_closed(
    tmp_path, monkeypatch
):
    path = tmp_path / "evidence.json"
    path.write_text('{"schema": 2, "entries": [')

    with pytest.raises(RuntimeError, match="corrupt"):
        evaluator.load_evidence(path)

    original = '{"schema": 2, "entries": []}\n'
    path.write_text(original)

    def fail_replace(_source, _destination):
        raise OSError("simulated interrupted replace")

    monkeypatch.setattr(evaluator.os, "replace", fail_replace)
    with pytest.raises(OSError, match="interrupted"):
        evaluator._atomic_write(path, '{"new": true}\n')

    assert path.read_text() == original
    assert list(tmp_path.glob(".evidence.json.*")) == []


def test_duplicate_cells_are_rejected_and_completed_cells_are_immutable(tmp_path):
    path = tmp_path / "evidence.json"
    duplicate = entry()
    path.write_text(json.dumps({
        "schema": 2,
        "entries": [duplicate, duplicate],
    }))

    with pytest.raises(RuntimeError, match="duplicate"):
        evaluator.load_evidence(path)

    completed = entry(
        passed=True,
        generation_completed=True,
        reason=None,
        status="passed",
    )
    with pytest.raises(RuntimeError, match="immutable"):
        evaluator.merge_entry(
            [completed],
            {**completed, "summary_sha256": "different"},
        )


@pytest.mark.asyncio
async def test_overload_retry_keeps_one_cell_identity_then_succeeds():
    calls = []
    checkpoints = []
    sleeps = []

    async def fake(case, run):
        calls.append((case["id"], run, evaluator.cell_seed(case["id"], run)))
        if len(calls) == 1:
            return entry(case["id"], run), ["synthetic-secret"]
        return entry(
            case["id"],
            run,
            passed=True,
            generation_completed=True,
            reason=None,
        ), ["synthetic-secret"]

    async def fake_sleep(delay):
        sleeps.append(delay)

    result, _secrets = await evaluator.run_cell(
        {"id": "case-a"},
        2,
        evaluator=fake,
        retry_base_seconds=1,
        sleep_fn=fake_sleep,
        checkpoint=lambda candidate, _values: checkpoints.append(candidate.copy()),
    )

    assert result["status"] == "passed"
    assert result["attempts"] == 2
    assert calls == [calls[0], calls[0]]
    assert [item["status"] for item in checkpoints] == ["incomplete", "passed"]
    assert len(sleeps) == 1
    assert 1 <= sleeps[0] <= 1.2


@pytest.mark.asyncio
async def test_overload_retry_exhaustion_stays_incomplete_not_failed():
    calls = 0
    checkpoints = []
    sleeps = []

    async def always_overloaded(case, run):
        nonlocal calls
        calls += 1
        return entry(case["id"], run), []

    async def fake_sleep(delay):
        sleeps.append(delay)

    result, _secrets = await evaluator.run_cell(
        {"id": "case-a"},
        1,
        evaluator=always_overloaded,
        previous_attempts=4,
        max_attempts=3,
        retry_base_seconds=2,
        sleep_fn=fake_sleep,
        checkpoint=lambda candidate, _values: checkpoints.append(candidate.copy()),
    )

    assert calls == 3
    assert result["status"] == "incomplete"
    assert result["attempts"] == 7
    assert len(checkpoints) == 3
    assert len(sleeps) == 2
    assert 2 <= sleeps[0] <= 2.4
    assert 4 <= sleeps[1] <= 4.8


@pytest.mark.asyncio
async def test_non_overload_failure_is_not_retried():
    calls = 0

    async def generic_failure(case, run):
        nonlocal calls
        calls += 1
        return entry(
            case["id"],
            run,
            reason="summary_error",
        ), []

    result, _secrets = await evaluator.run_cell(
        {"id": "case-a"},
        1,
        evaluator=generic_failure,
        retry_base_seconds=0,
    )

    assert calls == 1
    assert result["status"] == "incomplete"
