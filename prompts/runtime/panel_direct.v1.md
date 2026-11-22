# panel_direct.v1: the blind-spot check of a drafted decision memo by the route's one model, with nothing offered (Phase 10 package 10.5 round 3; D-A206, D-A210)

| Field | Value |
|---|---|
| Purpose | After a decision's memo is drafted, when settings `[discovery].panel_server_tools` is false (the component experiment on frozen evidence, D-A206), ask the route's one model whether the draft pick missed a product, a fact or a contradiction, from what the model itself knows on the run date. The request offers nothing: no server tool, no function tool, no page, no other model. The product's fusion panel keeps panel.v2. |
| Inputs | run_date; decision_subject; product_class; binding_requirements; usage; candidates (the labels of the candidates the comparison saw: the packs' candidates); draft (the recommendation, the runner-up, the why sentences). The packs' claim text is not supplied. |
| Allowed tools | None: the request carries no tool, and the answer is the model's own reading. |
| Output schema | PanelOut (schemas/models.py): status, consensus, contradictions (topic, stances), partial_coverage, unique_insights, blind_spots, products_named (vendor, product, product_class, why), models, failure_reason. The schema is enforced by response_format (no server loop runs). |
| Evidence requirements | None: nothing from this reading enters a memo without the application's own evidence; under the experiment the request for a follow-up is recorded and never researched (D-A188). |
| Failure behavior | When the model cannot judge the draft (the subject is outside what it knows), status error with failure_reason; code records it and never blocks the decision on it. |
| Stop conditions | One send. |
| Validation outside the model | Code reads only the fields named above, drops a named product of another class, and keeps the decision record's one line ("Panel check: 1 model; 2 blind spots researched; the pick held"). |

## Template

```
System: You run one blind-spot check on a drafted technology decision, alone, from what you know. Today is {run_date}. Nothing is offered to you in this request: no search, no page, no other model. Request nothing and quote no web page. Read the decision subject, its binding requirements, the founder's usage, the candidates compared and the draft pick, and answer from your own knowledge as of today: the points of the draft you agree with (consensus); each fact in the draft that is wrong or outdated (a contradiction: its topic, the draft's stance under the name "draft" and yours under your own model name); what the draft covers only in part (partial_coverage, with your own model name); what you know that the draft does not use (unique_insights, with your own model name); the topics the draft never examined (blind_spots); and every current product of this class that the draft missed (products_named: vendor, product, class, why). Do not add your own recommendation: code researches and verifies anything new itself. Under models list the one model that answered, your own name.
Output rule: return only JSON that matches the provided schema. No prose outside JSON. If you cannot judge this draft, return the schema with status "error", the lists empty and failure_reason set.
User: Decision subject: {decision_subject} (product class {product_class})
Binding requirements: {binding_requirements}
The founder's monthly usage: {usage}
Candidates compared: {candidates}
The draft: {draft}
Run date: {run_date}
```

Status: v1 for Phase 10 package 10.5 round 3 (3 November 2026): the instruction of the one-model branch that D-A206 opened (the panel send with no tool offered), which until this version carried panel.v2's instruction to call the fusion tool and copy its analysis (Codex 10.5 round 2, SF-1). panel.v2 is unchanged for the product's fusion panel. A change to this text bumps the version suffix and is recorded in docs/DECISIONS.md (D-17).
