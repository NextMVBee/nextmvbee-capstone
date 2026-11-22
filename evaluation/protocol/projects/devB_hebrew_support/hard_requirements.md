# Development project B: hard requirements

Written 2 November 2026 for evaluation/protocol/EVAL_PROTOCOL.md. Three hard requirements, in the exact form the Runner accepts: the fenced json block below is what `python -m app.runner.cli project --requirements` stores, one row per object, as the Start screen stores a constraint chip with a value (app/web/pages.py): a requirement of the founder's (`source` user), `status` confirmed, `hard` true, `version` 1, with the `key` and `value` of the Requirement model (schemas/models.py, `Requirement`). The decision subjects a requirement binds (`applies_to`) are written by the planner's applicability map during the run; the gold states which binding the evaluation expects.

| key | value | kind of the figure | binds (expected, per the gold) |
|---|---|---|---|
| languages | Hebrew in chat and email | user input (the founder's statement) | the assistant language model; the chat widget; the email delivery |
| budget_monthly_usd | 40 | user input: a figure chosen by the author of this file for the synthetic project and marked as the founder's input; the plan's cost ceiling | every cost-bearing decision |
| email_from_domain | replies are sent from the shop's own domain | user input (the founder's statement) | the email delivery |

```json
[
  {"key": "languages", "value": "Hebrew in chat and email"},
  {"key": "budget_monthly_usd", "value": "40"},
  {"key": "email_from_domain", "value": "replies are sent from the shop's own domain"}
]
```

Notes. `languages` is the Start screen's own chip key; its value names Hebrew, so the language rule of the evidence suite applies (a claim counts for the requirement only when its quote names Hebrew). The value of `budget_monthly_usd` is a string, as the Start screen stores a chip's value; app/core/derived.py reads the figure. Nothing else in the brief is hard: the hardness answer for any other key is "no" (evaluation/protocol/projects/devB_hebrew_support/founder_answers.md).
