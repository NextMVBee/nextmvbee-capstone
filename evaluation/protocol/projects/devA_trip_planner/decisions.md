# Development project A: the decisions the evaluation scores

Written 2 November 2026 for evaluation/protocol/EVAL_PROTOCOL.md. Four decisions. `python -m app.runner.cli start --decisions` loads them into the run: the research planner receives them as the decisions to support and may add subjects; the gold (docs/eval/gold/devA_gold.md) matches a run's subject to a decision below by its name or by the subject words listed in the gold's fenced json block. A decision the run never opens is scored as an abstention.

Version 2, 2 November 2026: the fenced json block at the end, the machine-readable form the start command loads (subject, question, candidate pool rule, research questions, criteria; the expected binding `binds` is the gold's and is never sent to the planner); reason: the decisions were hashed but never loaded into the Runner. Version 1, 2 November 2026: the first text.

## A1. The itinerary language model

- Question: which language-model API writes the day summaries and answers questions about the plan?
- Candidate pool rule: a commercial, hosted language-model API with published per-token prices and a published statement on the use of API customer data; the vendor's current (not deprecated) small or fast model. At least three candidates from at least two vendors.
- Research questions: does the vendor use API prompts and outputs to train its models, and under what condition (tier, opt-in, opt-out, preview models)? What is the price per million input and output tokens of the candidate model? Is the model current on the run date (the vendor's model page does not mark it deprecated)?
- Criteria: the privacy requirement (binding; one explicit vendor statement decides each way); the budget at the usage the founder gives (a calculation from the quoted prices); currency; then quality claims the vendor publishes.
- Hard requirements bound: privacy, budget_monthly_usd.

## A2. The routing and places service

- Question: which service computes routes between several stops and supplies their opening hours?
- Candidate pool rule: a hosted routing or maps API with a directions method over several waypoints and place details that include opening hours (or a documented way to obtain them); a published free allowance or price per request. At least three candidates.
- Research questions: does the directions method accept several stops (how many)? Are opening hours returned by the places method and under which SKU? What is the free monthly allowance and the price per 1,000 requests after it? Does the vendor state whether request data is used to train its models or for its own purposes?
- Criteria: the privacy requirement (binding); the budget at about 100 itineraries a month, each a few routing calls and place lookups (a calculation); the feature fit (several stops, opening hours).
- Hard requirements bound: privacy, budget_monthly_usd.

## A3. The itinerary storage

- Question: where are saved itineraries, notes and backups stored?
- Candidate pool rule: a hosted database or object store with a free or low tier and a published security page; at least three candidates.
- Research questions: does the vendor state that stored data is encrypted at rest? What are the free tier's storage and operation limits, and is a free project paused or deleted after inactivity? What does the first paid tier cost?
- Criteria: encryption at rest (binding; one explicit statement decides; absence of a statement leaves the candidate unsupported, not rejected); the budget (a calculation at the pilot's size: about 100 travellers, a few megabytes of itineraries); the pause or sleep conditions as a risk.
- Hard requirements bound: encryption_at_rest, budget_monthly_usd.

## A4. The web hosting

- Question: where does the web app itself run?
- Candidate pool rule: a hosted platform that runs a small always-on or on-demand web service from a container or a repository, with a published monthly price for its smallest plan; at least three candidates.
- Research questions: what does the smallest plan cost a month and what does it include? Does a free plan exist and does it spin down or sleep? Is the plan's use restricted (personal, non-commercial)?
- Criteria: the budget together with the other three services (a calculation); the spin-down and use restrictions as risks, not rejections, unless a restriction names the pilot's use.
- Hard requirements bound: budget_monthly_usd.

## The expected binding of the hard requirements

privacy binds A1 and A2 (its value names the routing and language-model vendors); encryption_at_rest binds A3; budget_monthly_usd binds every cost-bearing decision, A1 to A4. A run whose applicability map binds a requirement elsewhere is scored against the gold's binding, and the difference is reported.

## The machine-readable decisions (loaded by `cli start --decisions`)

```json
[
  {
    "id": "A1",
    "subject": "itinerary_language_model",
    "question": "which language-model API writes the day summaries and answers questions about the plan?",
    "candidate_pool_rule": "a commercial, hosted language-model API with published per-token prices and a published statement on the use of API customer data; the vendor's current (not deprecated) small or fast model. At least three candidates from at least two vendors.",
    "research_questions": [
      "does the vendor use API prompts and outputs to train its models, and under what condition (tier, opt-in, opt-out, preview models)?",
      "what is the price per million input and output tokens of the candidate model?",
      "is the model current on the run date (the vendor's model page does not mark it deprecated)?"
    ],
    "criteria": "the privacy requirement (binding; one explicit vendor statement decides each way); the budget at the usage the founder gives (a calculation from the quoted prices); currency; then quality claims the vendor publishes.",
    "binds": [
      "privacy",
      "budget_monthly_usd"
    ]
  },
  {
    "id": "A2",
    "subject": "routing_and_places_service",
    "question": "which service computes routes between several stops and supplies their opening hours?",
    "candidate_pool_rule": "a hosted routing or maps API with a directions method over several waypoints and place details that include opening hours (or a documented way to obtain them); a published free allowance or price per request. At least three candidates.",
    "research_questions": [
      "does the directions method accept several stops (how many)?",
      "are opening hours returned by the places method and under which SKU?",
      "what is the free monthly allowance and the price per 1,000 requests after it?",
      "does the vendor state whether request data is used to train its models or for its own purposes?"
    ],
    "criteria": "the privacy requirement (binding); the budget at about 100 itineraries a month, each a few routing calls and place lookups (a calculation); the feature fit (several stops, opening hours).",
    "binds": [
      "privacy",
      "budget_monthly_usd"
    ]
  },
  {
    "id": "A3",
    "subject": "itinerary_storage",
    "question": "where are saved itineraries, notes and backups stored?",
    "candidate_pool_rule": "a hosted database or object store with a free or low tier and a published security page; at least three candidates.",
    "research_questions": [
      "does the vendor state that stored data is encrypted at rest?",
      "what are the free tier's storage and operation limits, and is a free project paused or deleted after inactivity?",
      "what does the first paid tier cost?"
    ],
    "criteria": "encryption at rest (binding; one explicit statement decides; absence of a statement leaves the candidate unsupported, not rejected); the budget (a calculation at the pilot's size: about 100 travellers, a few megabytes of itineraries); the pause or sleep conditions as a risk.",
    "binds": [
      "encryption_at_rest",
      "budget_monthly_usd"
    ]
  },
  {
    "id": "A4",
    "subject": "web_hosting",
    "question": "where does the web app itself run?",
    "candidate_pool_rule": "a hosted platform that runs a small always-on or on-demand web service from a container or a repository, with a published monthly price for its smallest plan; at least three candidates.",
    "research_questions": [
      "what does the smallest plan cost a month and what does it include?",
      "does a free plan exist and does it spin down or sleep?",
      "is the plan's use restricted (personal, non-commercial)?"
    ],
    "criteria": "the budget together with the other three services (a calculation); the spin-down and use restrictions as risks, not rejections, unless a restriction names the pilot's use.",
    "binds": [
      "budget_monthly_usd"
    ]
  }
]
```
