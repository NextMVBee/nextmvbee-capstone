# Development project B: the founder's pre-written answers

Written 2 November 2026 for evaluation/protocol/EVAL_PROTOCOL.md. Version 2, 2 November 2026: the confirmation block answers with the founder's own statement for the keys of the value block and leaves every other confirmation to the founder (it answered "yes" to every confirmation); reason: a wildcard confirmation turned an invented model-attributed statement into a founder-confirmed input. Version 3, 2 November 2026: the reading of the usage answer gives the founder's figure one kind, a user input (it also called it an assumption); reason: a review of the gold. Version 4, 3 November 2026: value entries for order_lookup_integration and human_handoff added after run devB-R1 asked them (written on the author's behalf). Version 5, 3 November 2026: confirmation entries for channels, order_lookup_integration, human_handoff and volume added after run devB-R1 asked them at clarify round 1 (written on the author's behalf). Version 6, 3 November 2026: value and confirmation entries for target_decisions added after run devB-R1 asked it at clarify round 2 (written on the author's behalf). The run is unattended; `python -m app.runner.cli answers --run <run> --file <this file> --apply` answers every open question of a paused run from the fenced json block below, keyed by the question's kind and then by its requirement key or a lower-case fragment of its text (an entry whose name holds a space is a fragment; app/runner/cli.py). Any question the block does not cover, and any entry whose value is the literal `ask the founder`, stops the run item cleanly: the command prints the question verbatim, exits 4 and applies nothing; the run stays paused for the founder.

The interrupt kinds the Runner can raise, and the answer for each (the same table as project A):

| kind (questions.kind, or the paused tool) | raised by | answer |
|---|---|---|
| hardness | clarify, for every proposed hard key and every stored hard flag | "yes" for the three hard requirements; "no" for any other key |
| confirmation | clarify, for a requirement the model recorded as the founder's statement | the founder's own statement for the key, word for word from the value block (stored as the corrected, confirmed value); for any other key "ask the founder" |
| value | clarify's blocking questions, keyed by requirement_key | the brief's answers for the keys listed; otherwise "ask the founder" |
| usage | the usage figures, the scope question and the summary-length question | the figures below, in the words the parser reads |
| ask_user | the research planner's ask_user tool | "ask the founder" |
| unknown_call | a model call whose result is unknown after a crash (cli resolve) | "continue" (never retry) |
| version_approval | `cli approve` after the run | "ask the founder" (the author alone approves a version) |

```json
{
  "hardness": {"languages": "yes", "budget_monthly_usd": "yes", "email_from_domain": "yes", "*": "no"},
  "confirmation": {
    "supported_languages": "Hebrew in chat and email; English answers are allowed when the customer writes in English",
    "region": "no region requirement; any region is acceptable",
    "platform": "the shop's existing website; a chat widget on it and email; no mobile app",
    "users": "two staff accounts; about twenty customer conversations a day",
    "timeline": "a few weeks; not a hard requirement",
    "channels": "the shop's existing website's chat widget, shown in Hebrew and laid out right to left, and email in and out, the replies sent from the shop's own domain",
    "order_lookup_integration": "the shop runs on a hosted store platform (Shopify); order status is read from the platform's order data through its API; this is a given of the shop, not a decision for the plan",
    "human_handoff": "the two staff receive escalated conversations where they already work: chat escalations in the chat widget's own inbox and email escalations in the shop's mailbox, with the conversation attached; no separate helpdesk tool is wanted unless the chat widget decision brings one within the budget",
    "volume": "about twenty customer conversations a day, a few hundred emails a month, two staff accounts",
    "target_decisions": "three decisions: which language model answers customer questions in Hebrew; which chat widget shows the conversation in Hebrew on the shop's website; and which email service sends the replies from the shop's own domain in Hebrew",
    "*": "ask the founder"
  },
  "value": {
    "languages": "Hebrew in chat and email; English answers are allowed when the customer writes in English",
    "supported_languages": "Hebrew in chat and email; English answers are allowed when the customer writes in English",
    "budget_monthly_usd": "40 US dollars a month for all services together",
    "email_from_domain": "replies are sent from the shop's own domain",
    "region": "no region requirement; any region is acceptable",
    "platform": "the shop's existing website; a chat widget on it and email; no mobile app",
    "users": "two staff accounts; about twenty customer conversations a day",
    "timeline": "a few weeks; not a hard requirement",
    "order_lookup_integration": "the shop runs on a hosted store platform (Shopify); order status is read from the platform's order data through its API; this is a given of the shop, not a decision for the plan",
    "human_handoff": "the two staff receive escalated conversations where they already work: chat escalations in the chat widget's own inbox and email escalations in the shop's mailbox, with the conversation attached; no separate helpdesk tool is wanted unless the chat widget decision brings one within the budget",
    "channels": "the shop's existing website's chat widget, shown in Hebrew and laid out right to left, and email in and out, the replies sent from the shop's own domain",
    "volume": "about twenty customer conversations a day, a few hundred emails a month, two staff accounts",
    "target_decisions": "three decisions: which language model answers customer questions in Hebrew; which chat widget shows the conversation in Hebrew on the shop's website; and which email service sends the replies from the shop's own domain in Hebrew",
    "*": "ask the founder"
  },
  "usage": {
    "how much will the product be used": "600 sessions a month in all, 4 minutes each, 2 users, 1 recipient per summary",
    "team's total or per user": "the team's total: 600 sessions a month in all",
    "how long is one": "150 words per summary",
    "*": "ask the founder"
  },
  "ask_user": {"*": "ask the founder"},
  "unknown_call": "continue",
  "version_approval": "ask the founder"
}
```

Reading of the usage answer (the parser's words, app/core/derived.py): a session is one customer conversation, about twenty a day, 600 a month in all (the founder's figure, a user input of the synthetic project); 4 minutes each is a conversation's length; 2 users are the staff accounts; one recipient per summary is the customer who receives a reply; 150 words per summary is the length of one reply. Every figure is a user input.
