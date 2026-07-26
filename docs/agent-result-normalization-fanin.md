# Agent Result Normalization and Artifact Fan-in

This document defines the execution boundary introduced after Agent Contract
v1. It applies to contracted Agents while preserving the existing single-output
path for Agents that have not adopted a contract.

## Result normalization

The Scheduler normalizes an executor result before publishing Artifacts:

- A valid `AgentResultEnvelope` is checked against the selected Agent Contract.
- An unambiguous legacy payload may be mapped to declared contract outputs.
- Explicit business errors, partial results, malformed envelopes, missing
  required outputs, undeclared outputs, and Schema failures fail closed.
- Each named output creates its own typed Artifact and ArtifactRef.
- Contracted output is published only when `schema_valid` is `true`.

`partial` is not downstream-consumable in this version. Optional-output
semantics are expressed by `DataContractRef.required=false`, not by returning a
partial envelope for a successful request that did not ask for that output.

## Fan-in binding

A normal single-source input continues to use:

```json
{
  "parameter_name": "attachment",
  "source_step": "document_step",
  "source_output": "document"
}
```

One contract input assembled from multiple Artifacts uses:

```json
{
  "parameter_name": "report.sources",
  "source_artifacts": [
    {
      "source_step": "hr_step",
      "source_output": "employee.info"
    },
    {
      "source_step": "knowledge_step",
      "source_output": "policy.info"
    }
  ],
  "assembly": {
    "schema_ref": "report.sources@v1",
    "title": "员工档案与年假制度综合汇总",
    "instruction": "使用全部来源形成 Markdown 综合汇总"
  }
}
```

The single-source fields and `source_artifacts` are mutually exclusive.
Dependencies are derived from every `source_step`. Every source output must
exist exactly; the Scheduler never falls back to an arbitrary output.

The assembled `report.sources@v1` value contains each source's logical name,
Schema reference, and actual payload. The resulting `report.markdown` Artifact
records all consumed ArtifactRefs in `derived_from`.

## Failure boundary

If a required source is missing, inaccessible, invalid, or incompatible, the
consumer step does not run. If a contracted result cannot be normalized or
validated, no output from that step is registered for downstream resolution.

Frontend error presentation and MCP mapping are intentionally outside this
change.
