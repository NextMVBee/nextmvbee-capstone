# Samples

Two kinds of sample are in this repository, and they are never mixed. A delivered document is a real output of the deployed system for a real project. A file produced from a test input shows the system's rules on synthetic or recorded material and is not a result of live research.

## 1. Delivered by the hosted service

ZAMINO is the author's own project: illustrated history packs for children in Hebrew, Arabic and English, on a budget of 150 USD a month for all services. One journey on the hosted application took it from the idea to the downloaded documents, starting on 15 November 2026. The founder approved version 3 of its plan on 18 November 2026. Version 3 is the plan version this submission presents.

| File | What it is | Origin |
|---|---|---|
| `zamino-mvp-plan-v3.pdf` | The MVP plan of version 3, 52 pages: the four recommendations with their quoted evidence, the budget, the open requirements and seven planned verification checks | Produced in the hosted application on 21 November 2026, on the release this copy is built from. Published as printed |
| `zamino-product-requirements-v3.pdf` | The product requirements document of the same version, 20 pages | The same |

What a reader will find in them:

- **The cover.** The name "The Little Museum Plan" and one illustration. These are the two things a model contributes to a document. Every page after the cover follows the service's fixed layout.
- **Four conditional recommendations.** Each of the four is printed as "Recommended, not yet proven" on the must-haves it names. Three of the four parts are priced: 39.29 USD a month of a ceiling of 150.
- **One sentence that overstates.** The page "This version", near the end of the MVP plan, prints "Every decision of this version is settled." Every decision has a recommendation; none is proven on all must-haves. The sentence is content of the approved version and is published as approved. `evaluation/RESULTS.md` reports it as a finding (section 5.4).
- **Identifiers.** Every source, claim, decision and check reference begins with the identifier the hosted service gave the project.
- **Rule numbers.** Three lines of each document print a rule number from the author's records ("D-A133", "D-A143") beside options that were not weighed. The number identifies the rule that excluded the options and can be ignored.
- **Quoted text.** Dashes and other typography inside quoted vendor text and source titles are the sources' own. A source's title can carry the name of its publisher or its author.
- **Dates.** The documents print the dates of the research (15 to 16 November 2026), of the version (18 November 2026) and of their own production.

Two reader's notes on the delivered documents. The documents are published as the service printed them and are not changed.

- **The checks.** Two recommendations, PDF rendering and image generation, still lack a dedicated check for their open requirements. The cover of the requirements document prints "4 unresolved checks", one for each conditional recommendation, and the verification chapter of the MVP plan lists seven planned checks (VCK-016 to VCK-022). The two figures count different things.
- **The reason given for the language model.** The cited passages support Mistral's unit prices. They do not establish that it was the only text model within budget. Page 10 of the MVP plan and page 7 of the requirements document say that Mistral Large 4 is the only text model whose cost is confirmed within the budget. The claims cited for that sentence (CLM-650 and CLM-651) are Mistral's prices per million tokens. The comparison on page 19 of the MVP plan prints estimates of 7.20 USD and 57.60 USD a month for two alternatives, both under the ceiling of 150 USD.

The MVP plan reproduces the opening of the founder's brief and selected answers. The repository includes no separate raw brief, answer file or run screenshots.

## 2. Produced from a test input

| File | What it is | Origin |
|---|---|---|
| `examples/worked_example_output.txt` | The recorded output of the worked example: one requirement followed to its quote, its verdict, its cost calculation and its decision, then three variations | Printed by `examples/worked_example.py` on stand-in providers. The idea, the vendor, its page and its prices are invented. The build of this copy checks that the script prints exactly this file |
| `evaluation/protocol/projects/devA_trip_planner/` (4 files) | The inputs of a test project, a personal trip planner: the brief, the decisions to make, the must-haves, the founder's prepared answers | Written by the author for the evaluation. Synthetic |
| `evaluation/protocol/projects/devB_hebrew_support/` (4 files) | The inputs of a second test project, a support assistant for a small shop: the same four files | Written by the author for the evaluation. Synthetic |
| `tests/fixtures/deva_contradictions.json` | Recorded claims of the trip planner test project, among them the three price tiers of one service (5, 17 and 20 USD per 1,000 requests) | Recorded by a test run of 7 November 2026: short quotes of public web pages, with their addresses. One suite of the review path rechecks them |
| `tests/pipeline_fake.py` | The stand-in providers: a stub model server, a simulated search adapter and one synthetic vendor page | A file of the release. Synthetic text only |

## 3. Evaluation records

The three experiment records under `evaluation/experiments/` and the page `evaluation/RESULTS.md` are evidence, not samples of output. The README describes them in section 7.
