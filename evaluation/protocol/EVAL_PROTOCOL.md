# The evaluation protocol

Written 2 November 2026 on a development branch, from the evaluation design of the project's plan. Frozen before the first run: the SHA-256 of this file at the first protocol run is recorded in that run's report, and a change to this file after that starts a new evaluation set. Every path is relative to the repository unless it begins with a slash.

Versions of this file (a change after the first protocol run starts a new evaluation set; no run has been made under any version yet):

- Version 1, 2 November 2026: the first text (commit 3a8a224).
- Version 2, 2 November 2026: the fixed-data gate in step 6 names the evaluated project (`PROJECT=$PID`; the Makefile passes it as `--project`, default p6) and the scratch folder is created before the copy; reason: the command as written gated p6 on a multi-project backup and failed on a devA-only store.
- Version 3, 2 November 2026: step 3 loads the decisions (`start --decisions $P/decisions.md`; section 3's decisions.md carries the fenced json block the command reads); reason: the decisions file was hashed but never loaded into the Runner.
- Version 4, 2 November 2026: section 10 states that a pre-written confirmation answer is the founder's own statement for the key or "ask the founder" (the founder_answers files' version 2); reason: a wildcard confirmation turned an invented model-attributed statement into a founder-confirmed input.
- Version 5, 2 November 2026: step 4's status read runs after each drive and never while a drive is in progress (one rule; the step comment said every 5 minutes while a drive runs, the closing paragraph never); section 8 defines the identity measure exactly (the matched gold candidate, `open`, `missing`, 1 when the same in all three repetitions); reason: a review finding.
- Version 6, 2 November 2026: section 5's cost row names the send-time rule for a reported row without a finish time and the possible part beside the cost; reason: the scorecard omitted the unknown reserves from the per-decision cost and counted calls by their finish time.
- Version 7, 2 November 2026: section 5 names the cap stops under which a partial run counts as a first-attempt completion (budget_reserve_reached, time_limit, call_limit); reason: the scorecard counted every partial run and could list another project's run of the same version number.
- Version 8, 2 November 2026: section 8 states that experiment 1's panel send offers the provider no tool (no fusion, search or fetch tool: the fusion tool's analysis models search and fetch inside the provider), so the configuration's panel is the route's one model reading the draft and the packs; reason: the harness enabled provider-side collection through the fusion tool.
- Version 9, 3 November 2026: section 8 states that the panel send without a tool carries its own instruction (panel_direct.v1: the one model reads the draft and the candidates' labels and answers from its own knowledge on the run date), and that the model receives the candidates' labels and the draft, not the packs' claim text (version 8 said the draft and the evidence packs); reason: the no-tool send still carried panel.v2's instruction to call the fusion tool and copy its analysis.

## 1. What is measured

The evaluation measures decisions, not a fit to one vendor's name. The gold is written before any result is seen and does not change afterwards. The aim is a system that meets explicit acceptance criteria with known, reported limitations. A decision is one subject the research run compares (a row of `decisions` of the plan version scored); its outcome is the pick (the memo's recommendation), the state (recommended, open, superseded) and the two cited reasons.

## 2. The four projects

Four projects, 3 to 4 decisions each, about 16 decisions in total. Decisions of one project share a brief, a run and a store, so they are not fully independent observations; every report says so.

| project | kind | files | decisions |
|---|---|---|---|
| A, a personal trip planner | development, public | evaluation/protocol/projects/devA_trip_planner/ (brief.md, hard_requirements.md, decisions.md, founder_answers.md); gold docs/eval/gold/devA_gold.md | A1 itinerary language model; A2 routing and places service; A3 itinerary storage; A4 web hosting |
| B, a Hebrew support assistant for a small shop | development, synthetic | evaluation/protocol/projects/devB_hebrew_support/ (the same four files); gold docs/eval/gold/devB_gold.md | B1 assistant language model; B2 chat widget; B3 email delivery |
| C, transcription and processing for a small podcast | held out, public; one non-English language, Portuguese; storage in Europe | not written with this protocol; written after the decision-logic freeze of 15 November 2026 | 3 to 4, written with its gold |
| D, expense tracking for a family business | held out, synthetic; receipt capture from photos; data does not leave the EU | not written with this protocol; written after the freeze | 3 to 4, written with its gold |

Development versus held-out: the two development projects serve fixes and repetitions during development (they may run as often as the budget allows, each run labelled with its commit and configuration). The held-out projects do not run before the decision-logic freeze (15 November 2026); their gold is written after the freeze and before their single run; then they run once each and the result is reported as is.

## 3. The project files, in the form the Runner accepts

- brief.md: the founder's text after the file's last `---` line is the idea the start command sends (the Start screen's "idea as typed"; 300 to 500 words).
- hard_requirements.md: one fenced json block, a list of `{"key", "value"}` objects, stored by `cli project` exactly as the Start screen stores a constraint chip (app/web/pages.py): `requirements` rows with status confirmed, source user, hard 1, version 1, ids `<project>.REQ-001` onwards, and the Q-000 confirmation row of the idea. The decision subjects a requirement binds (`requirements.applies_to`) are written by the planner's applicability map during the run; the gold states the binding the evaluation expects, and the report states the run's.
- decisions.md: the decisions the evaluation scores, each with its question, candidate pool rule, research questions and criteria, and the expected binding; its fenced json block is what `cli start --decisions` loads into the run: the research planner receives each decision's subject, question, candidate pool rule, research questions and criteria as the decisions to support (the expected binding is the gold's and is never sent); the planner may add subjects. The gold matches a run's subject to a decision by its name or by its subject words.
- founder_answers.md: one fenced json block keyed by interrupt kind (section 10).

## 4. The gold format

One Markdown file per project, headed as a draft until the author's approval line is copied in, then its source-set table (every page with its URL, the date it was read, the "last updated" it shows), then per decision: the hard requirements it binds; the relevant evidence (the pages that decide, each with the exact sentence quoted and the date read); the acceptable alternatives (one or more); the candidates that must be rejected, each with the quoted reason; whether abstention is the right answer and why; the explicit marking of the cases where sufficient evidence exists so that a useful, supported decision is required; the budget calculation with its formula and data. Every number carries its kind (section 12). The file ends with one fenced json block, the machine-readable gold that scripts/eval/scorecard.py reads:

```text
{"project": "<id>", "schema": "eval-gold-1", "written": "<date>", "decisions": [
  {"id": "A1", "subject": "<the expected subject>", "subject_words": ["<phrases that identify the run's subject>"],
   "hard_requirements": ["<keys>"],
   "acceptable": [{"name": "...", "words": ["<every word must appear in the pick>"] or "phrase": "<the words in this order>", "sources": ["S1"]}],
   "rejected":   [{"name": "...", "words" or "phrase", "reason": "<the quoted reason>", "source": "S4"}],
   "unsupported": [...],            # named, neither accepted nor rejected (absence of a mention)
   "sufficient_evidence": true,     # a useful, supported decision is required
   "abstention_right": false,       # the right answer is an open decision with a settling check
   "settling_check": "..."}]}       # when abstention is right: the statement that would settle it
```

Matching rules (scripts/eval/scorecard.py): a run's subject matches a gold decision by its normalised name or by any `subject_words` phrase contained in it; a pick names a candidate when every one of the candidate's `words` is a word of the pick (lower case, letters and digits), or when its `phrase` appears in the pick's words in that order (so "gpt 5 mini" is not "gpt 5 4 mini"). The p6 gold (docs/experiments/gold/I01_reference.json, the document-model shape) is read as a legacy gold: one acceptable candidate per memo by the legacy first-word rule, no rejections, sufficient evidence everywhere; the legacy 27 rows (docs/experiments/scorecard.py, score and gates_v9) are computed beside the protocol's table.

## 5. The metrics, per decision, and how they are computed from the store

The store is a copy of a backup (never the live store; the scorecard refuses live paths as scripts/ops/replay.py does). The version scored is the latest signed plan version of the project (`plan_versions.snapshot_sig IS NOT NULL`) or the one named with `--version`. The memos come from the version's signed snapshot through the document model builder (app/export/document_model.py: `plan_versions.snapshot_json`, the current memos with their recommendation, status, why sentences with claim ids, alternatives, open items; the claims' status and scope). The settling check is the stored memo's `settling_check` (`decisions.memo_json`).

| metric | weight | definition | computed from |
|---|---|---|---|
| hard-requirement violation | non-compensable, always 0 | the pick names a candidate the gold rejects | the memo's recommendation against the gold's `rejected` entries |
| supported recommendation | high | the pick is among the acceptable alternatives and both quoted reasons are correct against the source: the code checks that at least two why sentences cite claims that exist, are supported and in scope (in_scope or conditional); the correctness of each quoted reason against its page is the reviewer's check (section 13) | the memo's `why[].claims` against the snapshot's claims (`status`, `scope`); the gold's `acceptable` |
| conditional recommendation | its own column, counted apart | the decision names a pick that is recommended and not yet proven on a binding hard requirement (the evidence is silent on it; the memo prints the requirement and the check that would confirm it). It is never a supported recommendation and never an abstention: the pick is still tested against the gold's `rejected` entries (a hard-requirement violation stays non-compensable) and against its `acceptable` entries (reported in `acceptable`); the reviewer judges whether the unproven requirement is in fact met | the memo's `conditional` record (the document model builds it from the snapshot's memo); `supported_recommendation` is false for such a row |
| justified abstention | high | the decision is unresolved (no recommendation) when the gold says there is no sufficient evidence in the source set, and the memo carries a settling check; the correctness of the check is the reviewer's | `decisions.memo_json.settling_check`; the gold's `sufficient_evidence` false |
| unjustified abstention | low (the count must be 0 where the gold marks sufficient evidence) | the decision is unresolved when the gold points at sufficient evidence in the source set; also an unresolved decision without a settling check, and a gold decision the run never opened (state missing) | the same; a missing subject |
| time, cost and calls to a useful decision | low | from the ledger: the wall-clock seconds from the run's start (`runs.started_at`) to the decision's storing (`run_events` event `decision` for the decision id, the final one preferred); the cost reported by then (`usage_ledger.reported_usd` of completed and unknown rows with `finished_at` by then, a reported row without a finish time counting by its `sent_at`, plus `reserved_usd` of unknown rows without a figure sent by then, named beside the cost as the possible part); the model rows sent by then (`usage_ledger.kind = 'model'`, `sent_at` by then) | `runs`, `run_events`, `usage_ledger` |
| consistency (the repetitions) | section 8 | correctness across repetitions and identity of the pick, each defined exactly in section 8 | the scorecards of the three repetitions |

Per run: the status and stop reason (`runs.status`, `runs.stop_reason`), the active seconds (`runs.active_seconds`), the whole run's reported cost, possible charges and model calls (`usage_ledger`), and first-attempt completion: the run ended (status completed, or partial under a cap stop, `runs.stop_reason` budget_reserve_reached, time_limit or call_limit, which is a result; any other partial stop is not a completion) with no infrastructure pause (`run_events` event `pause` with reason unknown_call, server_restart, stage_failed or credit_exhausted) and no ledger row in state unknown. A question pause (clarify, usage) is not a failure.

Totals per project: decisions, decided, abstentions, violations, supported recommendations, conditional recommendations (their own count; a decided decision is either supported, conditional or neither), acceptable picks, justified and unjustified abstentions, the cost and calls to the decisions, and `non_compensable_ok` (violations 0 and unjustified abstentions 0). The scorecard's exit code is 0 when the non-compensable criteria hold, 1 when one fails, 2 on an error, 3 when a live store was refused.

## 6. The non-compensable criteria

First, before any score: zero hard-requirement violations; zero unjustified abstentions in the cases the gold marked as having sufficient evidence; every number with a source or a computation basis (section 12); reproducibility (the fixed-data gate `make replay` passes on the copy), budget (the caps held, the ledger complete) and data integrity (the backup's SHA-256 recorded, `PRAGMA integrity_check` ok on the copy). A result that fails one of these is not scored further; it is reported as failed with the criterion named.

## 7. The disqualification rule and justified abstention

One claim suffices to reject a candidate if it is explicit, current, from an authoritative source (the vendor's own page) and refers exactly to the product and the relevant conditions (the plan, the tier, the model, the role in which the vendor processes the data). Absence of a mention is not evidence of absence: a candidate whose page does not state the fact is unsupported, not rejected. Two copies of the same vendor notice are one piece of evidence.

Justified abstention is "no sufficient evidence in the defined source set and date", not a claim that no such information exists anywhere. The defined source set is the gold's table of pages on the date read; a run may read other pages, and the second review judges a claim from outside the set by the same rule and the report says the set was extended.

## 8. The two experiments and the repetitions

Experiment 1, what the tree and the panel add to the decision: the same requirements, candidates and frozen evidence (a copy of a backup after the collection; no external collection, no candidate expansion); only the comparison configuration varies: simple; tree; tree and panel, where the panel send offers the provider no tool (no fusion, search or fetch tool, since the fusion tool's analysis models search and fetch inside the provider: the panel is the route's one model reading the draft and the candidates' labels under its own instruction, panel_direct.v1, and answering from its own knowledge on the run date; the packs' claim text is not supplied to it) and the panel's clarification request is recorded and not executed. Reported per configuration: decision quality against the gold (this scorecard), cost, time and calls.

Experiment 2, what the full process adds to the user: end-to-end runs under this protocol on the same projects and gold (the development runs, the official runs on main). Reported: the decisions and the differences in evidence collection, time and cost. Both compare configurations of one system, never "single agent versus multi-agent".

The repetitions: three repetitions per development project on one tagged version of the code, with fresh model judgement on the same frozen sources (the comparison re-run; the stored verdicts not reused across repetitions: the reuse keys are scoped by run id, and each repetition is its own run id on its own copy of the store, so no run state is shared). Two measures, each per gold decision and then over the project's gold decisions: correctness across repetitions, the fraction of the three scorecards in which the decision is a supported recommendation or a justified abstention (0, 1/3, 2/3 or 1; the project's figure is the mean over its gold decisions); identity of the pick, where a repetition's value for the decision is the name of the first gold candidate the pick names under the matching rules of section 4 (acceptable entries first, then rejected, then unsupported), the pick's normalised recommendation text when it names no gold candidate, the distinct value `open` for an abstention and the distinct value `missing` for a decision the run never opened; identity is 1 when the value is the same in all three repetitions and 0 otherwise (an abstention in one repetition beside a pick in another is 0; three abstentions are 1), and the project's identity is the fraction of its gold decisions with identity 1. Isolated run state per run id: each repetition starts from the same copy of the backup under its own run id and its own ledger.

## 9. The infrastructure-failure policy

A run stopped by an infrastructure failure is recorded with its stop reason. The order of repair: first the existing resume mechanism (`cli resolve`, then `cli resume`, from the checkpoint; completed calls are replayed, never resent); a call whose result is unknown is not resent before it is clarified (the pre-written choice for an unknown model call is `continue`, never `retry`); a 402 is classified: a vendor account problem is fixed and the run resumed, a deliberate budget stop (the cap) is a result, not a failure. A fresh start, once, under the same cap, only when resume is impossible. The time and money of a failed attempt count in the total cost, and the first-attempt completion rate is reported among the metrics (section 5). No repeat for any other reason: a disappointing result is a result.

## 10. The interrupts and the pre-written answers

The Runner can pause for: a hardness question (`questions.kind` hardness: is the requirement hard, yes or no), a confirmation question (confirmation: the model recorded a requirement as the founder's statement, yes, no or the corrected value; the pre-written answer is the founder's own statement for that key or "ask the founder", never a wildcard yes), a value question (value: clarify's blocking questions, at most three), the usage questions (usage: the four figures of app/core/derived.py, the scope question, the summary-length question), the research planner's ask_user tool (a paused tool call), an unknown model call after a crash (`cli resolve`), and the version approval after the run (`cli approve`, the author's act). founder_answers.md answers each kind from its block, keyed by the requirement key, then by a fragment of the question's text (an entry whose name holds a space), then the block's `"*"`; any question not covered, and any entry whose value is the literal `ask the founder`, stops the run item cleanly: `cli answers` prints the question verbatim, exits 4 and applies nothing (the run stays paused for the founder). The version approval is never given by a run item (the author alone approves a version).

## 11. The command sequence (non-interactive)

Executed in the driver worktree, the only tree that opens the live research store; `$PY` is `.venv/bin/python`; the key file is sourced only inside the subshell of a command that sends calls, never read or printed. The caps (money, active minutes, model calls) go on every command of the run: the cap and the call limit bind in the run's `budget_json` at start, the minutes are read from the settings at every drive. `P` is the project folder, `PID` the project id, `RUN` the run id the start command prints (`<PID>-R<n>`).

```sh
cd <repository root>
PY=.venv/bin/python; P=evaluation/protocol/projects/devA_trip_planner; PID=devA
CAPS="--cap-usd 20 --max-minutes 480 --max-calls 1000"
# 0. the protocol and gold frozen: the hashes recorded in the run's report
shasum -a 256 evaluation/protocol/EVAL_PROTOCOL.md docs/eval/gold/${PID}_gold.md $P/brief.md $P/hard_requirements.md $P/decisions.md $P/founder_answers.md
# 1. the backup before, with its SHA-256 (the backup API, never a file copy of the WAL store)
python3 -c "import sqlite3; s=sqlite3.connect('data/app.db'); d=sqlite3.connect('<backup path>'); s.backup(d); d.close(); s.close()" && shasum -a 256 <backup path>
# 2. the project, its brief and hard requirements (nothing sent)
$PY -m app.runner.cli --db data/app.db project --project $PID --brief $P/brief.md --requirements $P/hard_requirements.md
# 3. the run, the caps printed first; the process ends when the run pauses or ends
( set -a; source config/.env; set +a; $PY -m app.runner.cli --db data/app.db $CAPS start --project $PID --brief $P/brief.md --decisions $P/decisions.md --confirm-paid )
# 4. the loop, after each drive (the start or resume process has ended; never while a drive is in progress): the status read (nothing sent; the unknown calls listed), then one of the three branches below as the status says
$PY -m app.runner.cli --db data/app.db $CAPS resolve --run $RUN
#    awaiting_user: the answers from the file; exit 4 means "ask the founder": stop here, record the question, go to step 5
( set -a; source config/.env; set +a; $PY -m app.runner.cli --db data/app.db $CAPS answers --run $RUN --file $P/founder_answers.md --apply )
#    interrupted (unknown calls after a crash): the pre-written choice, never retry, then the resume from the checkpoint
$PY -m app.runner.cli --db data/app.db $CAPS resolve --run $RUN --call <entry id> --choice continue
( set -a; source config/.env; set +a; $PY -m app.runner.cli --db data/app.db $CAPS resume --run $RUN )
#    until the status is completed, partial, failed or cancelled
# 5. the ledger export (ids, states, costs, events; no page text), the backup after with its SHA-256
$PY -m app.runner.cli --db data/app.db export --run $RUN --out <reports>/${RUN}.trace.json
python3 -c "import sqlite3; s=sqlite3.connect('data/app.db'); d=sqlite3.connect('<after backup>'); s.backup(d); d.close(); s.close()" && shasum -a 256 <after backup>
# 6. the scorecard on a verified copy of the after backup under $TMPDIR (never the live store), deleted afterwards
shasum -a 256 <after backup> && mkdir -p $TMPDIR/nmb-${PID} && cp <after backup> $TMPDIR/nmb-${PID}/copy.db
$PY scripts/eval/scorecard.py --db $TMPDIR/nmb-${PID}/copy.db --project $PID --gold docs/eval/gold/${PID}_gold.md --out <reports>/${RUN}.scorecard.json
make replay DB=$TMPDIR/nmb-${PID}/copy.db OUT=<reports>/${RUN}.replay.json PROJECT=$PID    # the fixed-data gate on the copy, for this project
rm -rf $TMPDIR/nmb-${PID}
```

The commands `project`, `start --brief`, `start --decisions`, `answers` and the cap flags were added for this protocol and are tested on fixtures in tests/test_eval_protocol.py; `resolve`, `resume`, `export` and `approve` are the existing commands. `cli resolve --run` without `--call` takes the store lock, runs the startup recovery and prints the run's status and unknown calls: it is the status read between drives, and it is never run while a drive is in progress.

## 12. The four kinds of numbers

Every number in a gold, a report or a plan is one of four kinds, each with its proof: an external fact (the source page and the supporting quote); a user input (the brief, the founder's answer or the confirmed requirement it comes from); a calculation (the formula and the data used); an assumption (marked as such). A number without a checkable source or basis disqualifies the result it appears in.

## 13. The second review

Every rejection and every abstention gets a second review by the reviewer: the cited page is fetched, the quoted sentence confirmed on the page as quoted, and the rejection or abstention confirmed under the disqualification rule. When the reviewer is a model (an audit or a session of a language model), this is a model review in addition to the author's check, not two independent human reviewers, and the report says so.

## 14. The improvement rule

A comparison is always between two code versions on the same projects and the same gold. First the non-compensable criteria (section 6); only then the score: fewer violations and more supported recommendations without a rise in unjustified abstentions. A rise in cost or time without an improvement in one of those is a regression. Development results on a branch are labelled with commit and configuration and are not release performance; official measurements are the protocol runs on main or on the tagged release.

## 15. The data cut-off rule

Every number reported carries the run id, the commit (the code tree's sha), the backup's SHA-256 (the copy the scorecard read) and the date; the scorecard's JSON records the project, version, version date, run date, gold path, database path and code tree, and the report copies them. The gold's dates are the dates its pages were read; a page read later is a different source.

## 16. The reports

A protocol run's report (an internal report for the development runs) carries: the protocol's and the gold's SHA-256, the approval line of the gold, the caps, the interrupts and the answers used, the scorecard table with the totals, cost, time and calls per decision, first-attempt completion, the backups with their SHA-256, the ledger export path, and the deviations as facts. Nothing in a report changes the gold; a gold found wrong is corrected by the author before the next evaluation set, and the correction starts a new set.
