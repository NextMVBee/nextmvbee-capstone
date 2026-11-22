# Results

Every figure of the capstone report (the document of the course submission, which is not in this repository), of its presentation and of the README, with its date, the version it was measured on, its label and its source. State of 21 November 2026.

## How to read a row

- **Date**: when the measurement was made. All dates are in 2026.
- **Version**: the first characters of a commit of the author's private working repository. The commits are not in this repository. They are given so that a reader can see which figures come from the same version of the system; they are the only identifiers of that repository on this page. The release of this repository is tagged `capstone-v1.0`.
- **Label**, one of two. **Release**: observed on a deployed release, used the way a founder uses it: a run a founder started, or the documents it produced. **Development**: measured during development on a test configuration: an experiment, an offline analysis of stored records, or a run under other limits. A development figure is not a property of the release.
- **In this repository**: the file that supports the figure, or "record kept by the author".

The author's own project is called the demonstration project on this page.

## 1. The research runs

A decision is one service choice. A conditional recommendation ("recommended, not yet proven" on a named must-have) counts as a decision made. Minutes are active run time (a) or clock time from the run's start to its end, the founder's answering time included (c).

| Run | Date | Version | Decisions made | Research cost (USD) | Minutes | Label | In this repository |
|---|---|---|---|---|---|---|---|
| First test project | 6 to 7 November | 9ab2015 | None: the run stopped before the comparison | 5.94 | 481.5 a | Development: a test run from the command line under a limit of 20 USD | Record kept by the author |
| Trip planner test project | 7 November | 9ab2015 | 0 of 4 | 9.46 | 64 a | Development: as above, limit 12.50 USD | Inputs: `evaluation/protocol/projects/devA_trip_planner/` |
| Support assistant test project | 7 November | 9ab2015 | 0 of 3 | 3.67 | 21 a | Development: as above, limit 12.50 USD | Inputs: `evaluation/protocol/projects/devB_hebrew_support/` |
| Demonstration project, hosted | 13 November | 89fb97e | 0 of 4 | 12.89 | 107 c | Release | Record kept by the author |
| Demonstration project, hosted | 14 November | 16d5ae9 | 4 of 4, three conditional | 10.51 | 92 c | Release | Record kept by the author |
| Demonstration project, hosted: the validation journey | 15 to 16 November | 4dfe3a8 | 4 of 4, all conditional | 15.59 | 104 c | Release, in a reviewer account | Section 5 |

Notes:

- The hosted runs ran under 30 USD, 480 active minutes and 1,000 model calls (`config/settings.hosted.toml`). The three test runs ran under 20 and 12.50 USD.
- A run that chooses nothing violates no must-have. That is not a safety rate of chosen recommendations, and a withheld decision also costs the user.
- First test project: the time limit of 480 active minutes stopped the run. The limit is checked before each call is sent, so a run can end slightly above it.
- 14 November: the cost does not include up to 2.02 USD that two interrupted calls may have charged.
- 15 to 16 November: 15.593127 USD billed, no possible charge beside it; no call was interrupted and no limit stopped the run.

## 2. Other figures of the report

| Figure | Date | Version | Label | In this repository |
|---|---|---|---|---|
| A vendor's page listed one service at three rates for three tiers: 5, 17 and 20 USD per 1,000 requests | 7 November | 9ab2015 | Development: the trip planner test run | `tests/fixtures/deva_contradictions.json` holds the three claims with their quotes |
| A hosted run is limited to 30 USD, 480 active minutes and 1,000 model calls; a stage to eight tool calls | 21 November | The final release | Configuration of the release | `config/settings.hosted.toml`; printed by the worked example, part 6 |
| Seven stages; twelve kinds of role, five with search or reading tools; up to three researchers at once; a panel of three models | 21 November | The final release | Description of the release | `app/graph/workflow.py`, `app/core/roles.py`, `app/core/tools.py`, `config/settings.hosted.toml`; DESIGN.md lists the roles |
| The default model and its fallback were chosen on 180 scored outputs: three candidates, 20 ideas, three stages | 7 October | The system on that date | Development: an experiment | `evaluation/experiments/E1_RESULTS.md` |
| Default model Gemini 3.8 Flash, fallback GPT-6.1 Sol; judging, comparing, enriching and editing routed to Claude Opus 5.5 | 21 November | The final release | Configuration of the release | `config/settings.hosted.toml` |
| Over 70 queries, keyword ranking found 13 of 314 reference passages among the first eight returned, embeddings 12, a hybrid 8 (counts, not rates) | 29 October | 35bab44 | Development: one test project's stored pages | Record kept by the author |
| On two test projects, 134 of 177 missing reference items were on pages never collected | 5 November | 0e97508 | Development: an offline analysis | Record kept by the author. The tool is `scripts/eval/retrieval_diagnosis.py` |
| Tree against simple comparison on eleven decisions: each decided three; the simple comparison was right on two, the tree on one; the tree cost about 13 percent more (5.26 against 4.65 USD) | 3 November | dc8a169 | Development: an experiment on stored evidence of three test projects | Record kept by the author. `evaluation/experiments/TREE_RESULTS.md` is a different measurement: section 3 |
| A rerun of that experiment paused in 25 of 33 cases | 5 November | 4509ab7 | Development | Record kept by the author |
| The comparison model chose a language model for which a separate check had found no support for Hebrew and English in one call; code refused the choice | 29 to 30 October | 537d791, then 1eb9d2d | Development: a run of the first test project | Record kept by the author |
| Three design changes, tested together: a contradiction between sources no longer pauses the run; a must-have budget is judged by arithmetic; a must-have without evidence gives a conditional recommendation | Deployed 14 November | 16d5ae9 | Description of the release | `app/core/interventions.py`, `app/core/budget.py`, `app/core/coverage_step.py`; the worked example, parts 4, 8 and 9 |
| Of 878 kept sentences, 836 passed verification and 26 were set aside; the records do not explain the other 16 | 14 November | 16d5ae9 | Release | Record kept by the author |
| Across 20 crash tests, no completed step ran twice, and the ledger recorded no duplicate charge (23 live runs in all; a dry run on stand-ins the day before) | 23 October | The system on those dates | Development: an experiment with live providers | `evaluation/experiments/E3_CRASH_RESUME.md` |
| The editing role's effect was measured on one example: two rewrites in one test plan | 13 November | 3608f81 | Development | Record kept by the author |

## 3. Two tree measurements that must not be confused

| | The record in this repository | The experiment the report quotes |
|---|---|---|
| File | `evaluation/experiments/TREE_RESULTS.md` | Record kept by the author |
| Date | 24 October | 3 November |
| Decisions | Five, on synthetic fixtures with one distractor product each | Eleven, on the stored evidence of three test projects |
| Result | Both methods correct on 3 of 5; the tree cost about 2.5 times as much and took about 4 times as long | Each method decided three of eleven; the simple comparison right on two, the tree on one; the tree about 13 percent more costly |
| Consequence | The record does not claim that the tree decides better | The tree became optional and is off by default |

Both are development figures.

## 4. The review path of this repository

| Figure | Date | Version | Label |
|---|---|---|---|
| All ten commands of `sh review.sh` passed in each of nine runs, each from a fresh clone in a fresh environment, in 35 to 37 seconds a run: 219 checks in the nine suites and 12 in the worked example. The installation took 12 to 15 seconds in nine runs with the packages already in the local cache | 21 and 22 November | This copy, built from the final release | Development: one Apple silicon laptop, macOS, Python 3.12.13 |
| The same on Linux, by the workflow of the public repository (`.github/workflows/review.yml`), which runs the same command on every push; its runs are listed under the repository's Actions tab. It passed on 19 November (2 minutes 48 seconds, the installation included) and on 21 November, each time on the commit then published | 19 and 21 November | The commit published on each date: the first edition, built from fb3bded, then the final edition, built from the final release | Development: the hosted runner `ubuntu-latest`, Python 3.12 |

The copy of 21 November holds the same nine suites and the same worked example as the copy the workflow ran on; one suite changed (`tests/test_concepts.py`: four conditions of one check that stood inside a comment are evaluated again, and the check passes with them).

## 5. The validation journey, 15 to 19 November

One journey on the hosted application, in a reviewer account, from the founder's idea to the download of both documents. Every row is a release figure. The demonstration project has five must-haves: up to 150 USD a month for all services, about 50 adult users, three languages, a working pilot within 8 weeks built by one developer, and no personal data of children.

### 5.1 The research run, 15 to 16 November (version 4dfe3a8)

| Figure | Value |
|---|---|
| Questions to the founder | 6 before research; later one question about usage |
| Decisions to make; research questions | 4; 24 |
| Pages read in full | 198 |
| Quoted sentences kept; verified; set aside | 1,063; 1,001; 16. The records do not explain the other 46 |
| Decisions made | 4 of 4, each recommended and not yet proven on one to three must-haves |
| Clock time, the founder's answers included | 104 minutes |
| Research cost | 15.59 USD of a limit of 30.00 USD |

What the page figures count. "Pages read in full" is the count the run screen of the application shows: the stored pages of the project whose full text was fetched, whether a search returned the page with its text or a tool call fetched it by its address. The setting `max_pages` (80 on the hosted service, `config/settings.hosted.toml`) is narrower: it is the ceiling of pages fetched by address in one run (`_slot_body` in `app/core/tools.py`), beside a ceiling of 30 searches of 8 results each. A run can therefore store more pages than `max_pages`.

### 5.2 The four plan versions

Versions 2, 3 and 4 are recalculations. Each was started as a correction of the usage figures: the decisions those figures price were decided again on the evidence already verified, with no new research.

| Version | Created | Cost (USD) | What changed in the four recommendations | Approved by the founder |
|---|---|---|---|---|
| 1 | 16 November | 15.59 (the research run) | The first recommendations | 16 November |
| 2 | 17 November, 08:51 to 08:57 UTC | 1.61 | One: PDF rendering moved from DocRaptor to Puppeteer, self-hosted | Yes; the date is not in the records read for this page |
| 3 | 18 November, 07:57 to 08:03 UTC | 1.61 | None | 18 November, 10:22 UTC |
| 4 | 19 November, 15:15 to 15:21 UTC | 1.58 | One: hosting moved from Hetzner Cloud CAX21 (8.49 USD a month) to DigitalOcean Droplets (priced at 4.00 USD a month; section 5.6) | 19 November, 16:24 UTC |

Version 3 is the version this submission presents (sections 5.3 and 5.6). The three recalculations ran on three successive releases, so their differences are an observation and not a measure of the comparison's stability.

### 5.3 Version 3, the version presented and the version of the sample documents

| Decision | Recommended | USD a month | Not yet proven on |
|---|---|---|---|
| PDF rendering | Puppeteer, self-hosted, a free library | open: its running cost is not established | Budget, languages, timeline |
| Hosting | Hetzner Cloud CAX21 | 8.49 | Users, timeline, privacy |
| Language model | Mistral Large 4 | 3.92 | Users, languages, privacy |
| Images | Google Nano Banana 2.1 | 26.88 | Users |

The founder approved version 3 on 18 November 2026. All four recommendations are conditional. Three of four parts are priced: 39.29 USD a month of a ceiling of 150, by arithmetic on quoted prices and the founder's usage. Each cost equals a recomputation by hand. Version 1 chose DocRaptor at 15.00 USD for its proven price; the later versions chose Puppeteer, a free library, on verified right-to-left evidence, and left its hosting cost open with a named check. The plan names seven checks for the open must-haves, each with its cost and its pass condition. For two of the four picks the open must-haves have no check of their own among the seven. None has been run.

### 5.4 The documents

| Figure | Date | Value |
|---|---|---|
| The sample documents under `samples/`: version 3 as the hosted application printed it on the release this copy is built from | 21 November 2026 | MVP plan 52 pages; requirements document 20 pages. The pages follow the fixed layout; the cover carries the name "The Little Museum Plan" and one illustration |
| Productions the product's own checks refused before release | 15 to 18 November | Four: two of version 1, one of version 2 (it printed a worst case the version does not hold), one of version 3 (it printed "up to" apart from its figure). Each cause was fixed and the document then produced |
| Defects found by reading the delivered documents against the approved version | 16 to 17 November | Five in version 1, among them a worst case of 722.92 USD where the product's rule gives 146.92; ten in version 2. All closed |
| Outside reviews the founder obtained | 17 to 18 November | Version 2: "an evidence-backed starting plan, not a client-ready specification". Version 3: 6 of 10 for clarity, with suppliers before the product, comparisons before the work plan, and "every decision is settled" printed beside unproven conditions |
| A finding in the MVP plan of version 3 | As above | The page "This version" near the end prints "Every decision of this version is settled." beside four recommendations that are all conditional. The sentence is content of the approved version. The rule that composes it was corrected for versions written later (generation 5 in `app/core/docrules.py`: the line counts recommended picks, not settled decisions); an approved version is printed as it was approved |

### 5.5 Prices against the rates found on 16 November

Made by reading pages on the web on 16 November, outside the product, for the services of version 1. The three priced services of version 3 are the same services at the same rates, so the check carries over.

| Figure | Value |
|---|---|
| The three priced parts of version 3 (39.29 USD a month) at the rates found | About 47 USD: hosting 12.49 for new orders, the language model 7.70 after a launch sale ended, images 26.88 as in the plan |
| The fit of the whole system within the ceiling of 150 | Not yet shown: the fourth part, PDF rendering, is unpriced |
| Why the rates differ | The language model's price came from a page that still carried a launch sale that had ended on 22 October; the hosting price predates the vendor's price change of 15 June |
| Did the plan show the uncertainty | For hosting, yes: the plan names the other price and the check. For the language model, no |

### 5.6 Version 4 and the pricing of a plan

Version 4, the recalculation of 19 November, recommends for hosting a plan that its own words name at 24 USD a month, and prices it at 4.00 USD a month: the selection among the option's sourced monthly prices did not read those words. Its total reads 34.80 USD a month.

The final release closes the cause. The words that name a pick are read, and among an option's sourced monthly prices the plan those words identify, by its monthly price or by its quantities with their units, is the one priced (`identified_rows` in `app/core/budget.py`; generation 7 in `app/core/docrules.py`). An amount in those words names a plan's monthly price only where its own words, before it or behind it in its sentence, give it the month as its cadence, or no cadence and no other role (an amount charged once, by the year or per user, a deposit or a setup payment names no plan). A rate's denominator is read whole ("per calendar month" and "per 1.0 month" are the month, "per 3 months" is not), a figure is read whole with its decimal point and its thousands separators, each amount keeps the currency of its own mark, and a quantity with its unit names its plan whatever preposition stands before it (`plan_marks` and `_monthly_price` in the same file). The reading is a reading by forms and not a parse of the language: where the words name no single plan the lowest fee is priced, as before, and a wording outside the forms can be read wrongly (a cadence behind a preposition is not read, so "at 37 USD for the year" names the monthly plan of 37). The proof was made on the stored records of version 4, with no new run: under the new rule they give 24.00 USD for hosting and a total of 54.80 USD, where the stored version shows 4.00 and 34.80. Label: development, an offline recomputation on stored records, 20 November, repeated on 21 November with the rule as the final release holds it: the same figures.

A stored version keeps its figures and its approval, so version 4 still shows 4.00 and 34.80. Version 3, which the rule does not touch (its hosting pick has one sourced plan price), is the version presented.

## 6. What was not measured

- No reference answers exist for live projects, so the accuracy of a live run's recommendations is not measured.
- Calibration is not measured: a model's stated confidence triggers nothing.
- No single-agent baseline was tested.
- The stability of a recommendation under recalculation: observed three times (one recommendation changed, none, one), each time on a newer release, and not measured.
- Whether the recommended services meet the must-haves marked "not yet proven": the plan names the checks, and none has been run.
