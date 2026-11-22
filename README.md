# NextMVBee

NextMVBee turns a founder's idea into an MVP plan in which every recommended service rests on source sentences quoted word for word from stored pages, and it names each must-have requirement that is still unproven instead of guessing.

## 1. What it is

A founder who turns an idea into a first product must choose its services: which language model, which hosting, which speech service. The vendor facts behind each choice are scattered, conditional and soon outdated. A wrong choice surfaces late, as a bill above the budget or as a missing capability.

NextMVBee is a multi-agent system for that founder. For each technical decision it researches public web pages and ends in one of three states: a recommendation proven on every must-have, a recommendation marked "not yet proven" on the must-haves it names, or an open decision with the check that would settle it.

What is different from asking a model for a recommendation: models extract claims, judge support and compare options, and code enforces the rules a model cannot be trusted with. The exact quote must be on the stored page. Each claim gets a separate verdict. Each must-have is a gate. A budget is judged by arithmetic. Every paid call is reserved against a spending limit before it is sent. The founder approves.

This repository is the public review copy of a course capstone: the final project of a course on autonomous agent systems. It holds a selected core of the system, taken from the final release of the author's private working repository; the release of this repository is tagged `capstone-v1.0`. Section 3 lists what is in it; section 5 reviews it with one command, with no key and no account.

## 2. Architecture

A run passes through seven stages.

1. **Clarify.** A model turns the idea into requirements and proposes which of them are must-haves.
2. **Ask the founder.** The run stops and waits wherever only the founder can answer: the requirements, the expected usage, an interrupted call.
3. **Plan the research.** A planner names the decisions the plan needs and the candidates for each. Up to three researchers search and read in parallel. Every page is stored as untrusted text.
4. **Extract evidence.** An extractor proposes claims, each with a quote. Code keeps a claim only if the quote is on the stored page, word for word, and records its position.
5. **Verify.** A separate verifier reads each claim against its page and returns a verdict. Only a claim with the verdict "supported", whose passage covers the product, version and plan in question, can carry a decision.
6. **Compare the options.** Code judges each must-have against the verified claims and computes the monthly cost from sourced prices and the founder's usage. A comparison model then recommends one option per decision, and code accepts the recommendation only if it passes the gates.
7. **Write the plan.** The plan is stored as a version with a SHA-256 signature of its content. The founder approves a version, and the documents are produced from the approved version only.

The stages are nodes of one graph (`app/graph/workflow.py`, built on LangGraph), and code routes between them: no prompt decides what runs next. Twelve kinds of model role work inside the stages, each with its own instructions, its own typed output contract (`schemas/`) and its own tool permissions (`app/core/tools.py`); `prompts/runtime/` holds the instructions of the roles the review path drives. The roles exchange typed records through one SQLite store and never talk to each other directly. A ledger (`app/core/ledger.py`) reserves the worst-case cost of every model call and every search before it is sent, so a hosted run cannot pass 30 USD, and it counts the model calls in the same transaction, so the call that would pass 1,000 is not sent. The limit of 480 minutes of active run time is checked before a new external call is started: a call already under way is not cut, so a run can end slightly above the limit. By design, a run that crashes resumes from its recorded steps: a completed step is replayed from the store and is not sent again. [DESIGN.md](DESIGN.md) explains each part and names the file to read for it.

## 3. What is in this repository, and what is left out on purpose

The course asks four things of the repository.

| The course asks for | Where it is |
|---|---|
| A README that explains the project, the architecture, the setup and the usage | This file: sections 1, 2, 4 and 5 |
| The main code | `app/`, `schemas/`, `prompts/runtime/`, `config/` |
| Supporting materials: sample inputs, outputs, evaluation artifacts | Inputs: `evaluation/protocol/projects/`, `tests/fixtures/`. Outputs: `samples/`, `examples/worked_example_output.txt`. Evaluation: `evaluation/RESULTS.md`, `evaluation/protocol/EVAL_PROTOCOL.md`, `evaluation/experiments/` |
| Clear instructions for running or reviewing | Sections 4, 5 and 6; `review.sh` |

The paths:

| Path | What it holds |
|---|---|
| `app/graph/` | The workflow graph: the seven stages and the routing between them |
| `app/core/` | The rules: the exact-quote rule, verification, the must-have gates, the budget arithmetic, the spending ledger, tool permissions, the points where the founder decides |
| `app/runner/` | Starting, interrupting and resuming a run |
| `app/adapters/` | The gateways to the model provider and to web search |
| `app/repo.py` | The store |
| `schemas/` | The typed contract of every stage, the store's schema and its migrations |
| `prompts/runtime/` | The instructions of the roles the review path drives: 16 files |
| `config/` | The example settings and the limits of the hosted service. No key |
| `tests/` | Nine offline test suites, their stand-in providers and two fixtures |
| `scripts/` | Two measurement tools that one suite runs, and the replay tool they import |
| `examples/` | The worked example and its recorded output |
| `evaluation/RESULTS.md` | Every result with its date, its version and its label |
| `evaluation/protocol/` | The evaluation protocol and the inputs of two synthetic test projects |
| `evaluation/experiments/` | Three experiment records: the model comparison (E1), the crash and resume series (E3), a tree measurement (E5). The other numbers of the series are not published |
| `samples/` | Two delivered documents and the list of samples |
| `review.sh` | The one command of the review path |
| `DESIGN.md`, `NOTICE.md` | The design; the notice |
| `requirements.lock.txt`, `.github/workflows/review.yml` | The pinned packages; the workflow that runs the review path on Linux |

On 21 November 2026 the copy holds 203 files: 192 of the release's 1,956 files and 11 written for the copy (this file, `DESIGN.md`, `NOTICE.md`, `review.sh`, the two files under `examples/`, `evaluation/RESULTS.md`, the three files under `samples/` and `.github/workflows/review.yml`).

**The code is the release's code.** For publication, the comments and docstrings of 53 Python files, the comments of the 2 settings files and the wording of 10 evaluation documents were edited so that an outside reader can follow them, and `.gitignore` lost the rules for parts that are not here. The build of this copy proves, for every Python file, that the syntax tree without docstrings and the sequence of code tokens are identical before and after, and that both settings files load equal values. The proof compares each file with the private release, so it runs in the build and its per-file record is kept with the build. Two code lines differ from the release: each named a folder of the author's machine and now names a path relative to the repository (`scripts/ops/replay.py`, `tests/test_evidence_packs.py`). The 119 files of the prompts, the schemas, the fixtures and the lock file are the release's files, byte for byte.

**One folder was renamed.** In the working repository the 12 evaluation documents stand under `docs/`; here they stand under `evaluation/protocol/` and `evaluation/experiments/`, beside the results. No command, script or test opens one of them. Pointers to them in prose and in comments name the new paths. One error message in `app/core/retrieval.py` and one prompt header keep a `docs/` path, because they are code and a prompt.

**Identifiers of the development remain** in string literals, in the prompts and in the contracts under `schemas/`: rule numbers such as "D-A133" in the labels the suites print, in stored reason texts and in the headers of the prompts. These texts are read at run time, so they are published as the release holds them and are proven byte for byte equal to it. A reader can ignore the numbers.

Left out on purpose:

- The web interface, the sign-in and the document renderer. They are not needed to follow a requirement to its evidence and its decision.
- The command line that starts a paid research run. This copy cannot spend money.
- The prompts of the roles the review path does not drive (51 of the release's 67 prompt files; all 13 JSON contracts are here) and 42 of its 51 test suites, which need the parts above.
- The reference answers of the test projects.
- The development records: decisions, audits, handoffs and run reports. The evaluation documents name some scripts, reference answers and result files that are not published here, by their paths in the private repository (under `docs/` and `scripts/`).

## 4. Set up

Python 3.12 is needed. From the repository root:

```
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.lock.txt
```

The second command downloads the pinned packages, so it needs the network. Nothing after it does. No key, no account and no configuration file are needed. On 21 and 22 November 2026 the installation took 12 to 15 seconds in nine runs with the packages already in the local cache; a first installation takes longer.

## 5. Run the review

```
sh review.sh
```

The command runs nine offline test suites of the release and then the worked example, ten commands in all. Every command runs on stand-in providers: a stub model server on the loopback address and a simulated search adapter. No request leaves the machine.

It prints one line for each command (the status, the file, the seconds, the command's own last line) and a closing line:

```
ok    tests/test_workflow.py               1s  15/15 checks passed
...
review path: 10 of 10 commands passed in 36 seconds
```

The exit status is 0 only if all ten passed. On 21 and 22 November 2026, on one Apple silicon laptop with macOS and Python 3.12.13, all ten passed in each of nine runs, each from a fresh clone in a fresh environment, in 35 to 37 seconds a run. On Linux, the workflow of this repository (`.github/workflows/review.yml`) runs the same command on every push, and its runs are listed under the repository's Actions tab. It passed on 19 November 2026 (2 minutes 48 seconds, the installation included) and on 21 November 2026, each time on the commit then published.

| # | Command | What it proves | Checks |
|---|---|---|---|
| 1 | `tests/test_workflow.py` | Typed contracts refuse unknown fields. A call's cost is reserved before it is sent. A run pauses for the founder, survives a restart and resumes without repeating a model call. A changed requirement makes only its dependent decisions stale | 15 |
| 2 | `tests/test_lifecycle.py` | A question asked in the middle of tool use keeps its identity. A resume repeats no search and no model call. The limits on repairs and fallbacks hold across a resume | 8 |
| 3 | `tests/test_audit.py` | The ledger after an interrupted call: a timed-out call is recorded as unknown, a retry is a new billing attempt, and the possible charge stays counted. Nothing starts after a stop | 24 |
| 4 | `tests/test_concepts.py` | Tool permissions by stage, the tool limit per stage, parallel researchers whose proposals never overwrite a confirmed requirement, the optional tree search inside the comparison, interventions that fire on recorded signals only | 53 |
| 5 | `tests/test_evidence_packs.py` | What the comparison is shown for each candidate: must-have evidence first, every contradiction included and named. Recorded claims of one test project are rechecked | 36 |
| 6 | `tests/test_reuse_keys.py` | A stored verdict is reused only while the requirement, the claims, the prompt version and the model are the same. A call whose outcome is unknown blocks the next call of its step | 29 |
| 7 | `tests/test_tree_prune.py` | The gates inside the comparison: an option whose evidence contradicts a must-have is pruned with the reason; an option with no evidence stays in, marked unresolved; an option over the budget is pruned by arithmetic | 21 |
| 8 | `tests/test_currency_reread.py` | A price from a page stored by an earlier run is read again from the vendor's page before it is used; a failed read is recorded | 21 |
| 9 | `tests/test_evidence_pool.py` | Which products are eligible for a decision, and the statement of what was excluded and why when none is | 12 |
| 10 | `examples/worked_example.py` | One requirement followed end to end, then three variations (section 6) | 12 |

219 checks in the nine suites and 12 in the worked example. A suite can be run alone, and then prints one line per check:

```
env NMB_TEST_FIXTURES=1 .venv/bin/python tests/test_workflow.py
```

`NMB_TEST_FIXTURES=1` marks the process as a test run; `review.sh` sets it for each command.

## 6. A worked example

`examples/worked_example.py` follows one requirement through the whole workflow and prints what the run stored.

```
.venv/bin/python examples/worked_example.py
```

**Input.** The input is synthetic: the idea, the vendor page "Speech API A" and its prices are invented, and the models are stand-ins that return scripted answers.

```
Idea:   Multilingual meeting assistant for small teams: transcribes Hebrew and English calls and emails a summary.
Usage:  40 meetings a month in all, 45 minutes each, 3 users  (the founder's answer to the usage question)
```

**Output.** The recorded output is `examples/worked_example_output.txt`, and the command above prints exactly that file. The decision followed is the choice of a transcription service. Five lines carry it:

```
  budget_monthly_usd   = 50       must-have  (confirmed)
  Claim:        Speech API A v3: price_per_hour is $0.60 per audio hour
  Quote:        "Price per hour: $0.60 per audio hour."
  ok    the stored page holds exactly the quote at that position
  ok    40 meetings of 45 minutes are 30 hours; 30 x 0.60 = 18.00 USD a month
```

1. The founder's budget of 50 USD a month is stored as a confirmed must-have.
2. The extractor proposed a price claim with a quote. Code kept it because the quote is on the stored page, and the script reads the page again at the recorded position to check.
3. The verifier returned the verdict "supported".
4. No model judges the budget. Code multiplies the sourced price by the founder's usage: 30 hours at 0.60 USD is 18.00 USD a month, within 50.
5. The comparison proposed the option, and code made the decision active because it cites verified claims and no must-have is contradicted.

The stand-ins name the one option twice: the scripted page calls it Speech API A, and the scripted comparison calls it Orion Compute 1.

The same run is then repeated under three changes:

| Change | What the output shows |
|---|---|
| The model returns one more claim whose quote is not on the page | The claim is refused and never stored |
| The usage is 400 meetings a month | 300 hours at 0.60 USD is 180.00 USD, over the budget of 50: the calculation contradicts the must-have and no decision for the option is active |
| The verifier finds no claim that supports the languages must-have | The option is recommended as "not yet proven" on that must-have, never presented as proven |

The script makes twelve checks of its own and exits with status 1 if one fails.

## 7. Evaluation results

| What was measured | Date (2026) | Result | Label |
|---|---|---|---|
| A hosted run on ZAMINO, the author's own project (section 8): four decisions | 13 November | 0 of 4 decisions made; 12.89 USD; 107 minutes | Release |
| The same project after three design changes | 14 November | 4 of 4 made, three conditional; 10.51 USD; 92 minutes | Release |
| The research run of the validation journey | 15 to 16 November | 4 of 4 made, all conditional; 198 pages read; 1,063 quoted sentences kept, 1,001 verified; 15.59 USD of a ceiling of 30; 104 minutes | Release |
| Three recalculations on the evidence already verified, no new research | 17, 18 and 19 November | 1.61, 1.61 and 1.58 USD, about six minutes each; of four recommendations one changed, then none, then one | Release |
| The product's own checks of each document before release | 15 to 18 November | Four productions refused; each cause was fixed and the document then produced | Release |
| Reading the delivered documents against the approved version | 16 to 17 November | Five defects in version 1, ten in version 2; all closed | Release |
| The plan version presented, version 3, approved by the founder | 18 November | Four recommendations, all conditional. Three of four parts priced: 39.29 USD a month of a ceiling of 150; seven checks named for the open must-haves | Release |
| Crash and resume: live runs killed on purpose | 23 October | Across 20 crash tests, no completed step ran twice, and the ledger recorded no duplicate charge | Development |
| The optional tree search against a simple comparison, eleven decisions | 3 November | Each decided three; the simple comparison was right on two and the tree on one; the tree cost about 13 percent more | Development |
| The review path of this copy | 21 November | 10 of 10 commands passed; 219 and 12 checks. On Linux: passed on 19 November | Development |

Labels. Release: observed on a deployed release, used the way a founder uses it. Development: measured during development on a test configuration. A decision counts as made when the plan recommends a service for it, conditional or not.

Version 3 of ZAMINO is the plan version this submission presents:

| Decision | Recommended | USD a month | Not yet proven on |
|---|---|---|---|
| PDF rendering | Puppeteer, self-hosted, a free library | open: its running cost is not established | Budget, languages, timeline |
| Hosting | Hetzner Cloud CAX21 | 8.49 | Users, timeline, privacy |
| Language model | Mistral Large 4 | 3.92 | Users, languages, privacy |
| Images | Google Nano Banana 2.1 | 26.88 | Users |

Every figure, with its version, its source and what was not measured, is in [evaluation/RESULTS.md](evaluation/RESULTS.md).

## 8. The sample documents

`samples/` holds the two documents of version 3 of ZAMINO, the author's own project: illustrated history packs for children in Hebrew, Arabic and English, on a budget of 150 USD a month for all services.

| File | What it is |
|---|---|
| `samples/zamino-mvp-plan-v3.pdf` | The MVP plan, 52 pages: the four recommendations with their quoted evidence, the budget, the open requirements and seven planned verification checks |
| `samples/zamino-product-requirements-v3.pdf` | The product requirements document, 20 pages |

The founder approved version 3 on 18 November 2026. Both files were produced in the hosted application on 21 November 2026, by the release this copy is built from, and are published as the service printed them. The pages follow the service's fixed layout. The cover carries a name, "The Little Museum Plan", and one illustration: the two things a model contributes to a document (section 9).

[samples/README.md](samples/README.md) lists every sample with its origin, says what a reader will find in the two documents, and separates these two real outputs from the synthetic material.

## 9. Limitations

- The review path proves the control logic on stand-in providers and on synthetic or recorded text. It does not prove the quality of live research.
- The copy cannot start a live run (section 3).
- One journey was validated from the idea to the delivered documents, on one project. Live projects have no reference answers, so the accuracy of a live recommendation is not measured. Calibration is not measured either: a model's stated confidence triggers nothing.
- All four recommendations of the validated journey are conditional. The plan is a starting point with named checks, not proof that the services meet the must-haves. The plan names seven checks; for two of the four picks the open must-haves have no check of their own among the seven. None of the checks has been run.
- A recommendation is a model's judgement. The comparison was recalculated three times on the same evidence (17, 18 and 19 November 2026): one of four recommendations changed, then none, then one. Each recalculation ran on a newer release, so this is an observation, and the comparison's stability was not measured.
- Prices age. At the rates found on the vendors' pages on 16 November 2026, the three priced parts of version 3 cost about 47 USD a month against the plan's 39.29 (hosting 12.49 for new orders; the language model 7.70 after a launch sale ended). The fourth part is unpriced, so the fit of the whole system within 150 USD is not yet shown.
- Version 4, a recalculation of 19 November 2026, priced its hosting pick at a plan of 4 USD a month where its own words name the plan of 24 USD. The final release closes the cause: among an option's sourced monthly prices, the plan that the pick's own words identify is the one priced, and an amount in those words names a plan's monthly price only where its own words, before it or behind it, give it the month as its cadence, or no cadence and no other role, each figure read whole and each amount in the currency of its own mark. This is a reading by forms and not a parse of the language: where the words name no single plan the lowest fee is priced as before, and a wording outside the forms can be read wrongly ("at 37 USD for the year" is read as a monthly price). A stored version keeps its figures, so version 3 is the version presented (`evaluation/RESULTS.md`, section 5.6).
- On the hosted service the layout of every document is fixed. No layout that a model designed passed its review gate, so, by a change approved on 19 November 2026 and carried by this release, a model contributes the cover's concept only: a name and one illustration, by one design request and one illustration request. A production is delivered with the standard cover whenever either request is refused or fails a check.
- No single-agent baseline was tested, and nothing shows that twelve is the best number of roles.
- The review path was run on two systems only: macOS with Python 3.12.13 (21 November 2026) and the Linux runner of the workflow with Python 3.12 (19 November 2026).

## 10. Author and contact

Rozi Qzamil. Contact: Founder@NextMVBee.ai.

- The public repository: https://github.com/NextMVBee/nextmvbee-capstone
- The release: the tag `capstone-v1.0` of that repository.
- The hosted application runs at https://app.nextmvbee.ai behind a sign-in; a reviewer of the course receives access on request to Founder@NextMVBee.ai.

The repository is sufficient without the hosted application: nothing in the review path needs it.

## 11. Notice

This repository is published for the review of a course submission. All rights are reserved; it is not an open-source release. Third-party packages keep their own licences. See [NOTICE.md](NOTICE.md).
