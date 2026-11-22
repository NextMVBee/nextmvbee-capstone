# The design of NextMVBee

This document says how the system is built and why. Each section ends with the files to read and the commands that show the rule at work. Where a part is not in this repository, the section says so.

The whole design in one sentence: models extract claims, judge support and compare options; code enforces the rules a model cannot be trusted with; the founder approves.

## 1. One run, seven stages

1. **Clarify.** Turn the idea into requirements and propose which of them are must-haves.
2. **Ask the founder.** Stop and wait whenever only the founder can answer.
3. **Plan the research.** Decide which technical decisions the plan needs, which products are candidates for each, and what to look for.
4. **Extract evidence.** Read the stored pages and pull out claims, each with its quote.
5. **Verify.** Check every claim against its page and decide which must-have it bears on.
6. **Compare the options.** Recommend one service per decision, or leave the decision open.
7. **Write the plan.**

Code routes between the stages. Which stage comes next, whether a tool may be used, whether money may be spent and when a run stops are decided by code that reads the run's recorded state. No prompt decides what runs next. In the code, six of the seven are stages with model calls (`STAGES` in `app/graph/workflow.py`); asking the founder is an interrupt of the graph.

Every model call has a step key made of the run, the stage, the worker, the branch and the item. By design, a step that completed is replayed from the store and is not sent again, so a node that runs twice writes nothing twice.

Read: `app/graph/workflow.py` (the graph and its routers), `schemas/models.py` (the typed contract of each stage), `app/runner/runner.py` (start and resume).

## 2. Twelve kinds of role, and why they are separate

Twelve kinds of model role work inside the seven stages. A role is a model with its own instructions, its own typed output contract and its own permissions. Roles do not talk to each other. They exchange typed records through the store, and code validates every record.

A role is separate from another for one of three reasons.

- **It needs different tools.** Five roles hold search or reading tools: the research planner, the discovery scout (which asks two search engines for candidate products), the researcher worker, the verifier (which may read a page again) and the panel (three models that read a draft comparison). Seven hold none: the clarifier, the extractor, the coverage judge, the comparer, the plan writer, the plan enricher and the editor. Code refuses a tool call outside a stage's list, whatever a web page says.
- **It can work in parallel.** Up to three researcher workers run at once on independent tasks.
- **It must check another role independently.** The verifier checks the extractor's quotes. The coverage judge decides whether verified claims support a must-have before the comparer chooses. The panel reads each draft comparison.

The three roles that write the plan differ in what each may change. The plan writer produces the plan's body. The plan enricher adds entities, interfaces and checks, and code drops an interface that names an unknown component. The editor may change wording and no value: code drops a rewrite that changes a number, cites a different claim or adds a fact.

One case shows what the separation buys. In a test run of 29 to 30 October 2026, the comparison model chose a language model for which a separate check had found no support for Hebrew and English within one call. The check's verdict was on record, and code refused the choice. This does not show that the product lacks the capability. It shows that the choice was not supported by the evidence on file.

Every role adds calls, cost, waiting time and a merge step. No single-agent baseline was tested, and nothing shows that twelve is the best number.

Read: `prompts/runtime/` (the instructions of the roles the review path drives), `schemas/models.py` and `schemas/json/` (the contract of every role), `app/core/roles.py` (the parallel workers and the merge), `app/core/tools.py` (tool permissions by stage). The instructions of the discovery scout, the plan enricher and the editor are not in this repository.

## 3. How a statement becomes evidence

A sentence on a vendor's page passes four steps before a decision may rest on it.

1. **The page is stored as untrusted text.** It is data, never instruction. A page with text that reads like an instruction to the system is flagged and excluded until the founder decides.
2. **A researcher's proposal is only a direction.** What a researcher worker reports is a lead to check. It never counts as evidence.
3. **The quote must be on the stored page, word for word.** The extractor returns a claim with a quote. Code searches the stored page for that exact quote and records its position. A claim whose quote is not there is refused and never stored.
4. **A separate verdict.** The verifier reads the claim against the page and returns a verdict: supported, partial, contradicted, conflicting or insufficient. Only a supported claim that is in scope can carry a decision.

The panel reads the draft comparison, not the evidence, so it cannot introduce a new fact.

Read: `app/core/extraction.py` (the exact-quote rule and the exclusion of a flagged page), `app/core/evidence.py` and `app/core/coverage.py` (verdicts and what counts as support). Run: the worked example, parts 2, 3 and 7.

## 4. The gates, and the three states of a recommendation

A verified quote records what the cited source said. Whether an option may be recommended is a second question, and code answers it with a gate on each of the founder's must-haves.

| What the evidence shows for a must-have | What the gate does |
|---|---|
| Verified claims support it | The option passes on this must-have |
| No evidence either way | The option may still be recommended, marked "recommended, not yet proven" on this must-have, with the check that would settle it |
| A verified claim contradicts it | The option is rejected, with the claim quoted |
| Sources contradict each other on it | The decision that depends on it stays open, with the conflict stated; the run continues |

A decision therefore ends in one of three states: recommended and proven on every must-have; recommended and not yet proven on the must-haves named; or open, with the reason and the settling check. A quote can support a price while a language requirement stays without evidence. The recommendation is then conditional, and says so.

An option is never rejected for silence. It is rejected only on a proven violation.

Read: `app/core/coverage_step.py` and `app/core/reconcile.py` (the states and the gate), `app/core/tree.py` (pruning inside the comparison). Run: the worked example, parts 5, 8 and 9; `tests/test_tree_prune.py`.

## 5. The budget arithmetic and the spending ledger

Two budgets are involved. Code handles both.

**The founder's budget for the product.** No vendor page states that a service fits a particular founder's budget, so no model is asked. Code multiplies a sourced unit price (a verified claim) by the founder's own usage and compares the result with the budget. In the worked example, 30 hours a month at 0.60 USD an hour is 18.00 USD against a budget of 50 USD: within it. At 300 hours the same price gives 180.00 USD: the calculation contradicts the must-have and the option is rejected.

**The cost of the run itself.** Before a call to a model or to the search provider is sent, its worst-case cost is reserved in a ledger. A call that would pass the run's limit is not sent. After the call, the reservation is settled to the reported cost. A hosted research run is limited to 30 USD, 480 active minutes and 1,000 model calls; each stage has its allowed tools and at most eight tool calls. These are ceilings, not targets.

If a call is interrupted and its outcome is unknown, the provider may or may not have charged for it. The call is never sent again automatically. Its possible charge stays counted, and the founder decides: retry, continue without it, or replace the possible charge by the provider's own figure. By design, when a run resumes, recorded results are reused: a completed step is replayed from the store and is not sent again.

Read: `app/core/budget.py` and `app/core/derived.py` (the cost calculation), `app/core/ledger.py` (reservations and limits), `app/runner/recovery.py` (interruption and resume), `config/settings.hosted.toml` (the limits). Run: the worked example, parts 4, 6 and 8; `tests/test_audit.py`, `tests/test_workflow.py`.

## 6. Where the founder decides

The founder is asked only what the founder alone can answer or authorize.

| Point | What the founder decides | Does the run wait? |
|---|---|---|
| Requirements | Whether a stated requirement is right, and whether it is a must-have | Yes |
| Expected usage | The figures the cost calculation needs | Yes |
| An interrupted call | Retry, continue without it, or settle its charge by the provider's figure | Yes |
| A changed requirement | Whether to re-open the decisions it binds, before money is spent on that | Yes |
| A page with instruction-like text | Include it or exclude it | No, the run goes on without the page |
| A plan version | Approval. Nothing is produced from a version before it | The run has ended |
| A version drafted after a limit stopped the research | An explicit acceptance of the draft | The run has ended |
| The delivered documents | Accept or reject | The run has ended |

Two things do not reach the founder. A contradiction between sources does not pause the run: the founder usually cannot know which web page is right, so the decision stays open with the conflict stated. A model's stated confidence triggers nothing: every intervention fires on a recorded, observable signal.

Every stop is recorded with its reason.

Read: `app/core/interventions.py` (the same points as nine criteria, `CRITERIA`, each with its condition and its enforcement), `app/core/safety.py` (the signals), `app/runner/recovery.py`. The code of the approval and of the documents' acceptance belongs to the parts that are not in this repository.

## 7. Plan versions, recalculation and delivery

**A plan version is a snapshot with a signature.** Every version is stored whole with its requirements, decisions, claims and sources, validated inside the transaction that creates it, and signed with the SHA-256 of its canonical form. A stored signature that does not recompute is an error. A document is rendered from the snapshot alone, so it cannot show a value the version does not hold.

**A change re-decides only what depends on it.** When the founder changes a requirement or corrects the usage figures, code computes which decisions the change makes stale. A revision run decides those again on the evidence already verified, under its own limit of 5 USD on the hosted service, and stores a new version with the change's summary. The three recalculations of the validation journey cost 1.61, 1.61 and 1.58 USD (17, 18 and 19 November 2026).

**Delivery has three stages.** A stored plan version; the version the founder approved; and two documents produced from it, an MVP plan and a product requirements document. The PDF is the main document. Code checks each document before release: no overflow, no heading alone at a page foot, the same values in every format. A document that fails a check is not delivered. Between 15 and 18 November 2026 these checks refused four productions before release.

**The layout is fixed, and a model contributes the cover only.** The renderer has a mode in which a model designs a document's whole layout, behind a review gate; the example settings keep it (`[studio].mode = "layout"`). No layout a model designed passed that gate. By a change approved on 19 November 2026 and carried by this release, the hosted service prints every document in the standard layout, and a model contributes the cover's concept only: a name and one illustration, by one design request and one illustration request (`[studio].mode = "cover"` and `studio_cap_usd = 2.56` in `config/settings.hosted.toml`). A production is delivered with the standard cover whenever either request is refused or fails a check, so a document never waits on a model's design.

**A corrected rule reaches the next version, not a stored one.** Every version records the generation of the document rules it was written under and is always composed under that generation, so an approved version prints as it was approved. Two cases show it. Version 3 of the demonstration project prints "Every decision of this version is settled" beside four conditional recommendations; the rule was corrected later and the approved version keeps its sentence. Version 4 priced its hosting pick at a plan of 4 USD a month where its own words name the plan of 24 USD; the final release reads the words that name a pick and prices the plan they identify among the option's sourced monthly prices (`identified_rows` in `app/core/budget.py`), and version 4 keeps its stored figures.

Read: `app/core/snapshot.py` (the snapshot and its validation), `app/core/invalidation.py` (what a change makes stale), `app/core/docrules.py` (the generations of the document rules and what each changed). The document renderer and its checks are not in this repository; two documents it produced are under `samples/`.

## 8. What was measured, and what changed because of it

The design changed when a measurement or a case showed a problem. Each figure below is in `evaluation/RESULTS.md` with its date, its version and its label.

- **From one agent to roles under a coordinator in code.** The first design was one coordinating agent. It became separate roles so that each role's output could be checked against a typed contract. The default model and its fallback were chosen on 180 scored outputs (7 October 2026).
- **Retrieval: keyword ranking stayed.** Passages of stored pages are ranked by keyword search. Over 70 queries, keyword ranking found 13 of 314 reference passages among the first eight returned, embeddings 12, a hybrid 8 (29 October 2026). These are counts, and none of the three placed most reference passages among the first eight. The finding that mattered more: on two test projects, 134 of 177 missing reference items were on pages that had never been collected, which no ranking can recover (5 November 2026). Keyword ranking stayed, and wider page collection is a next step.
- **The comparison tree became optional.** A bounded tree was added to the comparison: a few alternatives, a few variants, one evaluation, at most seven calls. On eleven decisions, the simple comparison and the tree each decided three; the simple comparison was right on two and the tree on one, and the tree cost about 13 percent more (3 November 2026). The tree is off by default. A rerun paused in 25 of 33 cases (5 November 2026), which limits any general conclusion.
- **Three changes after a run that decided nothing.** On 13 November 2026 a hosted run made 0 of 4 decisions. Three changes followed, deployed together on 14 November. A contradiction between sources no longer pauses the run: three price tiers of one service had been read as a contradiction and put to the founder as a question the founder could not answer. A must-have budget is judged by arithmetic. A must-have without evidence gives a conditional recommendation with its confirming check, where before it left the decision open. The next run made 4 of 4 decisions. The three changes were tested together, so the improvement cannot be attributed to any one.

What the measurements do not show is stated in the README, section 9.
