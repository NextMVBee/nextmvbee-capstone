# Development project A: the founder's pre-written answers

Written 2 November 2026 for evaluation/protocol/EVAL_PROTOCOL.md. Version 2, 2 November 2026: the confirmation block answers with the founder's own statement for the keys of the value block and leaves every other confirmation to the founder (it answered "yes" to every confirmation); reason: a wildcard confirmation turned an invented model-attributed statement into a founder-confirmed input. Version 3, 2 November 2026: the reading of the usage answer gives the founder's figure one kind, a user input (it also called it an assumption); reason: a review of the gold. Version 4, 3 November 2026: confirmation entries for pilot_workload added after run devA-R2 asked them at clarify round 1 (written on the author's behalf). The run is unattended; `python -m app.runner.cli answers --run <run> --file <this file> --apply` answers every open question of a paused run from the fenced json block below, keyed by the question's kind and then by its requirement key or a lower-case fragment of its text (an entry whose name holds a space is a fragment; app/runner/cli.py). Any question the block does not cover, and any entry whose value is the literal `ask the founder`, stops the run item cleanly: the command prints the question verbatim, exits 4 and applies nothing; the run stays paused for the founder.

The interrupt kinds the Runner can raise, and the answer for each:

| kind (questions.kind, or the paused tool) | raised by | answer |
|---|---|---|
| hardness | clarify, for every proposed hard key and every stored hard flag (app/graph/workflow.py, clarify) | "yes" for the three hard requirements; "no" for any other key (nothing else is hard) |
| confirmation | clarify, for a requirement the model recorded as the founder's statement | the founder's own statement for the key, word for word from the value block (the Runner stores it as the corrected, confirmed value); for any other key "ask the founder": a statement the founder did not pre-write is never confirmed |
| value | clarify's blocking questions (at most three), keyed by requirement_key | the brief's answers for the keys listed; otherwise "ask the founder" |
| usage | the usage figures (app/core/derived.py USAGE_QUESTION), the scope question and the summary-length question, asked before the first cost comparison | the figures below, in the words the parser reads |
| ask_user | the research planner's ask_user tool (app/core/tools.py), a paused tool call | "ask the founder" (nothing covered) |
| unknown_call | a model call whose result is unknown after a crash (cli resolve) | "continue" (never retry: the call is not resent, its possible charge stays counted) |
| version_approval | `cli approve` after the run | "ask the founder" (the author alone approves a version) |

```json
{
  "hardness": {"privacy": "yes", "budget_monthly_usd": "yes", "encryption_at_rest": "yes", "*": "no"},
  "confirmation": {
    "region": "no region requirement; any region is acceptable",
    "platform": "a web app that works in a phone browser; no native app in the first version",
    "users": "about 100 travellers in the pilot",
    "timeline": "a few weeks of one developer's work; not a hard requirement",
    "pilot_workload": "about 100 itinerary requests a month across about 100 travellers in the pilot; each request makes a few routing calls and one or two summaries of about 200 words",
    "*": "ask the founder"
  },
  "value": {
    "privacy": "trip data stays private: trip requests, places and the traveller's text are not used by the routing or language-model vendor to train its models or for anything beyond providing the service",
    "budget_monthly_usd": "50 US dollars a month for all services together, the app budget",
    "encryption_at_rest": "saved itineraries and backups are encrypted at rest",
    "region": "no region requirement; any region is acceptable",
    "platform": "a web app that works in a phone browser; no native app in the first version",
    "users": "about 100 travellers in the pilot",
    "timeline": "a few weeks of one developer's work; not a hard requirement",
    "pilot_workload": "about 100 itinerary requests a month across about 100 travellers in the pilot; each request makes a few routing calls and one or two summaries of about 200 words",
    "*": "ask the founder"
  },
  "usage": {
    "how much will the product be used": "100 sessions a month in all, 3 minutes each, 100 users, 1 recipient per summary",
    "team's total or per user": "the team's total: 100 sessions a month in all",
    "how long is one": "200 words per summary",
    "*": "ask the founder"
  },
  "ask_user": {"*": "ask the founder"},
  "unknown_call": "continue",
  "version_approval": "ask the founder"
}
```

Reading of the usage answer (the parser's words, app/core/derived.py): a session is one itinerary request; 100 a month in all (the founder's figure, a user input); 3 minutes each stands for the planning session's length, which no cost here depends on; 100 users are the pilot's travellers; one recipient per summary is the traveller herself; 200 words per summary is the length of one day's written summary (the founder's figure). Every figure is a user input.
