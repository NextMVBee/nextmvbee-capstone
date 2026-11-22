# Development project A: hard requirements

Written 2 November 2026 for evaluation/protocol/EVAL_PROTOCOL.md. Three hard requirements, in the exact form the Runner accepts: the fenced json block below is what `python -m app.runner.cli project --requirements` stores, one row per object, as the Start screen stores a constraint chip with a value (app/web/pages.py): a requirement of the founder's (`source` user), `status` confirmed, `hard` true, `version` 1, with the `key` and `value` of the Requirement model (schemas/models.py, `Requirement`). The decision subjects a requirement binds (`applies_to`) are written by the planner's applicability map during the run; the gold states which binding the evaluation expects.

| key | value | kind of the figure | binds (expected, per the gold) |
|---|---|---|---|
| privacy | trip data stays private: trip requests, places and the traveller's text are not used by the routing or language-model vendor to train its models or for anything beyond providing the service | user input (the founder's statement) | the routing and places service; the itinerary language model |
| budget_monthly_usd | 50 | user input (the founder's statement, the app budget of the website example) | every cost-bearing decision |
| encryption_at_rest | saved itineraries and backups are encrypted at rest | user input (the founder's statement) | the itinerary storage |

```json
[
  {"key": "privacy", "value": "trip data stays private: trip requests, places and the traveller's text are not used by the routing or language-model vendor to train its models or for anything beyond providing the service"},
  {"key": "budget_monthly_usd", "value": "50"},
  {"key": "encryption_at_rest", "value": "saved itineraries and backups are encrypted at rest"}
]
```

Notes. The value of `budget_monthly_usd` is a string, as the Start screen stores a chip's value; app/core/derived.py reads the figure. The privacy requirement names its scope in its own words so that a storage or hosting vendor is not held to a model-training statement it has no reason to publish; the gold's decision A3 binds encryption at rest and the budget instead. Nothing else in the brief is hard: the hardness answer for any other key is "no" (evaluation/protocol/projects/devA_trip_planner/founder_answers.md).
