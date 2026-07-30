"""Tests for the seeded offline failure-recovery benchmark."""

from pathlib import Path

from experiments.failure_recovery.benchmark import (
    DEFAULT_OUTPUT_DIR,
    SCENARIOS,
    STRATEGIES,
    run_benchmark,
    summarize,
)


def test_default_output_dir_is_ignored_artifact_path():
    assert DEFAULT_OUTPUT_DIR == Path(".artifacts/failure_recovery/results")


def test_benchmark_is_reproducible_and_has_complete_matrix():
    first = run_benchmark(seeds=[11], trials_per_seed=2)
    second = run_benchmark(seeds=[11], trials_per_seed=2)

    assert first == second
    assert len(first) == len(SCENARIOS) * len(STRATEGIES) * 2
    assert {
        (row.scenario, row.strategy)
        for row in first
    } == {
        (scenario, strategy.strategy_id)
        for scenario in SCENARIOS
        for strategy in STRATEGIES
    }


def test_benchmark_preserves_governance_invariants():
    rows = run_benchmark(seeds=[11], trials_per_seed=3)

    assert sum(row.duplicate_side_effects for row in rows) == 0
    assert sum(row.policy_violations for row in rows) == 0
    side_effect_rows = [
        row for row in rows if row.scenario == "side_effect_uncertain"
    ]
    assert all(row.logical_calls == 1 for row in side_effect_rows)
    assert all(row.needs_reconciliation == 1 for row in side_effect_rows)


def test_b2_only_improves_failures_that_are_safe_to_redispatch():
    rows = run_benchmark(seeds=[11], trials_per_seed=4)
    summary = summarize(rows)

    persistent = {
        row["strategy"]: row
        for row in summary
        if row["scenario"] == "persistent_primary_failure"
    }
    assert (
        persistent["B2"]["completion_rate"]
        >= persistent["B1"]["completion_rate"]
    )

    non_retryable = [
        row
        for row in rows
        if row.scenario == "non_retryable_business_failure"
    ]
    assert all(row.logical_calls == 1 for row in non_retryable)
    assert all(row.redispatch_count == 0 for row in non_retryable)
