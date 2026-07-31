"""Seeded offline benchmark for bounded read-only failure recovery.

Run from the repository root:

    uv run --frozen --offline python -m experiments.failure_recovery.benchmark

The benchmark invokes the real ``TaskScheduler`` with deterministic stub
executors and routers.  It never calls an LLM, a remote service, or a real
side-effecting tool.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import math
import random
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

from src.contracts.agent_contract import AgentContract, DataContractRef
from src.interface.task_graph import TaskGraph, TaskSpec, TaskStep
from src.manager.executor.base import ExecuteResult, ExecutionStatus
from src.orchestration.completion import ReceiptStore
from src.orchestration.providers import RoutingResult
from src.orchestration.scheduler import TaskScheduler


PRIMARY_AGENT = "PrimaryAgent"
BACKUP_AGENT = "BackupAgent"
DEFAULT_OUTPUT_DIR = Path(".artifacts/failure_recovery/results")
SCENARIOS = (
    "transient_timeout",
    "persistent_primary_failure",
    "non_retryable_business_failure",
    "missing_trusted_backup",
    "side_effect_uncertain",
)


@dataclass(frozen=True)
class Strategy:
    strategy_id: str
    label: str
    retry_budget: int
    redispatch_enabled: bool


STRATEGIES = (
    Strategy("B0", "无自动恢复", 0, False),
    Strategy("B1", "同 Agent 有界重试", 1, False),
    Strategy("B2", "同 Agent 重试后等价 Agent 改派", 1, True),
)


@dataclass(frozen=True)
class TrialProfile:
    scenario: str
    fault_triggered: bool


@dataclass
class TrialRow:
    scenario: str
    strategy: str
    seed: int
    trial: int
    fault_triggered: int
    initial_failure: int
    completed: int
    recovered: int
    logical_calls: int
    retry_count: int
    redispatch_count: int
    virtual_latency_ms: float
    duplicate_side_effects: int
    policy_violations: int
    needs_reconciliation: int
    terminal_status: str
    step_status: str
    failure_code: str
    recovery_path: str


def _contract() -> AgentContract:
    return AgentContract(
        produces=[
            DataContractRef(
                name="policy.info",
                schema_ref="policy.info@v1",
            )
        ]
    )


def _success_envelope(agent: str) -> dict[str, Any]:
    return {
        "contract_version": "1.0",
        "status": "success",
        "outputs": {
            "policy.info": {
                "query": "年假",
                "answer": "满一年五天",
                "knowledge_items_count": 1,
                "policy_scope": "company",
            }
        },
        "error": None,
        "metadata": {
            "producer_agent": agent,
            "schema_version": "1.0",
        },
    }


def _error_envelope(agent: str, *, retryable: bool) -> dict[str, Any]:
    return {
        "contract_version": "1.0",
        "status": "error",
        "outputs": {},
        "error": {
            "code": (
                "UPSTREAM_TIMEOUT" if retryable else "INVALID_BUSINESS_REQUEST"
            ),
            "message": "seeded failure injection",
            "retryable": retryable,
            "details": {},
        },
        "metadata": {
            "producer_agent": agent,
            "schema_version": "1.0",
        },
    }


def _ok(payload: dict[str, Any]) -> ExecuteResult:
    return ExecuteResult(status=ExecutionStatus.SUCCESS, result=payload)


def make_trial_profile(
    scenario: str,
    *,
    seed: int,
    trial: int,
) -> TrialProfile:
    """Create the latent fault profile shared by B0/B1/B2 for one trial."""
    scenario_index = SCENARIOS.index(scenario)
    rng = random.Random(seed * 100_003 + trial * 101 + scenario_index)
    if scenario == "transient_timeout":
        triggered = rng.random() < 0.65
    elif scenario == "persistent_primary_failure":
        triggered = rng.random() < 0.70
    else:
        triggered = True
    return TrialProfile(scenario=scenario, fault_triggered=triggered)


class BenchmarkRoutingProvider:
    """Route to the primary first and the backup during recovery."""

    def __init__(self) -> None:
        self.calls = 0
        self.virtual_latency_ms = 0.0

    async def decide(
        self,
        step,
        *,
        authorized_agent_ids,
        **kwargs,
    ) -> RoutingResult:
        self.calls += 1
        self.virtual_latency_ms += 20.0
        if PRIMARY_AGENT in set(authorized_agent_ids or set()):
            selected = PRIMARY_AGENT
        elif BACKUP_AGENT in set(authorized_agent_ids or set()):
            selected = BACKUP_AGENT
        else:
            return RoutingResult(
                selected_agent=None,
                decision="NO_CAPABLE_AGENT",
            )
        return RoutingResult(selected_agent=selected, decision="DISPATCH")


class FaultInjectingExecutor:
    """Stateful executor implementing one seeded failure profile."""

    def __init__(self, profile: TrialProfile) -> None:
        self.profile = profile
        self.calls: list[str] = []
        self.first_call_failed = False
        self.virtual_latency_ms = 0.0

    async def __call__(self, *, selected_agent: str, **kwargs) -> ExecuteResult:
        self.calls.append(selected_agent)
        self.virtual_latency_ms += (
            120.0 if selected_agent == BACKUP_AGENT else 100.0
        )
        agent_attempt = self.calls.count(selected_agent)
        failed = False

        if self.profile.scenario == "transient_timeout":
            failed = (
                selected_agent == PRIMARY_AGENT
                and self.profile.fault_triggered
                and agent_attempt == 1
            )
            result = (
                _ok(_error_envelope(selected_agent, retryable=True))
                if failed
                else _ok(_success_envelope(selected_agent))
            )
        elif self.profile.scenario == "persistent_primary_failure":
            failed = (
                selected_agent == PRIMARY_AGENT
                and self.profile.fault_triggered
            )
            result = (
                _ok(_error_envelope(selected_agent, retryable=True))
                if failed
                else _ok(_success_envelope(selected_agent))
            )
        elif self.profile.scenario == "non_retryable_business_failure":
            failed = selected_agent == PRIMARY_AGENT
            result = (
                _ok(_error_envelope(selected_agent, retryable=False))
                if failed
                else _ok(_success_envelope(selected_agent))
            )
        elif self.profile.scenario == "missing_trusted_backup":
            failed = selected_agent == PRIMARY_AGENT
            result = (
                _ok(_error_envelope(selected_agent, retryable=True))
                if failed
                else _ok(_success_envelope(selected_agent))
            )
        elif self.profile.scenario == "side_effect_uncertain":
            failed = True
            result = ExecuteResult(
                status=ExecutionStatus.FAILED,
                error="seeded uncertain side effect",
            )
        else:  # pragma: no cover - protected by CLI choices/tests
            raise ValueError(f"unknown scenario: {self.profile.scenario}")

        if len(self.calls) == 1:
            self.first_call_failed = failed
        return result


def _make_graph(
    strategy: Strategy,
    profile: TrialProfile,
) -> TaskGraph:
    operation_mode = (
        "write" if profile.scenario == "side_effect_uncertain" else "read"
    )
    step = TaskStep(
        step_id="lookup",
        operation_mode=operation_mode,
        retry=strategy.retry_budget,
        agent_name=PRIMARY_AGENT,
        preferred_resource_id=PRIMARY_AGENT,
        expected_outputs=["policy.info"],
        agent_contract=_contract(),
    )
    return TaskGraph(
        spec=TaskSpec(task_id="failure-recovery-benchmark"),
        steps=[step],
    )


def _make_context(profile: TrialProfile) -> dict[str, Any]:
    agents = [
        SimpleNamespace(
            agent_name=PRIMARY_AGENT,
            agent_contract=_contract(),
        )
    ]
    if profile.scenario != "missing_trusted_backup":
        agents.append(
            SimpleNamespace(
                agent_name=BACKUP_AGENT,
                agent_contract=_contract(),
            )
        )
    return {
        "task_id": "failure-recovery-benchmark",
        "workflow_id": "wf-failure-recovery-benchmark",
        "user_query": "查询年假政策",
        "authorized_agent_ids": {PRIMARY_AGENT, BACKUP_AGENT},
        "agents": agents,
    }


async def run_trial(
    *,
    strategy: Strategy,
    profile: TrialProfile,
    seed: int,
    trial: int,
) -> TrialRow:
    router = BenchmarkRoutingProvider()
    executor = FaultInjectingExecutor(profile)
    scheduler = TaskScheduler(
        execute_step=executor,
        routing_provider=router,
        receipt_store=(
            ReceiptStore()
            if profile.scenario == "side_effect_uncertain"
            else None
        ),
        redispatch_enabled=strategy.redispatch_enabled,
        retry_delay_seconds=0,
    )
    workflow = await scheduler.run(
        _make_graph(strategy, profile),
        context=_make_context(profile),
    )
    result = workflow["lookup"]
    metrics = dict(result.metrics or {})
    completed = int(result.is_success)
    initial_failure = int(executor.first_call_failed)
    backup_calls = executor.calls.count(BACKUP_AGENT)
    write_calls = (
        len(executor.calls)
        if profile.scenario == "side_effect_uncertain"
        else 0
    )
    policy_violations = int(
        profile.scenario == "missing_trusted_backup" and backup_calls > 0
    )
    failure_code = (
        str(result.failure.code)
        if getattr(result, "failure", None) is not None
        else ""
    )
    terminal_status = getattr(workflow.terminal_status, "value", None)
    if terminal_status is None:
        terminal_status = str(workflow.terminal_status)
    return TrialRow(
        scenario=profile.scenario,
        strategy=strategy.strategy_id,
        seed=seed,
        trial=trial,
        fault_triggered=int(profile.fault_triggered),
        initial_failure=initial_failure,
        completed=completed,
        recovered=int(bool(initial_failure and completed)),
        logical_calls=len(executor.calls),
        retry_count=int(metrics.get("retry_count", 0)),
        redispatch_count=int(metrics.get("redispatch_count", 0)),
        virtual_latency_ms=(
            executor.virtual_latency_ms + router.virtual_latency_ms
        ),
        duplicate_side_effects=max(0, write_calls - 1),
        policy_violations=policy_violations,
        needs_reconciliation=int(
            "lookup" in set(workflow.needs_reconciliation)
        ),
        terminal_status=str(terminal_status),
        step_status=str(result.status),
        failure_code=failure_code,
        recovery_path=">".join(metrics.get("recovery_path", [])),
    )


def run_benchmark(
    *,
    seeds: Iterable[int],
    trials_per_seed: int,
) -> list[TrialRow]:
    rows: list[TrialRow] = []
    for scenario in SCENARIOS:
        for seed in seeds:
            for trial in range(trials_per_seed):
                profile = make_trial_profile(
                    scenario,
                    seed=seed,
                    trial=trial,
                )
                for strategy in STRATEGIES:
                    rows.append(
                        asyncio.run(
                            run_trial(
                                strategy=strategy,
                                profile=profile,
                                seed=seed,
                                trial=trial,
                            )
                        )
                    )
    return rows


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def _wilson_interval(successes: int, total: int) -> tuple[float, float]:
    """Return a two-sided 95% Wilson score interval for a binomial rate."""
    if total <= 0:
        return (0.0, 0.0)
    z = 1.959963984540054
    observed = successes / total
    denominator = 1 + z**2 / total
    centre = observed + z**2 / (2 * total)
    margin = z * math.sqrt(
        observed * (1 - observed) / total + z**2 / (4 * total**2)
    )
    return (
        max(0.0, (centre - margin) / denominator),
        min(1.0, (centre + margin) / denominator),
    )


def summarize(rows: list[TrialRow]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for scenario in (*SCENARIOS, "ALL"):
        for strategy in STRATEGIES:
            group = [
                row
                for row in rows
                if row.strategy == strategy.strategy_id
                and (scenario == "ALL" or row.scenario == scenario)
            ]
            initial_failures = sum(row.initial_failure for row in group)
            completed = sum(row.completed for row in group)
            recovered = sum(row.recovered for row in group)
            completion_ci = _wilson_interval(completed, len(group))
            recovery_ci = _wilson_interval(recovered, initial_failures)
            summaries.append(
                {
                    "scenario": scenario,
                    "strategy": strategy.strategy_id,
                    "strategy_label": strategy.label,
                    "trials": len(group),
                    "completed": completed,
                    "completion_rate": (
                        completed / len(group)
                        if group
                        else 0.0
                    ),
                    "completion_ci95_low": completion_ci[0],
                    "completion_ci95_high": completion_ci[1],
                    "initial_failures": initial_failures,
                    "recovered": recovered,
                    "recovery_success_rate": (
                        recovered / initial_failures
                        if initial_failures
                        else 0.0
                    ),
                    "recovery_ci95_low": recovery_ci[0],
                    "recovery_ci95_high": recovery_ci[1],
                    "avg_logical_calls": (
                        sum(row.logical_calls for row in group) / len(group)
                        if group
                        else 0.0
                    ),
                    "p95_virtual_latency_ms": _p95(
                        [row.virtual_latency_ms for row in group]
                    ),
                    "redispatch_rate": (
                        sum(int(row.redispatch_count > 0) for row in group)
                        / len(group)
                        if group
                        else 0.0
                    ),
                    "duplicate_side_effects": sum(
                        row.duplicate_side_effects for row in group
                    ),
                    "policy_violations": sum(
                        row.policy_violations for row in group
                    ),
                    "needs_reconciliation_rate": (
                        sum(row.needs_reconciliation for row in group)
                        / len(group)
                        if group
                        else 0.0
                    ),
                }
            )
    return summaries


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _write_markdown(
    path: Path,
    summaries: list[dict[str, Any]],
    *,
    seeds: list[int],
    trials_per_seed: int,
) -> None:
    overall = [row for row in summaries if row["scenario"] == "ALL"]
    lines = [
        "# 失败恢复 B0-B2 离线实验摘要",
        "",
        f"- 固定种子：`{', '.join(map(str, seeds))}`",
        f"- 每个种子、每个场景试验次数：`{trials_per_seed}`",
        f"- 每个场景、每个策略总样本：`{len(seeds) * trials_per_seed}`",
        "- 延迟：离线虚拟成本，不是线上墙钟性能。",
        "",
        "| 策略 | 总体闭环率 | 首次失败后的恢复成功率 | 平均逻辑调用 | P95虚拟延迟(ms) | 重复副作用 | 治理违规 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in overall:
        lines.append(
            "| {strategy} {label} | {completion:.1%} | {recovery:.1%} | "
            "{calls:.2f} | {latency:.0f} | {duplicates} | {violations} |".format(
                strategy=row["strategy"],
                label=row["strategy_label"],
                completion=row["completion_rate"],
                recovery=row["recovery_success_rate"],
                calls=row["avg_logical_calls"],
                latency=row["p95_virtual_latency_ms"],
                duplicates=row["duplicate_side_effects"],
                violations=row["policy_violations"],
            )
        )
    lines.extend(
        [
            "",
            "完整分场景结果见 `summary.csv`，逐次原始记录见 `trials.csv`。",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def write_results(
    *,
    output_dir: Path,
    rows: list[TrialRow],
    seeds: list[int],
    trials_per_seed: int,
) -> list[dict[str, Any]]:
    summaries = summarize(rows)
    output_dir.mkdir(parents=True, exist_ok=True)
    trial_dicts = [asdict(row) for row in rows]
    _write_csv(output_dir / "trials.csv", trial_dicts)
    _write_csv(output_dir / "summary.csv", summaries)
    (output_dir / "summary.json").write_text(
        json.dumps(summaries, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _write_markdown(
        output_dir / "summary.md",
        summaries,
        seeds=seeds,
        trials_per_seed=trials_per_seed,
    )
    return summaries


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the offline B0-B2 failure-recovery benchmark."
    )
    parser.add_argument(
        "--seeds",
        default="11,29,47,71,97",
        help="comma-separated deterministic seeds",
    )
    parser.add_argument(
        "--trials-per-seed",
        type=int,
        default=20,
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.getLogger("src.orchestration.scheduler").setLevel(logging.ERROR)
    seeds = [int(item.strip()) for item in args.seeds.split(",") if item.strip()]
    if not seeds or args.trials_per_seed <= 0:
        raise SystemExit("seeds and trials-per-seed must be positive")
    rows = run_benchmark(
        seeds=seeds,
        trials_per_seed=args.trials_per_seed,
    )
    summaries = write_results(
        output_dir=args.output_dir,
        rows=rows,
        seeds=seeds,
        trials_per_seed=args.trials_per_seed,
    )
    overall = [row for row in summaries if row["scenario"] == "ALL"]
    print(json.dumps(overall, ensure_ascii=False, indent=2))
    print(f"results: {args.output_dir.resolve()}")


if __name__ == "__main__":
    main()
