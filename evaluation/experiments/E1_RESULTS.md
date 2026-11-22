# E1 results

Last run on 2026-10-07T16:56:59+00:00; fixtures e1-fixtures-v2 (20 ideas x 3 stages per candidate); OpenRouter, require_parameters true, json_schema strict, one repair allowed, no cross-vendor fallback inside the bake-off (fallback pinned to the candidate itself); ceilings {'clarify': 2400, 'extract_evidence': 900, 'plan_generate': 2000}; cap $0.90 for this run. Cost per typical run = pinned prices at 350,000 input and 35,000 output tokens (the design's assumed size of a typical run).

## Runs

Runs 1 to 3 (7 October) were scored on fixtures v1: run 1 all stages (ceilings 1200/900/2000, $1.8828), run 2 clarify and plan_generate (2400/4000, $1.4876, cap reached before Gemini), run 3 plan_generate for three candidates with the fixed wire schema ($1.1036, 60 of 60 valid); kept as recorded in results/E1_run1_*, E1_run2_*, e1_run1_results.json, e1_run2_results.json and e1_merged_runs1_3_results.json. Run 4 (total cap $2.00, fixtures v2, v2 scorer: scope_out violations only when the plan builds the forbidden thing; contradiction fixtures without local_only): 4a plan_generate for all three candidates, ceiling 4000, $1.0998 (results/e1_run4a_results.json); 4b clarify and extract_evidence for openai/gpt-6.1-sol so its row is complete, ceiling clarify 2400. Clarify and extract_evidence rows of Sonnet and Gemini come from runs 1 and 2; their scoring rules did not change between v1 and v2.

| model | calls | schema valid | first attempt | quote accuracy | decision accuracy | hard violations | injections flagged | latency ms (mean) | measured USD | rows without provider cost | cost per typical run USD |
|---|---|---|---|---|---|---|---|---|---|---|---|
| openai/gpt-6.1-sol | 60 | 1.000 | 1.000 | 1.000 | 0.808 | 3 | 4/4 | 18034 | 0.5753 | 0 | 1.050 |
| anthropic/claude-sonnet-5.5 | 60 | 0.983 | 0.967 | 1.000 | 0.746 | 1 | 4/4 | 12758 | 1.0560 | 0 | 1.050 |
| google/gemini-3.8-flash | 60 | 1.000 | 0.833 | 1.000 | 0.767 | 0 | 4/4 | 12781 | 0.3200 | 0 | 0.394 |

## Per stage (valid answers / calls, mean decision score)

| model | clarify | extract_evidence | plan_generate |
|---|---|---|---|
| openai/gpt-6.1-sol | 20/20, 0.97 | 20/20, 1.00 | 20/20, 0.45 |
| anthropic/claude-sonnet-5.5 | 19/20, 0.89 | 20/20, 1.00 | 20/20, 0.35 |
| google/gemini-3.8-flash | 20/20, 0.93 | 20/20, 1.00 | 20/20, 0.38 |

## Decision (scripts/e1_model_bakeoff.py decide())

`{"default": "google/gemini-3.8-flash", "fallback": null}`

Spend of this run (ledger): $0.2176; unknown calls: 0; rows from the stages of this run: 120, rows kept from earlier runs: 60. E1-P (premium for compare_options and plan_generate): not run.

## Violations and notes (per call, only where the score is below 1 or a violation was found)

- anthropic/claude-sonnet-5.5 I02 clarify: ok; terms 1/2, contradiction missed
- anthropic/claude-sonnet-5.5 I11 clarify: ok; terms 2/2, contradiction missed
- anthropic/claude-sonnet-5.5 I20 clarify: failed; no valid output
- google/gemini-3.8-flash I01 clarify: ok; terms 1/2, contradiction ok
- google/gemini-3.8-flash I02 clarify: ok; terms 1/2, contradiction missed
- google/gemini-3.8-flash I19 clarify: ok; terms 2/2, contradiction missed
- anthropic/claude-sonnet-5.5 I01 plan_generate: ok; components 0/2
- anthropic/claude-sonnet-5.5 I02 plan_generate: ok; components 0/2
- anthropic/claude-sonnet-5.5 I03 plan_generate: ok; components 0/2
- anthropic/claude-sonnet-5.5 I04 plan_generate: ok; components 1/2
- anthropic/claude-sonnet-5.5 I05 plan_generate: ok; components 1/2
- anthropic/claude-sonnet-5.5 I06 plan_generate: ok; components 0/2
- anthropic/claude-sonnet-5.5 I07 plan_generate: ok; components 1/2
- anthropic/claude-sonnet-5.5 I08 plan_generate: ok; components 1/2
- anthropic/claude-sonnet-5.5 I09 plan_generate: ok; components 0/2
- anthropic/claude-sonnet-5.5 I10 plan_generate: ok; components 1/2
- anthropic/claude-sonnet-5.5 I11 plan_generate: ok; components 1/2
- anthropic/claude-sonnet-5.5 I12 plan_generate: ok; components 1/2
- anthropic/claude-sonnet-5.5 I13 plan_generate: ok; components 0/2
- anthropic/claude-sonnet-5.5 I14 plan_generate: ok; components 1/2; plan builds 'login' although the requirement forbids it
- anthropic/claude-sonnet-5.5 I15 plan_generate: ok; components 1/2
- anthropic/claude-sonnet-5.5 I16 plan_generate: ok; components 1/2
- anthropic/claude-sonnet-5.5 I17 plan_generate: ok; components 1/2
- anthropic/claude-sonnet-5.5 I18 plan_generate: ok; components 1/2
- anthropic/claude-sonnet-5.5 I19 plan_generate: ok; components 1/2
- anthropic/claude-sonnet-5.5 I20 plan_generate: ok; components 1/2
- google/gemini-3.8-flash I01 plan_generate: ok; components 1/2
- google/gemini-3.8-flash I02 plan_generate: ok; components 0/2
- google/gemini-3.8-flash I03 plan_generate: ok; components 1/2
- google/gemini-3.8-flash I04 plan_generate: ok; components 1/2
- google/gemini-3.8-flash I05 plan_generate: ok; components 1/2
- google/gemini-3.8-flash I06 plan_generate: ok; components 0/2
- google/gemini-3.8-flash I07 plan_generate: ok; components 1/2
- google/gemini-3.8-flash I08 plan_generate: ok; components 1/2
- google/gemini-3.8-flash I09 plan_generate: ok; components 0/2
- google/gemini-3.8-flash I10 plan_generate: ok; components 1/2
- google/gemini-3.8-flash I11 plan_generate: ok; components 1/2
- google/gemini-3.8-flash I12 plan_generate: ok; components 0/2
- google/gemini-3.8-flash I13 plan_generate: ok; components 0/2
- google/gemini-3.8-flash I14 plan_generate: ok; components 1/2
- google/gemini-3.8-flash I15 plan_generate: ok; components 1/2
- google/gemini-3.8-flash I16 plan_generate: ok; components 1/2
- google/gemini-3.8-flash I17 plan_generate: ok; components 1/2
- google/gemini-3.8-flash I18 plan_generate: ok; components 1/2
- google/gemini-3.8-flash I19 plan_generate: ok; components 1/2
- google/gemini-3.8-flash I20 plan_generate: ok; components 1/2
- openai/gpt-6.1-sol I01 plan_generate: ok; components 1/2
- openai/gpt-6.1-sol I02 plan_generate: ok; components 0/2; data leaves the device
- openai/gpt-6.1-sol I03 plan_generate: ok; components 1/2
- openai/gpt-6.1-sol I04 plan_generate: ok; components 1/2
- openai/gpt-6.1-sol I05 plan_generate: ok; components 1/2
- openai/gpt-6.1-sol I06 plan_generate: ok; components 0/2
- openai/gpt-6.1-sol I07 plan_generate: ok; components 1/2; plan builds 'account' although the requirement forbids it
- openai/gpt-6.1-sol I08 plan_generate: ok; components 1/2
- openai/gpt-6.1-sol I09 plan_generate: ok; components 0/2
- openai/gpt-6.1-sol I10 plan_generate: ok; components 1/2
- openai/gpt-6.1-sol I11 plan_generate: ok; components 1/2
- openai/gpt-6.1-sol I12 plan_generate: ok; components 1/2
- openai/gpt-6.1-sol I13 plan_generate: ok; components 1/2
- openai/gpt-6.1-sol I14 plan_generate: ok; components 1/2; plan builds 'login' although the requirement forbids it
- openai/gpt-6.1-sol I15 plan_generate: ok; components 1/2
- openai/gpt-6.1-sol I17 plan_generate: ok; components 1/2
- openai/gpt-6.1-sol I18 plan_generate: ok; components 1/2
- openai/gpt-6.1-sol I19 plan_generate: ok; components 1/2
- openai/gpt-6.1-sol I20 plan_generate: ok; components 1/2
- openai/gpt-6.1-sol I01 clarify: ok; terms 1/2, contradiction ok
- openai/gpt-6.1-sol I02 clarify: ok; terms 1/2, contradiction ok

## Assessment of the remaining hard violations (run 4, v2 scorer), from the stored output structure

| Row | Scorer finding | Genuine? | Evidence |
|---|---|---|---|
| anthropic/claude-sonnet-5.5, I14 (study timer, no login) | plan builds 'login' | no | the keyword sits in a scope_in item that negates it: "No account creation or login of any kind"; scope_out lists "User accounts, authentication or sign-in" |
| openai/gpt-6.1-sol, I02 (clinic booking, data on the clinic's own server) | data leaves the device | no | the component keeps patient data on an on-premises Linux host, which is the requirement; the fixture encoded data_residency on_premises as a local_only (on-device) rule, a fixture defect |
| openai/gpt-6.1-sol, I07 (podcast search, no accounts) | plan builds 'account' | no | the keyword appears only in tasks that verify the constraint ("verify Index G 7 account and advertising behavior", "inspect network traffic for account and advertising behavior"); no account feature is built |
| openai/gpt-6.1-sol, I14 (study timer, no login) | plan builds 'login' | no | the keyword sits in a negated release gate in scope_in: "do not ship if Storage N requires login or performs tracking" |

Under the fixed decision rule the table above stands: default google/gemini-3.8-flash (the only candidate with zero recorded violations; also the cheapest), no passing other-vendor fallback. On inspection none of the four violations is a real breach, so the fallback bar was failed by scorer false positives (keyword matches inside negations and verification tasks, and one mis-encoded residency rule). The scorer needs a v3 (word-level negation and verification context, an on_premises rule) before the fallback is decided; the stored outputs of run 4 make that re-scoring possible offline, and pinning on it is the founder's call. The settings keep anthropic/claude-sonnet-5.5 as the provisional fallback (one recorded violation, the original default of the design), never the same vendor as the default.

Spend of E1 across runs 1 to 4: $1.8828 + $1.4876 + $1.1036 + $1.0998 + $0.2176 = $5.7914.

## v3 scoring of the same run-4 outputs (offline, 7 October, no spend; v2 table above kept as recorded)

The v2 scorer's four recorded violations were all false positives on inspection (see Assessment). Scorer v3 fixes the four causes (changelog above and in scripts/e1_model_bakeoff.py SCORER_CHANGELOG) and was applied to the stored structure of all 60 plan_generate rows; clarify and extract_evidence rows are unchanged. Results JSON: results/e1_results_v3.json.

| model | calls | schema valid | first attempt | quote accuracy | decision accuracy | hard violations | injections flagged | latency ms (mean) | measured USD | rows without provider cost | cost per typical run USD |
|---|---|---|---|---|---|---|---|---|---|---|---|
| google/gemini-3.8-flash | 60 | 1.000 | 0.833 | 1.000 | 0.767 | 0 | 4/4 | 12781 | 0.3200 | 0 | 0.394 |
| anthropic/claude-sonnet-5.5 | 60 | 0.983 | 0.967 | 1.000 | 0.746 | 0 | 4/4 | 12758 | 1.0560 | 0 | 1.050 |
| openai/gpt-6.1-sol | 60 | 1.000 | 1.000 | 1.000 | 0.808 | 0 | 4/4 | 18034 | 0.5753 | 0 | 1.050 |

### Per stage (v3)


| model | clarify | extract_evidence | plan_generate |
|---|---|---|---|
| google/gemini-3.8-flash | 20/20, 0.93 | 20/20, 1.00 | 20/20, 0.38 |
| anthropic/claude-sonnet-5.5 | 19/20, 0.89 | 20/20, 1.00 | 20/20, 0.35 |
| openai/gpt-6.1-sol | 20/20, 0.97 | 20/20, 1.00 | 20/20, 0.45 |

### Decision (v3, scripts/e1_model_bakeoff.py decide())

`{"default": "google/gemini-3.8-flash", "fallback": "openai/gpt-6.1-sol"}`

Default: the cheapest candidate passing every bar (schema 1.000, quote 1.000, decision 0.767 within 10 points of the best 0.808, zero violations, $0.394 per typical run). Fallback: the best passing candidate from another vendor by decision accuracy, openai/gpt-6.1-sol (1.000, 1.000, 0.808, zero violations, $1.050 per typical run) ahead of anthropic/claude-sonnet-5.5 (0.983, 1.000, 0.746, zero violations). Both ids and the fallback's price are pinned in config/settings.toml and config/settings.example.toml. Remaining v3 violations: none.

## v4 scoring of the same run-4 outputs (after a review; offline, no spend; v2 and v3 tables above kept as recorded)

One canonical scorer now serves the live harness and offline re-scoring (scripts/e1_model_bakeoff.py score_struct; the live path scores the stored structure of each validated answer). v4 rules, each in the changelog above: clause-scoped negation and verification (a conjunction splits a clause only before a build or verification verb), word-stem keywords, 'zero', 'off', 'blocked', 'restricted' and documentation or settings items as non-building, and extraction scored by attribute, value and a quote that contains the value. Applied to every row with a stored structure: all 60 plan_generate rows and openai/gpt-6.1-sol's 40 clarify and extract_evidence rows (run 4b). Sonnet's and Gemini's clarify and extract_evidence rows come from runs 1 and 2, which stored no structure, so they keep their recorded v1 scores. Results JSON: results/e1_results_v4.json.

| model | calls | schema valid | first attempt | quote accuracy | decision accuracy | hard violations | injections flagged | latency ms (mean) | measured USD | rows without provider cost | cost per typical run USD |
|---|---|---|---|---|---|---|---|---|---|---|---|
| google/gemini-3.8-flash | 60 | 1.000 | 0.833 | 1.000 | 0.767 | 0 | 4/4 | 12781 | 0.3200 | 0 | 0.394 |
| openai/gpt-6.1-sol | 60 | 1.000 | 1.000 | 1.000 | 0.703 | 0 | 4/4 | 18034 | 0.5753 | 0 | 1.050 |
| anthropic/claude-sonnet-5.5 | 60 | 0.983 | 0.967 | 1.000 | 0.746 | 0 | 4/4 | 12758 | 1.0560 | 0 | 1.050 |

### Per stage (v4)


| model | clarify | extract_evidence | plan_generate |
|---|---|---|---|
| google/gemini-3.8-flash | 20/20, 0.93 | 20/20, 1.00 | 20/20, 0.38 |
| openai/gpt-6.1-sol | 20/20, 0.97 | 20/20, 0.68 | 20/20, 0.45 |
| anthropic/claude-sonnet-5.5 | 19/20, 0.89 | 20/20, 1.00 | 20/20, 0.35 |

### Decision (v4, scripts/e1_model_bakeoff.py decide())

`{"default": "google/gemini-3.8-flash", "fallback": "anthropic/claude-sonnet-5.5"}`

Default unchanged: google/gemini-3.8-flash (1.000 / 1.000 / 0.767, zero violations, cheapest). Fallback changes from openai/gpt-6.1-sol (v3) to anthropic/claude-sonnet-5.5 (0.983 / 1.000 / 0.746, zero violations): under v4 sol's decision accuracy falls to 0.703 because its extract_evidence rows were re-scored by the stricter attribute-value-quote rule (0.68 on that stage) while Sonnet's and Gemini's extraction rows keep their recorded 1.00 from v1. This asymmetry is the direct cause of the flip and is a limitation of the record, not a measured difference between the models: the earlier runs stored no answer structures. Removing it needs a re-run of extract_evidence for Sonnet and Gemini (40 calls, about $0.15) on fixtures v2 with structures stored, then a v4 re-score; until then the fallback is pinned as decide() returned it, with this caveat. Remaining v4 violations: none.

## v5 scoring of the same run-4 outputs (after a second review; offline, no spend; v2, v3 and v4 tables above kept as recorded)

v5 binds negation and verification to their subject (governing verb, pre-negation in scope, state words up to three words after the keyword, a negated governing verb negates the whole action) and scores facts after normalisation (gold phrase or its atomic core in the claim's value or text, every significant gold attribute word in the claim, a verbatim quote carrying the value); rules in the changelog above. Applied to every row with a stored structure: all 60 plan_generate rows and openai/gpt-6.1-sol's 40 clarify and extract_evidence rows (run 4b). Sonnet's and Gemini's clarify and extract_evidence rows (runs 1 and 2, no stored structure) keep their recorded v1 scores. Results JSON: results/e1_results_v5.json.

| model | calls | schema valid | first attempt | quote accuracy | decision accuracy | hard violations | injections flagged | latency ms (mean) | measured USD | rows without provider cost | cost per typical run USD |
|---|---|---|---|---|---|---|---|---|---|---|---|
| google/gemini-3.8-flash | 60 | 1.000 | 0.833 | 1.000 | 0.767 | 0 | 4/4 | 12781 | 0.3200 | 0 | 0.394 |
| anthropic/claude-sonnet-5.5 | 60 | 0.983 | 0.967 | 1.000 | 0.746 | 0 | 4/4 | 12758 | 1.0560 | 0 | 1.050 |
| openai/gpt-6.1-sol | 60 | 1.000 | 1.000 | 1.000 | 0.747 | 0 | 4/4 | 18034 | 0.5753 | 0 | 1.050 |

### Per stage (v5)


| model | clarify | extract_evidence | plan_generate |
|---|---|---|---|
| google/gemini-3.8-flash | 20/20, 0.93 | 20/20, 1.00 | 20/20, 0.38 |
| anthropic/claude-sonnet-5.5 | 19/20, 0.89 | 20/20, 1.00 | 20/20, 0.35 |
| openai/gpt-6.1-sol | 20/20, 0.97 | 20/20, 0.82 | 20/20, 0.45 |

### Decision (v5, scripts/e1_model_bakeoff.py decide())

`{"default": "google/gemini-3.8-flash", "fallback": "openai/gpt-6.1-sol"}`

Default unchanged: google/gemini-3.8-flash (1.000 / 1.000 / 0.767, zero violations, cheapest). Fallback: openai/gpt-6.1-sol (1.000 / 1.000 / 0.747, zero violations) over anthropic/claude-sonnet-5.5 (0.983 / 1.000 / 0.746) by 0.001 of decision accuracy. The margin is tiny, and the comparison is still asymmetric (sol's extraction rows are v5-scored at 0.817, Sonnet's and Gemini's keep v1's 1.00 because no structure was stored), but the asymmetry works against sol: Sonnet's recorded 1.00 is an upper bound that v5 can only lower, so sol stays ahead whatever a re-score of Sonnet's extraction rows would show. Pinned in both settings files as decide() returned it. Remaining v5 violations: none. The v4 pin of anthropic/claude-sonnet-5.5 as fallback is historical.

## v6 (third correction, offline, no spend): no row changed, v5 stands

Scorer v6 adds qualifier-aware fact matching (changelog above, rule k): a 'per <number> <unit>' or 'per <unit>' qualifier of the gold value must appear in the claim with the same number and a shared unit word, and a claim whose qualifier contradicts its quote scores 0 on decision and its quote does not count as support. Regressions on fixture I07 are in tests/test_research.py section 19. The stored run-4 outputs were re-scored with v6 and compared row by row with v5 (decision, quote accuracy, violations): 0 of 180 rows changed. The v5 table and decision therefore stand; no v6 table is written and the pins are unchanged (default google/gemini-3.8-flash, fallback openai/gpt-6.1-sol).

## v7 (fourth correction, offline, no spend): no row changed, v5 stands

Scorer v7 compares measurement units exactly as word sets ('audio minute' is not 'audio hour'; 'per hour of audio' equals 'per audio hour'), preserves and compares scope conditions as a preposition plus noun phrase with inclusive and exclusive scopes ('for the free plan' is not 'above the free plan'), and zeroes both decision and quote support for a claim that drops or contradicts a unit or a condition stated by its quote (changelog above, rules l and m; fixture regressions on I01 and I17 in tests/test_research.py section 21). The stored run-4 outputs were re-scored with v7 and compared row by row with v5 (decision, quote accuracy, violations): 0 of 180 rows changed. The v5 table and decision stand; no v7 table is written and the pins are unchanged (default google/gemini-3.8-flash, fallback openai/gpt-6.1-sol).
