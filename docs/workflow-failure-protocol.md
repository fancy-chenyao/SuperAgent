# Structured Workflow Failure Protocol

The Scheduler publishes a stable, payload-free failure descriptor for every
failed or blocked step. The descriptor is shared by checkpoints, task logs,
SSE events, and the Web UI.

## Descriptor

```json
{
  "code": "SCHEMA_VALIDATION_FAILED",
  "category": "schema",
  "message": "The Agent output failed Schema validation.",
  "retryable": false,
  "action": "Check the output fields and Contract Schema version.",
  "step_id": "hr_step",
  "agent_id": "RemoteHRAssistantAgent",
  "parameter_name": null,
  "source_step": null,
  "source_output": null,
  "blocked_by": [],
  "details_safe": {
    "schema_ref": "employee.info@v1"
  }
}
```

`code` is the stable machine contract. `category`, `message`, and `action` are
platform-owned presentation hints. A remote Agent cannot choose the platform
failure code or publish arbitrary diagnostic fields. The only remote signal
that survives is the retryability verdict preserved by the result adapter
(`result_retryable`): when it is `true`, the descriptor's `retryable` flag is
upgraded, while message and action still come from the platform catalog.

`details_safe` is allow-listed. Tracebacks, raw remote responses, business
payloads, validator error trees, credentials, and policy internals never cross
the SSE boundary.

## Event compatibility

`step_result` includes both:

- `failure`: the authoritative structured descriptor;
- `error`: a safe legacy message retained for older consumers.

`end_of_workflow` includes:

- `failures`: all failed and blocked step descriptors;
- `failed_steps`: steps that executed and failed;
- `blocked_steps`: steps that did not execute because an upstream dependency
  failed.

Blocked steps are explicit `SKIPPED` results with
`code=UPSTREAM_STEP_FAILED` for failed dependencies or
`code=CLARIFICATION_BLOCKED` for a workflow-wide clarification gate.
Independent DAG branches continue to run after ordinary dependency failures.

## Retry policy

For read-only steps, the Scheduler consumes `retryable` as an execution
decision rather than only a UI hint:

1. `retryable=false` stops immediately without spending the remaining retry
   budget;
2. `retryable=true` may retry the same Agent once when `TaskStep.retry > 0`;
3. after that retry is exhausted, the Scheduler may redispatch once when
   `SCHEDULER_REDISPATCH_ENABLED=true`;
4. redispatch excludes the failed Agent, requires an authorized candidate with
   a trusted compatible Agent Contract, rebuilds the execution context and
   resolves inputs for the actual Agent;
5. a runtime `CLARIFY`, `REJECT`, `NO_CAPABLE_AGENT`, invalid candidate, or
   routing exception is terminal for recovery and never reopens the
   workflow-wide clarification gate.

The bound is per Scheduler run: initial execution, at most one same-Agent retry,
and at most one redispatch. Recovery-attempt budgets are not persisted in the
checkpoint, so a crash/resume starts a fresh per-run budget. This is accepted
for read-only operations in the prototype and must not be described as a
workflow-lifetime global bound.

Executor-level transport retries are unchanged and remain internal to one
Scheduler logical attempt. Scheduler `attempts` / `retry_count` therefore count
logical step invocations, not every underlying socket or HTTP retry.

Safe operational metrics include `attempts`, `retry_count`,
`redispatch_count`, `redispatch_outcome`, and `recovery_path`. Checkpoints also
retain the payload-free `attempt_failures` entries (`attempt`, `phase`, stable
failure `code`, and `retryable`). Raw exception text and remote diagnostic
payloads remain excluded.

Side-effect steps never enter automatic retry or redispatch.
`SIDE_EFFECT_UNCONFIRMED` always requires manual reconciliation and must never
trigger an automatic resend.
