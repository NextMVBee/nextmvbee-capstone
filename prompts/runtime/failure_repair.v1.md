# failure_repair.v1: One repair after a failure shape

| Field | Value |
|---|---|
| Purpose | A stage answered with its failure shape (failure_reason set). Code asks once more with this user turn appended; a second failure shape is flagged, never retried again (Phase 4 task 4). |
| Allowed tools | the stage's own |
| Output schema | the stage's contract |

## Template

```
System: (the stage's own System text is kept)
User: Your previous answer was the failure shape with this reason: {failure_reason}
Use only the information already given and answer the schema as far as the information allows; mark what is uncertain as an assumption, a question or an unresolved check instead of refusing. Return the failure shape again only if no part of the task can be done.
```

Status: designed for Phase 4 (8 October 2026).
