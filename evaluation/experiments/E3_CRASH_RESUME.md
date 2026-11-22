# E3: crash and resume (22 to 23 October 2026)

The promise under test: a run survives a crash, saved results never run again, an interrupted
call is never resent automatically and keeps counting as a possible charge until the founder decides, and the founder's three choices (retry,
continue without, reconcile) each lead to an honest end. The harness is scripts/e3_crash_resume.py; the product paths it drives are the Runner,
recover_on_startup, resolve_call and the workflow's replay.

## Method

- The store: the experiment's own database named with --db (data/e3/stub/app.db for the dry run, data/e3/live/app.db for the paid series, data/e3/stop/app.db
  for the Stop verification); the runner keeps checkpoints.db, sources/ and exports/ beside it (app/repo.py, app/adapters/research.data_root), so the live store
  data/app.db is never opened. The settings: config/settings.toml, byte-identical to config/settings.example.toml (the product's default budget: cap 2.50 USD,
  completion reserve 0.45 USD, 100 model calls, 15 active minutes, 30 searches, 50 pages).
- One run = one project (e3pNN) on fixture I01, driven by a fresh child process per drive (the child is the script itself with the word child; it takes the
  store lock as every runner entry does). The parent answers the clarify questions from the fixture as p6-R2 was answered (hardness yes for the keys naming
  the languages or the budget, every confirmation yes, values from the idea), reads the store on a read-only connection between drives, and applies the
  founder's choice through the product's resolve entry (Runner.resolve, the same path as the resolve command of app/runner/cli.py).
- The kills (SIGKILL from inside the child, no cleanup, as a crash): kind sent, after the provider's reply to the n-th model send of the drive arrived and
  before the ledger settles it (the row stays sent; the next process's recover_on_startup marks it unknown: the case of "a provider can finish and charge
  a call while the server crashes before saving the result"); kind settled, after the ledger settled the n-th completed row of the drive and before the step
  record (the stored result replays; nothing is resent); kind node, after the n-th checkpoint of the drive was written (a node boundary). Every run takes one
  sent kill (schedule A: the send index rotates through the stages) and one settled or node kill (alternating by run, schedule B), then runs to its end; a
  second kill the run finishes before is recorded as not reached.
- The choices rotate by run: retry (the attempt stays unknown with resolution retried and its possible charge counted; the next drive sends attempt 2),
  continue without (resolution skipped; nothing is resent for that step; the stage proceeds without the call's output: a verification composition settles
  flagged and the protocol composes the pending claims into a new group, an extraction unit is left for the next run, a single-contract stage drafts the plan
  from the evidence gathered as an explicit partial result), reconcile (the provider's figure replaces the possible charge: the founder's figure when given,
  the provider's figure for the generation id when the row carries one, zero when the reply never reached the server, said so in the summary; the step is sent
  again as a new attempt).
- Assertions per run, from the ledger and the records alone: no completed step repeats (a step with a stored result before a kill gets no new attempt after
  it; at most one stored result per step; a completed step mark is never rewritten, the extraction stage's merged plan and report marks excepted); no duplicate
  charge (one reported cost per provider generation id on the live provider; the step rule above); the unknown calls of a sent kill are listed in the pause
  event with their possible charges; a valid final plan (a plan version whose stored snapshot validates and recomputes its signature, with its Markdown file),
  or the recorded case where the founder's continue fell on the plan's own send (no plan can exist by that choice).
- The standing rule: two runs in a row failing for a cause the experiment does not exercise stop the series; a kill the script makes is not a failure, and an
  unknown call the choices handle is the experiment working.

## The dry run (the stub provider, NMB_TEST_FIXTURES=1; 22 October 2026, 21:4x UTC; data/e3/stub)

`NMB_TEST_FIXTURES=1 PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/e3_crash_resume.py --runs 20 --provider stub --db data/e3/stub/app.db`

The pipeline fake of tests/pipeline_fake.py (the stub model on 127.0.0.1 scripted per contract, the simulated research adapter; shared with
tests/test_milestone1.py): about eleven model sends per run after clarify. Nothing sent, nothing costs (the stub's reported costs are the fake's 0.001 per send).

Summary: 20 runs attempted, 20 crashed (every sent kill reached), 19 with the four assertions holding and run 8 the skipped-plan case (its continue fell on the plan's own send: no plan version, so its valid-plan assertion is false and it is reported apart; the first record counted it as valid); kills: 20 sent, 6 settled, 10 node (16 runs took both of their kills; the others completed before the second); choices: 7 retry, 7 continue, 6 reconcile on 20 unknown calls, every one listed in its pause event with its possible charge (20 runs); outcomes: {'completed:complete': 19, 'partial:plan_skipped': 1}; plan versions: 19; possible charges left counted 0.144756 USD (the stub's reservations of the retried and skipped attempts), reported 0.178 USD (the fake's figures). Result file: docs/experiments/results/e3_stub_2026-10-22_2141.json.

| Run | Project | Choice | Kills | Unknown calls (stage) | Possible charge left (USD) | Outcome | Plan version | Assertions: no repeat / no duplicate charge / unknown listed / valid plan |
|---|---|---|---|---|---|---|---|---|
| 1 | e3p01 | retry | sent:2, settled:2 | 1 (research_plan) | 0.008718 | completed (complete) | 1 | yes / yes / yes / yes |
| 2 | e3p02 | continue | sent:3, node:3 | 1 (research_plan) | 0.010896 | completed (complete) | 1 | yes / yes / yes / yes |
| 3 | e3p03 | reconcile | sent:4, settled:4 | 1 (research_plan) | 0.000000 | completed (complete) | 1 | yes / yes / yes / yes |
| 4 | e3p04 | retry | sent:5, node:1 | 1 (extract_evidence) | 0.011530 | completed (complete) | 1 | yes / yes / yes / yes |
| 5 | e3p05 | continue | sent:6, settled:2 | 1 (verify) | 0.009186 | completed (complete) | 1 | yes / yes / yes / yes |
| 6 | e3p06 | reconcile | sent:7, node:3 | 1 (verify) | 0.000000 | completed (complete) | 1 | yes / yes / yes / yes |
| 7 | e3p07 | retry | sent:8 | 1 (compare_options) | 0.008784 | completed (complete) | 1 | yes / yes / yes / yes |
| 8 | e3p08 | continue | sent:9, node:1 | 1 (plan_generate) | 0.010487 | partial (plan_skipped) | none | yes / yes / yes / no (plan skipped by the choice: no plan version; reported apart) |
| 9 | e3p09 | reconcile | sent:2, settled:2 | 1 (research_plan) | 0.000000 | completed (complete) | 1 | yes / yes / yes / yes |
| 10 | e3p10 | retry | sent:3, node:3 | 1 (research_plan) | 0.010896 | completed (complete) | 1 | yes / yes / yes / yes |
| 11 | e3p11 | continue | sent:4 | 1 (research_plan) | 0.011495 | completed (complete) | 1 | yes / yes / yes / yes |
| 12 | e3p12 | reconcile | sent:5, node:1 | 1 (extract_evidence) | 0.000000 | completed (complete) | 1 | yes / yes / yes / yes |
| 13 | e3p13 | retry | sent:6, settled:2 | 1 (verify) | 0.009186 | completed (complete) | 1 | yes / yes / yes / yes |
| 14 | e3p14 | continue | sent:7, node:3 | 1 (verify) | 0.011348 | completed (complete) | 1 | yes / yes / yes / yes |
| 15 | e3p15 | reconcile | sent:8 | 1 (compare_options) | 0.000000 | completed (complete) | 1 | yes / yes / yes / yes |
| 16 | e3p16 | retry | sent:9, node:1 | 1 (plan_generate) | 0.010487 | completed (complete) | 1 | yes / yes / yes / yes |
| 17 | e3p17 | continue | sent:2 | 1 (research_plan) | 0.008718 | completed (complete) | 1 | yes / yes / yes / yes |
| 18 | e3p18 | reconcile | sent:3, node:3 | 1 (research_plan) | 0.000000 | completed (complete) | 1 | yes / yes / yes / yes |
| 19 | e3p19 | retry | sent:4, settled:4 | 1 (research_plan) | 0.011495 | completed (complete) | 1 | yes / yes / yes / yes |
| 20 | e3p20 | continue | sent:5, node:1 | 1 (extract_evidence) | 0.011530 | completed (complete) | 1 | yes / yes / yes / yes |

Three things the dry run found and the build fixed before any paid call: the verify protocol's state
read an unknown attempt the founder had retried as still in flight after its retry completed (an endless replay loop; the resolved attempt now reads as settled money owing a fresh
first send); a run whose unknown call the founder resolved still closed partial because the close counted every unknown row as open (the close now counts
unresolved calls only); the fake's plan script named records of project P1 whatever the project (made project-agnostic). One product limitation noticed, not
changed: the snapshot's reference pattern (app/core/snapshot.py REF_PATTERN) reads "p01.REQ-003" inside a hyphenated project id such as e3-p01, which the
Project model allows, and refuses the version as citing another project; the experiment's project ids carry no hyphen.

## The Stop verified live (23 October 2026, 06:50:45 to 06:51:33 UTC; data/e3/stop; the default budget, cap 2.50 USD)

`( set -a; source config/.env; set +a; .venv/bin/python -u scripts/e3_crash_resume.py stop-check --provider live --db data/e3/stop/app.db --confirm-paid --stop-after-sends 3 )`
(the key loaded by the shell in a subshell for this one command, never printed). The Stop endpoint of app/web/runs.py served by uvicorn in the script's
process on 127.0.0.1:8802; the owner's session seeded in the store (a users row for founder and a sessions row with its CSRF token); the POST carried the
cookie and the X-CSRF-Token header. The run e3stop-R1 on project e3stop (fixture I01): the clarify round paused with 8 questions, answered from the
fixture; the research drive ran in a child process holding the store lock; the stop landed at 2026-10-23T06:51:24.434Z while the second research send was in
flight (3 research rows at that moment, one of them sent).

- The endpoint's answer: HTTP 200, stopped True, 4 completed call(s) kept, in flight marked unknown: ['e3stop-R1:research_plan:w0:b0:model:2'], possible charge 0.034266 USD.
- No new work: 0 dispatch claimed after the stop (every later claim is refused inside its transaction); the runner's drive ended on its own with the stored outcome: status cancelled, stop_reason founder_stop, no plan.
- Completed results kept: 5 completed calls ({'model:completed': 4, 'search:completed': 1}), 3 recorded steps, 5 sources and 0 claims on the project.
- The pause event (reason founder_stop): "Run stopped (founder_stop): no new work starts; 4 completed call(s) and their results are kept; 1 call(s) in flight marked unknown (e3stop-R1:research_plan:w0:b0:model:2), possibly charged up to $0.03."; it names the unknown call with its possible charge of 0.034266 USD.
- The call in flight afterwards: its reply arrived after the stop and the gateway's settlement completed the row at the provider's figure (0.00499425 USD), so the ledger holds the truth and the pause event the figure of the moment.
- The founder's choice (continue without) applied to every unknown call of the stop: 0 call(s) left unresolved at that time (the reply had settled the row, so the continue was vacuous: no still-unknown live Stop call remained to resolve; the non-vacuous resolution after a Stop is proved offline in tests/test_recovery.py T27), the safe end: run ('cancelled', 'founder_stop'), billed 0.017577 USD, possible 0 USD, unresolved unknown calls [].
- Cost of the verification: 0.017577 USD billed, 0 possible. Result file: docs/experiments/results/e3_stop_live_2026-10-23_0651.json.

## The live series (23 October 2026, 06:50:45 to 11:06:42 UTC; the live provider; data/e3/live; the default budget per run)

Printed before the first paid call: standing cap 150.00 USD; cumulative spend 11.508322 USD billed plus 0.714042 USD possible; E3 cap 30.00 USD; the estimate
for 20 runs 0.70 x 20 x 1.33 = 18.62 USD (p6-R2's 0.642996 plus a resume's share), within the cap, so the series kept its 20 runs. The commands (the key loaded
by the shell in a subshell for each, never printed):

`( set -a; source config/.env; set +a; .venv/bin/python -u scripts/e3_crash_resume.py --runs 20 --provider live --db data/e3/live/app.db --confirm-paid --spent-before 11.508322 --possible-before 0.714042 --standing-cap 150 --e3-cap 30 )`
for runs 1 and 2, then (see the operator's restart below)
`( set -a; source config/.env; set +a; export NMB_DATA_DIR=data/e3/live; .venv/bin/python -u scripts/e3_crash_resume.py --runs 18 --start-index 3 --provider live --db data/e3/live/app.db --settings config/settings.toml --confirm-paid --spent-before 12.628879 --possible-before 0.814360 --standing-cap 150 --e3-cap 28.796702 )`
for runs 3 to 20. config/settings.toml stayed byte-identical to the example before and after. The start time of the paid part: the author's rule was no new paid run after
06:00 UTC; the offline part's green suite came at 06:34 UTC and the CI at 06:50 UTC, and the author's instruction of
06:17 UTC was to run the paid part under the same approvals and say so; the first paid call was at 06:50:45 UTC.

The operator's restart after run 2 (not a product failure): the author's env file exports NMB_DATA_DIR=data beside the keys, so the two first runs' source files and
Markdown went to data/sources/e3p01, data/sources/e3p02 and data/exports/e3p01, data/exports/e3p02 (project-named folders beside the live project's; the live
store data/app.db itself was never opened by the series: 7 runs, 649 ledger rows and the two unresolved calls of earlier runs before and after), and the script's
Markdown check looked beside the store, so it marked runs 1 and 2 "plan not valid" and the series stopped itself by the standing rule. Both plan versions
validate and their Markdown files exist under the recorded export paths (re-evaluated below); the script now reads the version's recorded export path, and the
relaunch set the data directory to the E3 store. Runs 1 and 2 stand as full data points.

The results (result files docs/experiments/results/e3_live_2026-10-23_0650.json and e3_live_2026-10-23_0708.json; the row per run from them, runs 1 and 2 with the
corrected plan check):

| Run | Project | Choice | Kills taken | Unknown calls (stage): the choice, the possible charge | Model / search / contents calls completed | Active s | Outcome | Claims / decisions in the plan | Billed (USD) | Possible left (USD) | Every assertion |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | e3p01 | retry | sent:3, settled:3 | research_plan: retry to retried, 0.034212 USD | 66 / 5 / 2 | 365 | completed (complete) | 52 / 3 | 0.397524 | 0.034212 | yes |
| 2 | e3p02 | continue | sent:8 | extract_evidence: continue to skipped, 0.032387 USD; compare_options (natural): continue to skipped, 0.033719 USD | 88 / 8 / 1 | 684 | completed (complete) | 157 / 4 | 0.705456 | 0.066106 | yes |
| 3 | e3p03 | reconcile | sent:15, settled:10 | verify: reconcile to reconciled, 0.025728 USD, figure 0.0 | 90 / 7 / 1 | 893 | completed (complete) | 149 / 4 | 0.647458 | 0.000000 | yes |
| 4 | e3p04 | retry | sent:25, node:4 | verify: retry to retried, 0.028004 USD | 61 / 8 / 4 | 811 | completed (complete) | 105 / 4 | 0.566337 | 0.028004 | yes |
| 5 | e3p05 | continue | sent:40, settled:8 | extract_evidence: continue to skipped, 0.034616 USD | 63 / 8 / 4 | 721 | completed (complete) | 64 / 4 | 0.418301 | 0.034616 | yes |
| 6 | e3p06 | reconcile | sent:55, node:3 | extract_evidence: reconcile to reconciled, 0.031964 USD, figure 0.0; extract_evidence (natural): reconcile to reconciled, 0.034459 USD, figure 0.0 | 88 / 7 / 2 | 810 | completed (complete) | 133 / 3 | 0.658149 | 0.000000 | yes |
| 7 | e3p07 | retry | not reached | none | 62 / 8 / 2 | 730 | completed (complete) | 87 / 4 | 0.494390 | 0.000000 | yes |
| 8 | e3p08 | continue | sent:5 | verify: continue to skipped, 0.027629 USD | 59 / 8 / 1 | 799 | completed (complete) | 121 / 4 | 0.516528 | 0.027629 | yes |
| 9 | e3p09 | reconcile | sent:12, settled:4 | extract_evidence: reconcile to reconciled, 0.034950 USD, figure 0.0 | 81 / 8 / 2 | 840 | completed (complete) | 145 / 3 | 0.684332 | 0.000000 | yes |
| 10 | e3p10 | retry | sent:20 | extract_evidence: retry to retried, 0.034948 USD | 70 / 8 / 2 | 858 | completed (complete) | 123 / 4 | 0.542993 | 0.034948 | yes |
| 11 | e3p11 | continue | sent:30, settled:3 | extract_evidence: continue to skipped, 0.034911 USD | 81 / 8 / 3 | 925 | partial (time_limit) | 154 / 2 | 0.705063 | 0.034911 | yes |
| 12 | e3p12 | reconcile | sent:48 | extract_evidence: reconcile to reconciled, 0.034799 USD, figure 0.0 | 88 / 6 / 0 | 839 | completed (complete) | 148 / 3 | 0.667995 | 0.000000 | yes |
| 13 | e3p13 | retry | sent:62, settled:10 | extract_evidence: retry to retried, 0.034969 USD | 83 / 8 / 3 | 874 | completed (complete) | 152 / 4 | 0.717697 | 0.034969 | yes |
| 14 | e3p14 | continue | sent:80, node:4 | verify: continue to skipped, 0.026549 USD | 90 / 8 / 2 | 908 | completed (complete) | 145 / 4 | 0.700820 | 0.026549 | yes |
| 15 | e3p15 | reconcile | sent:10, settled:8 | extract_evidence: reconcile to reconciled, 0.032521 USD, figure 0.0 | 44 / 8 / 3 | 611 | completed (complete) | 63 / 4 | 0.396484 | 0.000000 | yes |
| 16 | e3p16 | retry | sent:18, node:3 | extract_evidence: retry to retried, 0.030540 USD | 89 / 5 / 3 | 844 | completed (complete) | 140 / 4 | 0.620037 | 0.030540 | yes |
| 17 | e3p17 | continue | sent:35, settled:6 | extract_evidence: continue to skipped, 0.034992 USD | 90 / 7 / 6 | 875 | completed (complete) | 113 / 4 | 0.645818 | 0.034992 | yes |
| 18 | e3p18 | reconcile | sent:45 | extract_evidence: reconcile to reconciled, 0.031300 USD, figure 0.0 | 77 / 7 / 4 | 879 | completed (complete) | 102 / 3 | 0.625449 | 0.000000 | yes |
| 19 | e3p19 | retry | not reached | none | 38 / 8 / 0 | 436 | completed (complete) | 58 / 4 | 0.342568 | 0.000000 | yes |
| 20 | e3p20 | continue | not reached | none | 36 / 8 / 1 | 433 | completed (complete) | 43 / 4 | 0.319826 | 0.000000 | yes |

Summary: 20 runs, of which 17 reached their scheduled crash: those 17 are the recovery trials, every one with the four assertions holding (no completed step repeats, no
duplicate charge, the unknown calls listed with their possible charges, a valid final plan); runs 7, 19 and 20 made 62, 38 and 36 model sends and never reached their
scheduled sent kill at 70, 58 and 75, so they are complete runs without a crash and no recovery evidence (the first record counted them);
outcomes 19 completed and 1 partial at the 15-minute active-time limit with its permitted draft (run 11); plan versions 20 (43 to 157 claims; 2 to 4 decisions:
unresolved on the one hard requirement as in p6 in 18 runs, while e3p18 holds one active and two unresolved decisions and e3p20 two active and two unresolved);
kills 17 sent, 8 settled, 4 node (12 runs took both of their kills; 5 runs completed before the second kill's index; the live schedule was sized on p6-R2's
91 sends, and the E3 runs made 36 to 91 model sends including the unknown attempts, 36 to 90 completed); choices 5 retry, 7 continue, 7 reconcile on 19 unknown
calls, two of them natural (run 2's comparison send and run 6's extraction send ended unknown on the provider's side without a kill, and the pause and the
choice handled them like the killed ones); costs 11.373225 USD billed (mean 0.568661, the dearest 0.717697, every run under its 2.50 USD cap) plus 0.387476
USD left counted as possible charges (the 5 retried and 7 skipped attempts at their reservations; the 7 reconciled attempts at the founder's figure 0, since no
reply reached the server and the provider has no generation id to ask for); the E3 store's own ledger agrees (11.373226 billed, 0.387476 possible); mean active
time 757 s. The Stop verification added 0.017577 USD. Cumulative spend of the 150 USD standing cap after everything: 22.899124 USD billed plus 1.101518 USD
possible. The standing rule never fired on a real cause.

## The series completed to 20 crashed runs (23 October 2026, 14:57:40 to 15:41 UTC; runs 21 to 23; the corrected harness)

An audit of the first twenty live runs and the dry run found that three of the first twenty live runs (7, 19 and 20) completed before their scheduled sent kill and were counted as
recovery successes, and that the stub run 8's missing plan was counted as a valid final plan. The harness was corrected:
a run counts as a recovery trial only when its scheduled crash was reached, every sent kill is scheduled at index 30 or below (the live minimum was 36 sends), valid_final_plan
equals the validator's result, the skipped-plan case is reported apart, the series summary reports the crashed runs, the both-kill runs and the four assertions per run; and
the harness passes the store's own directory as the data directory, so every file of a run stays under data/e3 whatever NMB_DATA_DIR says (the files of runs 1, 2 and the
Stop were moved there: data/e3/live/sources/e3p01 (26 files), e3p02 (57), data/e3/live/exports/e3p01 and e3p02 (1 each), data/e3/stop/sources/e3stop (5); the two
export_path rows of runs 1 and 2 keep the path they recorded, so their Markdown is found at the moved path, not at the recorded one). Under the author's approval
(up to 5 more runs under the default budget each and 5 USD together) the series was continued until the crashed runs counted 20:

`( set -a; source config/.env; set +a; .venv/bin/python -u scripts/e3_crash_resume.py --runs 3 --start-index 21 --provider live --db data/e3/live/app.db --settings config/settings.toml --confirm-paid --spent-before 22.899124 --possible-before 1.101518 --standing-cap 150 --e3-cap 5 --estimate-per-run 0.568661 )`

Printed before the first paid call: standing cap 150.00 USD; cumulative spend 22.899124 USD billed plus 1.101518 USD possible; E3 cap 5.00 USD; the estimate for 3 runs
0.568661 x 3 x 1.33 = 2.269 USD (the first series' mean cost per run). CI run 39971836098 (the fix commit fa62a4a) was green before the first call; config/settings.toml
byte-identical to the example before and after.

| Run | Project | Choice | Kills taken | Unknown calls (stage): the choice, the possible charge | Model / search / contents calls completed | Active s | Outcome | Claims / decisions in the plan | Billed (USD) | Possible left (USD) | The four assertions |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 21 | e3p21 | reconcile | sent:3, settled:3 | research_plan: reconcile to reconciled, 0.034367 USD, figure 0.0 (no generation id) | 81 / 8 / 3 | 907 | completed (complete) | 135 / 4 | 0.609157 | 0.000000 | yes / yes / yes / yes |
| 22 | e3p22 | retry | sent:8 (node:6 not reached) | verify: retry to retried, 0.025126 USD | 73 / 5 / 2 | 836 | completed (complete) | 115 / 3 | 0.548136 | 0.025126 | yes / yes / yes / yes |
| 23 | e3p23 | continue | sent:15, settled:10 | verify: continue to skipped, 0.027277 USD | 85 / 7 / 4 | 863 | completed (complete) | 125 / 3 | 0.593033 | 0.027277 | yes / yes / yes / yes |

The three runs: runs_attempted 3, crashed_runs 3, runs_ok 3, both_kills 2, kills 3 sent, 2 settled, 0 node; choices 1 retry, 1 continue, 1 reconcile on 3 unknown calls, each
listed in its pause event with its possible charge; outcomes 3 completed:complete; 1.750326 USD billed plus 0.052403 USD possible; the standing rule did not fire. Every decision
of the three plans is unresolved on the one hard requirement (4, 3 and 3). Result file: docs/experiments/results/e3_live_2026-10-23_1457.json.

The series over all 23 live runs (the summary recomputed with the corrected harness's summarise over the three result files): crashed_runs 20 (runs 7, 19 and 20 not crashed),
both_kills 14, kills 20 sent, 10 settled, 4 node; choices 6 retry, 8 continue, 8 reconcile on 22 unknown calls (2 of them the provider's own); the four assertions over the 20
crashed runs: no completed step repeats 20, no duplicate charge 20, unknown calls listed 20, valid final plan 20 (runs 1 and 2 re-evaluated on a backup copy of the store: their
snapshots validate and recompute their signatures, and their Markdown files exist at the moved path; their retained result records still say false from the first launch's
lookup beside the store); outcomes 22 completed and 1 partial at the time limit (run 11); 23 plan versions; 13.123551 USD billed (the store's ledger 13.123552; 1,921 ledger
rows: model 1,683 completed and 14 unknown, searches 168, contents 55) plus 0.439879 USD possible (6 retried and 8 skipped attempts at their reservations; 8 reconciled at the
founder's 0); mean 0.570589 USD and 771 active seconds per run; 36 to 91 model sends including the unknown attempts; 43 to 157 claims. The Stop verification stays 0.017577
USD. Cumulative spend of the 150 USD standing cap after the close: 24.649450 USD billed plus 1.153921 USD possible.

What the series proves: a run killed after the provider's reply and before the ledger settles it resumes in a fresh process with the call unknown and listed,
nothing resent until the founder decides, and each choice ends honestly (retry: attempt 2 sent and attempt 1 counted; continue: the stage without that call,
the plan still produced; reconcile: the figure replacing the charge and the step sent anew); a run killed after a settlement or at a node boundary replays
every completed step and sends nothing twice (the per-step assertion over the ledger); the live provider's own interruptions take the same path.
