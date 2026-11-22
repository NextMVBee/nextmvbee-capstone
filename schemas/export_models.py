"""ExportSnapshot: the contract of one plan version's content (Phase 6; docs/addenda/ADDENDUM_06X.md section 3), the one
document plan_versions.snapshot_json holds and the one the document exports read (app/export/document_model.py since Phase 8d; the
Phase 06x content builder before it; nothing else is read). Every item is copied by value with its ids and versions; the optional typed sections of D-31 are present as empty
lists until the Planner enrichment fills them. The status vocabularies are the store's (requirements.status, claims.status,
coverage.evidence_status and processing_status, decisions.status, tree_nodes.hard_status); the production-shaped fixtures of
Phase 06x (tests/fixtures/export/) validate against the same models, so the producer and the consumer agree on the shape.
Unknown keys are refused: a renderer may rely on every key here and on nothing else."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Part(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectMeta(Part):
    id: str
    title: str
    idea: str
    created_at: str
    privacy_mode: str


class PlanMeta(Part):
    id: str
    version: int = Field(ge=1)
    requirement_version: int = Field(ge=1)
    created_at: str
    change_summary: str


class Spend(Part):
    billed: float
    committed: float
    possible: float


class RunMeta(Part):
    id: str
    status: str
    started_at: str | None = None
    ended_at: str | None = None
    spend: Spend
    kind: str | None = None                    # initial or revision (the store's runs.kind); absent in the fixtures
    parent_run_id: str | None = None


class RequirementRecord(Part):
    id: str
    key: str
    value: object
    status: str                                # confirmed, assumed, open, superseded
    source: str                                # user, inferred
    hard: bool
    version: int = Field(ge=1)
    supersedes: str | None = None


class AssumptionRecord(Part):
    id: str
    text: str
    impact: str
    status: str


class QuestionRecord(Part):
    id: str
    text: str
    answer: object = None
    date: str | None = None
    kind: str | None = None                    # value, hardness, confirmation (the store's questions.kind); absent in the fixtures
    requirement_key: str | None = None


class PlanBody(Part):
    problem: str
    users: object                              # the Planner's list of user groups (PlanOut.users); a string in the fixtures
    scope_in: list[str]
    scope_out: list[str]
    user_flow: list[str]
    user_flow_labels: list[str] = Field(default_factory=list)   # D-A511 (Package 19.11 round 2, R1 b): one short label a step, present only where the version holds labels
    risks: list[str]
    unresolved_checks: list[str]


class SupportRecord(Part):
    requirement_id: str
    requirement_version: int = Field(ge=1)
    verdict: str
    claim_id: str | None = None
    status: str | None = None                  # counts or withdrawn (derived); absent in the fixtures
    reason: str | None = None


class Alternative(Part):
    label: str
    strengths: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    meets_hard_constraints: bool
    claim_ids: list[str] = Field(default_factory=list)
    product_id: str | None = None
    product_version: str | None = None


class TreeNode(Part):
    id: str
    parent: str | None = None
    label: str
    depth: int
    score: float | None = None
    hard_status: str
    pruned: bool
    reason: str | None = None
    claim_ids: list[str] = Field(default_factory=list)
    hard_check: list[dict] = Field(default_factory=list)   # Phase 18 package 18.3 (D-A383): what the hard check read behind hard_status (requirement_id, verdict, verdict_id, checked_at, claim_ids); empty for a node of an older store


class ExclusionRecord(Part):
    """Phase 18 package 18.3 (D-A383): one cut of a decision's weighing: the identity a stage left out, the stage, the rule, the reason, the check's date,
    the record it rests on (the row's own id when a run recorded it, the claim or the node otherwise) and its source (run, export, history)."""
    id: str | None = None                      # <project>.EXC-<nnn> when a run recorded the cut
    product_id: str
    product_version: str = ""
    label: str = ""
    stage: str                                 # eligible set, tree, evidence pack, citable gate
    rule: str = ""                             # the D-A id, the pack's omitted reason or the history rule
    reason: str
    checked_at: str | None = None
    record_ids: list[str] = Field(default_factory=list)
    record_count: int | None = None
    source: str                                # run (recorded by the run), export (derived read-only at the build), history (the run's code before D-A151)


class RejectedRecord(Part):
    label: str
    reason: str


class DecisionRecord(Part):
    id: str
    subject: str
    choice: str
    product_id: str | None = None
    product_version: str | None = None
    status: str                                # active, unresolved, stale, rejected
    importance: str
    rationale: str
    claim_ids: list[str] = Field(default_factory=list)
    requirement_ids: list[str] = Field(default_factory=list)
    requirement_support: list[SupportRecord] = Field(default_factory=list)
    alternatives: list[Alternative] = Field(default_factory=list)
    tree_nodes: list[TreeNode] = Field(default_factory=list)
    rejected: list[RejectedRecord] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    plan_version: int | None = None
    memo: dict | None = None                   # Phase 9a (D-A44): the decision memo of the comparison (recommendation, runner_up, why, options, change_mind, open); absent before Phase 9a
    exclusions: list[ExclusionRecord] = Field(default_factory=list)   # Phase 18 package 18.3 (D-A383): every cut of the decision's weighing, per stage
    check_dates: list[dict] = Field(default_factory=list)             # D-A383: per memo option: label, product_id, product_version, verdict_date (the newest counting verdict's created_at), claims_checked_at (the identity's newest checked_at)


class ComponentRecord(Part):
    id: str
    key: str
    name: str
    responsibility: str
    data_leaves_device: bool | None = None
    product_id: str | None = None
    product_version: str | None = None


class Effort(Part):
    min: float | None = None
    max: float | None = None
    unit: str
    text: str


class TaskRecord(Part):
    id: str
    key: str
    title: str
    component_key: str
    depends_on: list[str] = Field(default_factory=list)
    effort: Effort


class AcceptanceRecord(Part):
    id: str
    task_id: str | None = None
    requirement_id: str | None = None
    criterion: str


class TraceEdge(Part):
    from_id: str
    to_id: str
    kind: str


class ClaimRecord(Part):
    id: str
    text: str
    kind: str
    subject: str | None = None
    attribute: str | None = None
    value: str | None = None
    conditions: list[str] = Field(default_factory=list)
    status: str
    scope_status: str
    quote: str | None = None
    source_id: str
    source_version: str | None = None
    range: str | None = None
    product_id: str | None = None
    product_version: str | None = None
    checked_at: str | None = None
    fresh_until: str | None = None


class SourceRecord(Part):
    id: str
    url: str
    canonical_url: str
    title: str | None = None
    publisher: str | None = None
    tier: object = None                        # the store's 1, 2, 3; a word in the fixtures
    published_at: str | None = None
    retrieved_at: str
    freshness_class: str
    fetch_status: str
    source_version: str
    found_by: list[str] = Field(default_factory=list)     # Phase 9b (D-A52): the engines whose results contained the source


class SourceVersionRef(Part):
    source_id: str
    source_version: str


class CoverageRecord(Part):
    subject_type: str                          # requirement, research_question (decision in the fixtures)
    subject_id: str
    subject: str
    evidence_status: str
    processing_status: str
    reason: str
    claim_ids: list[str] = Field(default_factory=list)
    source_versions: list[object] = Field(default_factory=list)   # SourceVersionRef entries from the store; hashes in the fixtures
    requirement_version: int | None = None


class ResearchQuestionRecord(Part):
    id: str
    decision_subject: str
    target_fact: str
    query_text: str | None = None
    run_id: str | None = None


class DerivedTable(Part):
    id: str
    decision_id: str | None = None             # Phase 7 part 2 (D-A28): the cost-bearing decision the table was derived for (the documents place it under that decision's card)
    title: str
    fields: list[str]
    units: dict[str, str] = Field(default_factory=dict)
    labels: dict[str, str] = Field(default_factory=dict)
    rows: list[list[object]]
    claim_ids: list[list[str]]


class ComponentRelation(Part):
    id: str
    from_key: str
    to_key: str
    kind: str
    label: str
    decision_ids: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    status_label: str


class Deployment(Part):
    id: str
    component_key: str
    location: str
    record_id: str | None = None
    status_label: str


class InterfaceStep(Part):
    order: int
    from_: str = Field(alias="from")
    to: str
    message: str
    condition: str | None = None
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Interface(Part):
    id: str
    name: str
    from_key: str
    to_key: str
    direction: str
    kind: str
    description: str
    status_label: str
    steps: list[InterfaceStep] = Field(default_factory=list)


class EntityField(Part):
    name: str
    type: str
    key: str | None = None


class EntityRelation(Part):
    to: str
    cardinality: str
    kind: str


class Entity(Part):
    id: str
    name: str
    fields: list[EntityField]
    relations: list[EntityRelation] = Field(default_factory=list)
    status_label: str


class CodeExample(Part):
    id: str
    title: str
    language: str
    text: str
    status: str                                # tested code, untested example, pseudocode
    decision_ids: list[str] = Field(default_factory=list)


class VerificationRecord(Part):
    """Phase 9a (D-A45): one check of the version's verification plan."""
    id: str
    check: str
    how: str
    pass_: str = Field(alias="pass")
    cost: str
    requirement_id: str | None = None
    risk: str | None = None
    result: str | None = None                  # Phase 18 package 18.3 (D-A342): the pass record of a run of the product on the founder's own input (pass, fail); None until the check ran
    result_at: str | None = None
    result_product_id: str | None = None
    result_product_version: str | None = None
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ExportSnapshot(Part):
    project: ProjectMeta
    plan: PlanMeta
    run: list[RunMeta]
    requirements: list[RequirementRecord]
    assumptions: list[AssumptionRecord]
    questions: list[QuestionRecord]
    plan_body: PlanBody
    decisions: list[DecisionRecord]
    components: list[ComponentRecord]
    tasks: list[TaskRecord]
    acceptance_tests: list[AcceptanceRecord]
    trace_edges: list[TraceEdge]
    claims: list[ClaimRecord]
    sources: list[SourceRecord]
    coverage: list[CoverageRecord]
    derived: list[DerivedTable]
    labels: dict[str, str]
    research_questions: list[ResearchQuestionRecord] = Field(default_factory=list)      # the subjects of the coverage lines beside the requirements (absent in the fixtures)
    claim_requirement_support: list[SupportRecord] = Field(default_factory=list)       # every support record of a claim the snapshot holds (absent in the fixtures)
    component_relations: list[ComponentRelation] = Field(default_factory=list)
    deployment: list[Deployment] = Field(default_factory=list)
    interfaces: list[Interface] = Field(default_factory=list)
    entities: list[Entity] = Field(default_factory=list)
    code_examples: list[CodeExample] = Field(default_factory=list)
    verification: list[VerificationRecord] = Field(default_factory=list)                 # Phase 9a (D-A45): the verification plan of the version
    first_week: list[str] = Field(default_factory=list)                                  # Phase 9a (D-A45): derived in code from the task ladder
    usage: dict | None = None                                                            # Phase 9a (D-A46): the four numbers of the usage question and the founder's words
    budget: dict | None = None                                                           # Phase 9a (D-A46): the budget at the founder's usage (rows, total, ceiling, worst cases)
    lead: dict | None = None                                                             # Phase 9b (C9): the plan on one page's lead with its claim ids, written by the plan model
    editor: dict | None = None                                                           # Phase 9b (D-A55): the Editor's record (rewritten, dropped, merged, flags)
    candidates: list[dict] = Field(default_factory=list)                                 # Phase 9b close (S24, D-A108): the version's eligible typed candidates (id, subject, label, class, aliases), the universe a reader sentence may name
    document_rules: int | None = None                                                    # Phase 19 package 19.7 (D-A465): the generation of the composing rules the version was written under (app/core/docrules.py); absent: generation 1, every version before the package


def validate_snapshot(snapshot: dict) -> ExportSnapshot:
    """The snapshot against the contract; a pydantic ValidationError names what does not fit."""
    return ExportSnapshot.model_validate(snapshot)
