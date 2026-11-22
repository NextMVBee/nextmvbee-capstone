# panel.v3: the blind-spot check of a drafted decision memo (Phase 9b; D-A52, D-A58)

| Field | Value |
|---|---|
| Purpose | After a decision's memo is drafted, ask a panel of current frontier models (the openrouter:fusion tool with the documented quality preset, web search and fetch inside) whether the draft pick missed a product, a fact or a contradiction, and return the panel's analysis as it came back. |
| Inputs | run_date; decision_subject; product_class; binding_requirements; usage; candidates (the typed candidates compared); draft (the recommendation, the runner-up, the why sentences) |
| Allowed tools | the server tool openrouter:fusion, once, with max_tool_calls 4 for the panel (set by code); no function tools |
| Output schema | PanelOut (schemas/models.py): status, consensus, contradictions (topic, stances), partial_coverage, unique_insights, blind_spots, products_named (vendor, product, product_class, why), models, responses (model, outcome, note; D-A212), failure_reason |
| Evidence requirements | None: nothing from the panel enters a memo without the application's own evidence. Code turns a product the panel names that is not a candidate into a typed candidate, researched and verified in one bounded follow-up, and a contradiction into a research question; then the memo is finalised once. |
| Failure behavior | A failed fusion call (the tool is in beta) returns status error with the reason; code records it and never blocks the decision on it. |
| Stop conditions | One fusion call. |
| Validation outside the model | Code reads only the fields named above, drops a named product of another class, counts the substantive responses (D-A212) and keeps the decision record's one line ("Panel check: 3 models requested, 1 answered with a substantive response (2 reported the fusion tool unavailable); 2 blind spots researched; the pick held"). |

## Template

```
System: You run one blind-spot check on a drafted technology decision. Today is {run_date}. Call the fusion tool once with the question below and copy its analysis into the answer without editing it: the consensus points, every contradiction with each model's stance, the partial coverage, the unique insights, the blind spots, and every product the panel named (vendor, product, class, why). List the panel's models and, for each, what its response was. Do not add your own recommendation and do not quote web pages: code researches and verifies anything new itself.
The question for the panel: given this decision subject, its binding requirements, the founder's usage, the candidates compared and the draft pick, what current product of this class did the draft miss, what fact in the draft is wrong or outdated on today's date, and where do the sources contradict each other?
Output rule: return one JSON object with exactly these keys and no others (the tool loop does not enforce the schema, so the key names below are the contract): {"status": "ok", "consensus": ["<point>"], "contradictions": [{"topic": "<topic>", "stances": [{"model": "<model>", "stance": "<stance>"}]}], "partial_coverage": [{"models": ["<model>"], "point": "<point>"}], "unique_insights": [{"model": "<model>", "insight": "<insight>"}], "blind_spots": ["<topic>"], "products_named": [{"vendor": "<vendor>", "product": "<product>", "product_class": "<class>", "why": "<why>"}], "models": ["<model>"], "responses": [{"model": "<model>", "outcome": "substantive|tool_unavailable|error|empty", "note": "<the response in a few words>"}], "failure_reason": null}. For each analysis model of the panel, one responses entry with its outcome: substantive when it answered the question with facts, tool_unavailable when it reported that the fusion tool or a tool it needed was unavailable, error when it failed, empty when it gave no answer; copy what the panel reported, never guess. The JSON is the last thing in the answer, in one fenced json block, after nothing else of yours. If the fusion tool fails, return the same keys with status "error", the lists empty and failure_reason set.
User: Decision subject: {decision_subject} (product class {product_class})
Binding requirements: {binding_requirements}
The founder's monthly usage: {usage}
Candidates compared: {candidates}
The draft: {draft}
Run date: {run_date}
```

Status: v3 for Phase 11 package 11.2 (3 November 2026; D-A212): v2 plus the responses key, one entry per analysis model with its outcome, so the memo's panel line states how many models answered with a substantive response (p6-R39 printed "3 models" over one substantive response). v2 archived. A change to this text bumps the version suffix and is recorded in docs/DECISIONS.md (D-17).
