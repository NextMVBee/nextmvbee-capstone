# Development project B: the decisions the evaluation scores

Written 2 November 2026 for evaluation/protocol/EVAL_PROTOCOL.md. Three decisions. `python -m app.runner.cli start --decisions` loads them into the run: the research planner receives them as the decisions to support and may add subjects; the gold (docs/eval/gold/devB_gold.md) matches a run's subject to a decision below by its name or by the subject words listed in the gold's fenced json block. A decision the run never opens is scored as an abstention.

Version 2, 2 November 2026: the fenced json block at the end, the machine-readable form the start command loads (subject, question, candidate pool rule, research questions, criteria; the expected binding `binds` is the gold's and is never sent to the planner); reason: the decisions were hashed but never loaded into the Runner. Version 1, 2 November 2026: the first text.

## B1. The assistant language model

- Question: which language-model API answers customer questions in Hebrew?
- Candidate pool rule: a commercial, hosted language-model API with published per-token prices; the vendor's current (not deprecated) small or fast model. At least three candidates from at least two vendors.
- Research questions: does the vendor document Hebrew among the languages the model understands and answers in (a page that names Hebrew)? What is the price per million input and output tokens? Is the model current on the run date?
- Criteria: the language requirement (binding; a quote counts only when it names Hebrew; a general "multilingual" statement does not); the budget at the shop's volume (a calculation); currency.
- Hard requirements bound: languages, budget_monthly_usd.

## B2. The chat widget

- Question: which chat widget shows the conversation to customers in Hebrew on the shop's website?
- Candidate pool rule: a hosted live-chat or helpdesk product with a website widget, a published list of widget languages or a right-to-left statement, and a published monthly price (free plan or per seat). At least three candidates.
- Research questions: does the vendor name Hebrew among the widget's languages, or state right-to-left support? What does the plan the shop needs cost a month for two staff accounts, and is a free plan sufficient? Does the widget hand a conversation to a human with its history?
- Criteria: the language requirement (binding; a page naming Hebrew decides); the budget for the seats the shop uses (a calculation); the handover as a feature criterion.
- Hard requirements bound: languages, budget_monthly_usd.

## B3. The email delivery

- Question: which service sends the assistant's replies by email from the shop's own domain, in Hebrew?
- Candidate pool rule: a transactional email API with a published free or entry plan, custom sending domains, and a published price per thousand emails. At least three candidates.
- Research questions: does the service send from a domain the shop owns, on the plan the shop would use? What are the free plan's monthly and daily limits and the first paid plan's price? Does the vendor state that message content in Hebrew (or in UTF-8 or Unicode) is supported?
- Criteria: the own-domain requirement (binding; the vendor's own statement decides); the language requirement (binding if the run's map binds it here; a quote naming Hebrew, or UTF-8 content, is what the evidence rule needs); the budget at a few hundred emails a month (a calculation).
- Hard requirements bound: email_from_domain, languages, budget_monthly_usd.

## The expected binding of the hard requirements

languages binds B1, B2 and B3 (chat and email are both named in its value); email_from_domain binds B3; budget_monthly_usd binds every cost-bearing decision, B1 to B3. A run whose applicability map binds a requirement elsewhere is scored against the gold's binding, and the difference is reported. B3 is the project's expected justified abstention when the language requirement binds it: the gold's source set holds no email vendor page naming Hebrew or UTF-8 content, so the right outcome is an open decision with a settling check naming that statement.

## The machine-readable decisions (loaded by `cli start --decisions`)

```json
[
  {
    "id": "B1",
    "subject": "assistant_language_model",
    "question": "which language-model API answers customer questions in Hebrew?",
    "candidate_pool_rule": "a commercial, hosted language-model API with published per-token prices; the vendor's current (not deprecated) small or fast model. At least three candidates from at least two vendors.",
    "research_questions": [
      "does the vendor document Hebrew among the languages the model understands and answers in (a page that names Hebrew)?",
      "what is the price per million input and output tokens?",
      "is the model current on the run date?"
    ],
    "criteria": "the language requirement (binding; by D-A155 a quote counts only when it names Hebrew; a general \"multilingual\" statement does not); the budget at the shop's volume (a calculation); currency.",
    "binds": [
      "languages",
      "budget_monthly_usd"
    ]
  },
  {
    "id": "B2",
    "subject": "chat_widget",
    "question": "which chat widget shows the conversation to customers in Hebrew on the shop's website?",
    "candidate_pool_rule": "a hosted live-chat or helpdesk product with a website widget, a published list of widget languages or a right-to-left statement, and a published monthly price (free plan or per seat). At least three candidates.",
    "research_questions": [
      "does the vendor name Hebrew among the widget's languages, or state right-to-left support?",
      "what does the plan the shop needs cost a month for two staff accounts, and is a free plan sufficient?",
      "does the widget hand a conversation to a human with its history?"
    ],
    "criteria": "the language requirement (binding; a page naming Hebrew decides); the budget for the seats the shop uses (a calculation); the handover as a feature criterion.",
    "binds": [
      "languages",
      "budget_monthly_usd"
    ]
  },
  {
    "id": "B3",
    "subject": "email_delivery",
    "question": "which service sends the assistant's replies by email from the shop's own domain, in Hebrew?",
    "candidate_pool_rule": "a transactional email API with a published free or entry plan, custom sending domains, and a published price per thousand emails. At least three candidates.",
    "research_questions": [
      "does the service send from a domain the shop owns, on the plan the shop would use?",
      "what are the free plan's monthly and daily limits and the first paid plan's price?",
      "does the vendor state that message content in Hebrew (or in UTF-8 or Unicode) is supported?"
    ],
    "criteria": "the own-domain requirement (binding; the vendor's own statement decides); the language requirement (binding if the run's map binds it here; a quote naming Hebrew, or UTF-8 content, is what the evidence rule needs); the budget at a few hundred emails a month (a calculation).",
    "binds": [
      "email_from_domain",
      "languages",
      "budget_monthly_usd"
    ]
  }
]
```
