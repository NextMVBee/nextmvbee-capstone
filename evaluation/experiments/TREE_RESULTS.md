# E5: the bounded exploration tree against the simple comparison (rerun after an audit of the first run)

Question: on a fixed set of five decisions with gold answers, does the Comparer's bounded tree (app/core/tree.py: up to three
alternatives, three variants each, one batched evaluation, at most seven calls) decide better than the one-call comparison
(compare_options.v4), at what cost and latency? The record states what was measured; it does not assume the tree wins.

## The fixed set

The five gold decisions of docs/experiments/e1_fixtures.json (fixtures I01 to I05: the fixture's product is the gold choice and its
extract facts are the supported evidence). Each decision gets one distractor product, written in scripts/e5_tree_vs_simple.py
(DISTRACTORS): a second identity whose facts fail a hard requirement where the evidence model can express it (a price above the hard
budget under the usage answer: I01, I04) or leave it unsupported (no verification record for the hard requirement: I02, I03, I05). The
evidence is seeded as supported, in-scope claims with verification records for the gold product's facts that entail the fixture's hard
requirements (ENTAILS in the script). In the rerun: the fixtures' own hardness booleans are kept (I05's payments
preference is soft), and a payout delay no longer counts as support for the launch timeline, so I03's and I05's candidates stay unresolved
by code (no fact entails the offline requirement or the six-week timeline).

| Decision | Gold | Distractor | Hard requirements | What the code can decide |
|---|---|---|---|---|
| I01 transcription | Speech API A v3 ($0.60 per audio hour, Hebrew) | Speech API B v1 ($3.00 per audio hour, English only) | budget 50 USD a month, languages he and en | the distractor fails the budget (90 USD at 30 hours); the gold is supported for both |
| I02 booking | Scheduler B 2.4 (self-hosting supported) | Scheduler C cloud (cloud only) | data residency on premises | the gold is supported; the distractor unresolved |
| I03 sync | Sync SDK C 1.9 | Sync SDK D 3.0 (constant connection) | privacy offline | both unresolved (no fact entails the requirement) |
| I04 OCR | OCR API D ($0.0015 per page, 500 free) | OCR API E ($49 per month) | budget 20 USD a month | the distractor fails the budget; the gold is supported |
| I05 payments | Payments E (card fee 2.9% plus $0.30, payouts in 2 days) | Payments F (bank transfers only) | timeline 6 weeks (hard); card payments (soft) | both unresolved for the timeline (a payout delay does not entail launch time); the gold's fee supports the soft card preference |

## Method

scripts/e5_tree_vs_simple.py, run in the author's subshell against the live routes of config/settings.toml, one temporary store per
decision (data/e5/<stamp>/<fixture>.db), one run row per method with its own cap (--per-run) and the completion reserve of 0.45 USD,
under one envelope for the whole experiment (--cap): before every new reservation the committed spend of every method store so far
(reported amounts and open reservations, possible charges included) plus the send's worst case must stay under the cap. A settled
method closes through finish_run; a credit, unknown or budget outcome stops the experiment and leaves the gateway's terminal row.
The simple method: one compare_options.v4 send. The tree: app/core/tree.explore with the three Comparer prompts under
<run>:tree:w0:b0:d1:<part>, the hard constraints checked by code as the workflow does. Cost is the ledger's provider-reported USD per
method and decision, summed unrounded and rounded once; latency the wall seconds of the method.

The gold policy: a method's answer is correct when it names the gold identity and the code settles the hard constraints of what it chose (hard status pass); an answer whose hard status the code leaves unresolved is conditional (an abstention from a settled answer), counted apart whether or not it names the gold identity; no recommendation is an abstention; identity agreement (the gold identity named, settled or not) is reported beside it.

## Results

Run on 24 October 2026 (stamp 20261024T210145; the stores under data/e5/20261024T210145; the JSON docs/experiments/results/e5_tree_vs_simple_20261024T210145.json). The envelope's cap 1.00 USD, the per-method cap 1.00 USD, the completion reserve 0.45 USD; no stop; the envelope's committed total at the end 0.039913 USD.

| Decision | Method | Outcome under the gold policy | Identity agrees | Hard status (code) | Calls | Cost USD (ledger) | Seconds | Chosen (the method's own words) |
|---|---|---|---|---|---|---|---|---|
| I01 transcription | simple | correct | yes | pass | 1 | 0.002129 | 4.7 | Speech API A v3 |
| I01 transcription | tree | correct | yes | pass | 3 | 0.005736 | 20.8 | Multilingual transcription (score 8.85, hard pass; 4 nodes, 1 pruned) |
| I02 booking | simple | correct | yes | pass | 1 | 0.002054 | 2.9 | Scheduler B 2.4 |
| I02 booking | tree | correct | yes | pass | 4 | 0.005769 | 31.5 | Self-Hosted Linux Deployment (Free Tier) (score 9.0, hard pass; 4 nodes, 0 pruned) |
| I03 sync | simple | abstained | no | none (no recommendation) | 1 | 0.002579 | 15.6 | ok: None |
| I03 sync | tree | conditional | yes | unresolved | 4 | 0.005881 | 55.0 | Android Deployment (score 4.05, hard unresolved; 4 nodes, 0 pruned) |
| I04 OCR | simple | correct | yes | pass | 1 | 0.002126 | 4.2 | OCR API D |
| I04 OCR | tree | correct | yes | pass | 3 | 0.004903 | 27.7 | Free Tier Usage (score 9.15, hard pass; 4 nodes, 1 pruned) |
| I05 payments | simple | abstained | no | none (no recommendation) | 1 | 0.002667 | 15.4 | ok: None |
| I05 payments | tree | conditional | yes | unresolved | 4 | 0.006070 | 35.5 | Standard Implementation (score 5.6, hard unresolved; 4 nodes, 0 pruned) |
| all five | simple | correct 3/5; conditional 0; abstained 2 | 3/5 | | 5 | 0.011555 | 42.8 | |
| all five | tree | correct 3/5; conditional 2; abstained 0 | 5/5 | | 18 | 0.028359 | 170.5 | |

Total reported spend of the rerun: 0.039914 USD (the ledger totals summed unrounded: simple 0.011555, tree 0.028359; the table's rows are rounded to six places), under the author's 1 USD.

## Reading

- Under the gold policy both methods answer three of the five decisions correctly (the gold identity with the hard constraints settled by
  code: I01, I02, I04). On the two decisions the code cannot settle (I03, I05) the simple comparison returned no recommendation twice
  (abstained) and the tree selected a branch of the gold identity with its hard status unresolved twice (conditional): identity agreement
  5 of 5 for the tree against 3 of 5 for the simple comparison, but neither is a settled answer, and the product leaves a conditional
  selection unresolved as the live run shows. The record does not claim that the tree decides better.
- Cost: the tree cost about two and a half times the simple comparison (0.028359 against 0.011555 USD over the five; 3 to 4 calls against 1).
- Latency: the tree took about four times longer (170.5 against 42.8 s in all; 20.8 to 55.0 s a decision against 2.9 to 15.6).
- The tree's distinct contribution is the deterministic pruning and the visible trace: where a distractor failed the budget (I01, I04) the
  tree pruned it by code before the evaluation (one pruned node each). Five decisions with one distractor each are too few to claim a
  general accuracy difference. The tree is affordable within a run (0.0049 to 0.0061 USD a decision) and bounded (at most 4 calls here),
  and it returns a selection with its reasons when the code cannot settle the hard constraints, which the simple comparison did not.

## The first run (24 October 2026, before the audit) and the abandoned attempt

The first measurement (stamp 20261024T151500; data/e5/20261024T151500; docs/experiments/results/e5_tree_vs_simple_20261024T151500.json)
scored the tree 5 of 5 and the simple comparison 4 of 5 by identity alone, with I05's payments preference seeded hard and a payouts-to-timeline
support row the audit found unentailed; its figures (tree 0.039645 USD and 101.3 s, simple 0.013403 USD and 21.0 s by the rounded
rows; the ledger totals summed unrounded 0.039644 and 0.053047 in all) stand as the historical record and are superseded by the rerun
above. A first attempt the same minute (stamp 20261024T151411) stopped after its first send (0.002193 USD, I01 simple); its run row
(E5-I01-simple) was left running by the script of that day and was closed later through finish_run as failed with the
reason "abandoned: the first E5 attempt ...", never as completed; it stays a rounded 0.002193 USD of the standing cap.
