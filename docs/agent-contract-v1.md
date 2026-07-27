# Agent Contract v1

Agent Contract v1 defines stable business inputs and outputs independently of
the tools used to obtain them. This version applies to
`RemoteHRAssistantAgent`, `RemoteKnowledgeAgent`, and `RemoteReportAgent`.
Agents without a contract continue to use the legacy registration and execution
path.

This document specifies and validates results that already follow the new
protocol. It does not normalize legacy Agent output, change Scheduler behavior,
or implement Artifact fan-in.

## Scope of runtime enforcement

Contract v1 ships the validation building blocks only. `validate_agent_result`
and `register_agent_schemas` are not yet called on the runtime execution path;
they are wired in by the result-normalization layer (the follow-up
"Agent result normalization / Artifact fan-in" PR), which registers the schema
catalog on the scheduler execution path and runs `validate_agent_result` on
every contracted remote result before it reaches downstream steps. Until that
layer lands, contracted results are still gated by the remote error envelope
(`status=error` fails the step), but schema conformance is not enforced at
runtime.

`DataContractRef.required` and `cardinality` are declared for forward
compatibility and are not enforced by v1 validation.

## Contract

An `AgentContract` has a `contract_version`, `requires`, and `produces`.
Each data reference contains:

```json
{
  "name": "employee.info",
  "schema_ref": "employee.info@v1",
  "required": true,
  "cardinality": "one"
}
```

`name` is a business logical name, never a tool name. `schema_ref` must resolve
through the existing `SchemaRegistry`. Contract v1 uses exact schema versions
and fails closed when a schema is missing or mismatched. A registry entry whose
contract declares names without schema refs is rejected at sync time: that
Agent is not registered, while the rest of the batch continues to load.

Registry metadata may additionally declare `legacy_produces`: logical names
that predate the contract and are still referenced by planner dependency
chains (for example `employee.id` and `employee.name`). They are appended to
the Agent's planner-visible `produces` but stay outside the strict contract,
so they require no schema refs and are not validated.

## Result envelope

Every contracted Agent returns:

```json
{
  "contract_version": "1.0",
  "status": "success",
  "outputs": {
    "employee.info": {
      "records": [],
      "matched_count": 0
    }
  },
  "error": null,
  "metadata": {
    "producer_agent": "RemoteHRAssistantAgent",
    "schema_version": "1.0"
  }
}
```

The allowed statuses are `success`, `partial`, and `error`.

- `success` contains at least one output and no error.
- `error` contains a standard error and no outputs.
- `partial` contains both valid outputs and a standard error.
- Every output logical name is declared by the Agent's `produces`.
- Every output payload validates against its declared schema.

The standard error is:

```json
{
  "code": "REMOTE_TOOL_TIMEOUT",
  "message": "remote_person_info_tool timed out",
  "retryable": true,
  "details": {
    "tool": "remote_person_info_tool"
  }
}
```

Metadata is deliberately allow-listed. It must not contain credentials, full
request context, or sensitive business content.

## Schema catalog

Contract v1 registers these schemas in the existing `SchemaRegistry`:

- `employee.info@v1`: employee record collection, optional query and match count.
- `employee.salary@v1`: salary record collection and match count.
- `policy.info@v1`: query, answer, knowledge item count, and policy scope.
- `report.sources@v1`: generic report sources, instruction, and title.
- `report.markdown@v1`: title, Markdown body, and source count.

`policy_scope` is one of `company`, `statutory`, `mixed`, or `unknown`.
Statutory material must not be presented as a current internal company policy.

## Pilot contracts

`RemoteHRAssistantAgent` produces `employee.info` and, only for an explicit
salary request, `employee.salary`.

`RemoteKnowledgeAgent` produces `policy.info`. The current demonstration
knowledge base contains statutory material, so results default to
`policy_scope=statutory` unless the tool supplies explicit provenance.

`RemoteReportAgent` requires the generic `report.sources` input and produces
`report.markdown`. Its contract is not tied to HR-specific source names.
Because `report.sources` is a synthetic fan-in input that no single Agent
produces, the registry entry defers this `requires` declaration until the
fan-in layer lands; until then the planner treats the Report Agent as
autonomous, exactly as before this contract existed.

## Validation order

`validate_agent_result` checks:

1. Envelope structure.
2. Status, outputs, and error invariants.
3. Contract version.
4. Declared output logical names.
5. Registered schema references.
6. Payload conformance.

Validation returns structured errors. It never rewrites the Agent result.
