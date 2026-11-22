"""NextMVBee typed records and stage contracts (Pydantic v2).

Status: imported, exported to JSON Schema and round-tripped offline on 6 Oct 2026
with pydantic 2.13.5 (tests/test_workflow.py). Not yet exercised with live model output.
IDs are assigned by application code, never by the model.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


Impact = Literal["low", "medium", "high"]
ClaimKind = Literal["price", "license", "availability", "capability", "version", "other"]
ScopeStatus = Literal["in_scope", "conditional", "out_of_scope", "unchecked"]
CallState = Literal["planned", "sent", "completed", "failed", "unknown"]
PrivacyMode = Literal["cloud", "cloud_zdr"]  # cloud-only; offline is not offered in v1


class ReqStatus(str, Enum):
    confirmed = "confirmed"
    assumed = "assumed"
    open = "open"
    superseded = "superseded"


class ClaimStatus(str, Enum):
    supported = "supported"
    partial = "partial"
    contradicted = "contradicted"
    conflicting = "conflicting"
    insufficient = "insufficient"
    unverified = "unverified"


class Freshness(str, Enum):
    volatile = "volatile"    # prices, model IDs, rate limits, availability
    policy = "policy"        # licenses, retention and privacy terms
    technical = "technical"  # framework docs, release notes
    stable = "stable"        # concepts, protocols


# ---------- durable records (written by code) ----------
class Requirement(Strict):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]+\.REQ-\d{3,}$")  # project-scoped, unique across the database
    project_id: str
    key: str
    value: str | int | float | bool | list[str]
    status: ReqStatus
    source: Literal["user", "inferred"]
    hard: bool = False
    version: int = Field(ge=1)
    supersedes: str | None = None
    applies_to: list[str] | None = None        # Phase 9a (D-A40): the decision subjects a hard requirement binds; None binds every subject


class Assumption(Strict):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]+\.ASM-\d{3,}$")  # project-scoped, unique across the database
    project_id: str
    text: str
    impact: Impact
    status: Literal["open", "confirmed", "rejected"] = "open"
    question_id: str | None = None


class OpenQuestion(Strict):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]+\.Q-\d{3,}$")  # project-scoped, unique across the database
    project_id: str
    text: str = Field(max_length=300)
    why_blocking: str = Field(max_length=300)
    status: Literal["open", "answered", "skipped"] = "open"
    answer_requirement_id: str | None = None
    kind: Literal["value", "hardness"] = "value"   # Phase 05b (C12): a value question asks for the requirement's value; a hardness question asks whether the founder states it hard


class Source(Strict):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]+\.SRC-\d{3,}$")  # project-scoped, unique across the database
    project_id: str
    url: str
    canonical_url: str
    title: str | None = None
    publisher: str | None = None
    tier: Literal[1, 2, 3]
    published_at: datetime | None = None
    retrieved_at: datetime
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    freshness: Freshness
    fetch_status: Literal["ok", "partial", "failed", "blocked"] = "ok"
    injection_flag: bool = False
    cache_status: Literal["livecrawl", "cached", "cache_fallback", "unknown"] = "unknown"
    max_age_hours: int | None = None


class Claim(Strict):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]+\.CLM-\d{3,}$")  # project-scoped, unique across the database
    project_id: str
    text: str
    kind: ClaimKind
    source_id: str | None = None
    quote: str | None = Field(default=None, max_length=600)
    status: ClaimStatus = ClaimStatus.unverified
    impact: Impact = "medium"
    checked_at: datetime | None = None
    fresh_until: datetime | None = None
    subject: str | None = None          # atomic claim: what the claim is about (product and version)
    attribute: str | None = None        # one capability, price or term
    value: str | None = None
    conditions: list[str] = Field(default_factory=list)   # every limit stated by the source; a response carries 5, continuation accumulates the rest (Phase 03b)
    scope_status: ScopeStatus = "unchecked"


class RejectedOption(Strict):
    option: str
    reason: str


class Decision(Strict):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]+\.DEC-\d{3,}$")  # project-scoped, unique across the database
    project_id: str
    subject: str
    scope: Literal["product", "operating"] = "product"
    choice: str
    rationale: str = Field(max_length=600)
    importance: Impact
    requirement_ids: list[str]
    claim_ids: list[str] = Field(default_factory=list)
    rejected: list[RejectedOption] = Field(default_factory=list)
    status: Literal["active", "stale", "rejected", "unresolved"] = "active"
    plan_version: int = 1
    run_id: str | None = None         # Phase 18 package 18.1 (D-A302): the run that wrote the decision; None keeps what the row holds


class UsageEntry(Strict):
    id: str
    run_id: str
    step_key: str
    kind: Literal["model", "search", "contents"]
    model_id: str | None = None
    provider: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    reserved_usd: float = Field(ge=0)
    reported_usd: float | None = None
    cost_uncertain: bool = False
    latency_ms: int | None = None
    attempt: int = Field(default=1, ge=1)
    result_json: str | None = None       # stored validated output, replayed instead of resending
    state: CallState = "planned"
    resolution: Literal["retried", "skipped", "reconciled"] | None = None


class Project(Strict):
    """The container every record belongs to; its id is the prefix of every record id in the project (no dot)."""
    id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    owner_id: str
    title: str = Field(max_length=200)         # Phase 17 package 17.3 (D-A277): the schema's maximum; the title is stored whole up to it, shortened at a word boundary by app.core.textlimit.title_of, never cut inside a word (devA's title was stored cut at 80)
    privacy_mode: PrivacyMode = "cloud"
    retention_days: int | None = Field(default=None, ge=1)   # None: keep until the founder deletes the project
    requirement_version: int = Field(default=1, ge=1)


class Component(Strict):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]+\.CMP-\d{3,}$")  # project-scoped, unique across the database
    project_id: str
    name: str
    responsibility: str
    data_leaves_device: bool = False
    status: Literal["current", "stale"] = "current"


class Task(Strict):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]+\.TSK-\d{3,}$")  # project-scoped, unique across the database
    project_id: str
    component_id: str
    title: str
    depends_on: list[str] = Field(default_factory=list)
    effort_low_h: float | None = Field(default=None, ge=0)
    effort_high_h: float | None = Field(default=None, ge=0)
    status: Literal["current", "stale"] = "current"


class AcceptanceTest(Strict):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]+\.ACC-\d{3,}$")  # project-scoped, unique across the database
    project_id: str
    task_id: str | None = None
    requirement_id: str | None = None
    criterion: str
    status: Literal["current", "stale"] = "current"


class PlanVersion(Strict):
    """One exported plan state; keyed by (project_id, version), the display number is the version. Phase 6: snapshot_sig is the
    signature of the version's content snapshot (None for a version created before Phase 6: "no snapshot"); approved_by,
    approved_at and approval_ref are the approval act of D-A13 bound to it and content_sig the ContentDoc signatures per family
    (D-A7, a JSON object), None until the owner approves the version for document export. The repository reads them; it never
    writes them through upsert_plan_version."""
    project_id: str
    version: int = Field(ge=1)
    requirement_version: int = Field(ge=1)
    change_summary: str
    diff: dict = Field(default_factory=dict)
    export_path: str | None = None
    snapshot_sig: str | None = None
    approved_by: str | None = None
    approved_at: str | None = None
    approval_ref: str | None = None
    content_sig: str | None = None


# ---------- stage contracts (returned by the model, validated by code) ----------
class RequirementDraft(Strict):
    key: str
    value: str
    source: Literal["user", "inferred"]
    hard: bool = False


class QuestionDraft(Strict):
    text: str = Field(max_length=300)
    why_blocking: str = Field(max_length=300)
    requirement_key: str


class ClarifyOut(Strict):
    requirements: list[RequirementDraft]
    assumptions: list[str] = Field(default_factory=list, max_length=8)
    contradictions: list[str] = Field(default_factory=list, max_length=5)
    questions: list[QuestionDraft] = Field(default_factory=list, max_length=3)
    ready_for_research: bool
    failure_reason: str | None = Field(default=None, max_length=300)  # set only when the stage cannot comply


class QueryDraft(Strict):
    text: str = Field(max_length=200)
    decision_subject: str
    target_fact: str = Field(max_length=200)
    include_domains: list[str] = Field(default_factory=list, max_length=10)
    recency_days: int | None = Field(default=None, ge=1, le=3650)
    freshness: Freshness = Freshness.technical   # decides Exa maxAgeHours (app/core/freshness.py)


class ResearchPlanOut(Strict):
    decision_subjects: list[str] = Field(max_length=10)
    queries: list[QueryDraft] = Field(max_length=8)
    failure_reason: str | None = Field(default=None, max_length=300)  # set only when the stage cannot comply


class ExtractedClaim(Strict):
    text: str
    kind: ClaimKind
    source_id: str
    quote: str = Field(max_length=600)
    decision_subject: str
    subject: str = ""                 # product and version the claim is about
    attribute: str = ""               # exactly one capability, price or term
    value: str = ""
    conditions: list[str] = Field(default_factory=list, max_length=5)  # every limit the source states; at most 5 per response
    more_conditions: bool = False     # Phase 03b: the source states further conditions for this claim; a follow-up call collects them
    vendor: str = ""                  # Phase 4: explicit product identity (vendor, product, version), normalised by code into product_id and product_version
    product: str = ""
    product_version: str = ""
    access_route: str = Field(default="", max_length=120)             # Phase 18 package 18.1 (D-A300): the plan, endpoint, batch or deployment the claim speaks about: a facet of the product identity, never an identity of its own
    identity_kind: Literal["product", "approach"] = "product"          # D-A300: product, or approach for a way of using the product; the identity stays the product's


class EvidenceExtractionOut(Strict):
    claims: list[ExtractedClaim] = Field(max_length=12)
    injection_suspected: bool = False
    more_claims: bool = False         # Phase 03b: the batch holds further claims; the same batch is continued with the identities already recorded
    failure_reason: str | None = Field(default=None, max_length=300)  # set only when the stage cannot comply


class ConditionContinuationOut(Strict):
    """Phase 03b: the remaining conditions of one claim, asked against the same source_version and range."""
    claim_id: str
    conditions: list[str] = Field(default_factory=list, max_length=5)
    more_conditions: bool = False
    failure_reason: str | None = Field(default=None, max_length=300)


class VerificationOut(Strict):
    claim_id: str
    verdict: Literal["supported", "partial", "contradicted", "conflicting", "insufficient"]
    quote: str | None = Field(default=None, max_length=600)
    reason: str = Field(max_length=400)
    scope_matches_claim: bool = False      # the passage covers this subject, version, language, tier or region
    conditions_found: list[str] = Field(default_factory=list, max_length=5)
    version_or_date: str | None = None
    requirements_entailed: list[str] = Field(default_factory=list, max_length=10)   # Phase 4: ids of the listed hard requirements the quote entails for this claim within scope; persisted per requirement version
    failure_reason: str | None = Field(default=None, max_length=300)  # set only when the stage cannot comply


class ExtractedClaimV2(ExtractedClaim):
    """Phase 05b (C2): a claim names the subjects of the send it bears on; the application validates every id against the subject list and refuses a claim without a valid one (refused_unlinked)."""
    subject_ids: list[str] = Field(default_factory=list, max_length=20)   # C2: the application validates the ids; a claim with none valid (an empty list included) is refused and counted, never a schema repair


class EvidenceExtractionOutV2(Strict):
    """Phase 05b (C2, D-17): the version 2 extraction contract; EvidenceExtractionOut (version 1) stays for the replay of stored runs."""
    claims: list[ExtractedClaimV2] = Field(max_length=12)
    injection_suspected: bool = False
    more_claims: bool = False
    failure_reason: str | None = Field(default=None, max_length=300)  # set only when the stage cannot comply


class ClaimVerdict(Strict):
    """One verdict of a verification group (Phase 05b, C5): the fields of the version 1 verdict, keyed by the claim id it judges."""
    claim_id: str
    verdict: Literal["supported", "partial", "contradicted", "conflicting", "insufficient"]
    quote: str | None = Field(default=None, max_length=600)
    reason: str = Field(max_length=400)
    scope_matches_claim: bool = False
    conditions_found: list[str] = Field(default_factory=list, max_length=5)
    version_or_date: str | None = None
    requirements_entailed: list[str] = Field(default_factory=list, max_length=10)


class VerificationOutV2(Strict):
    """Phase 05b (C5, D-17): one verdict per claim id of the group sent; VerificationOut (version 1) stays for the replay of stored runs."""
    verdicts: list[ClaimVerdict] = Field(default_factory=list, max_length=20)
    failure_reason: str | None = Field(default=None, max_length=300)  # set only when the stage cannot comply


class OptionAssessment(Strict):
    option: str
    product_id: str = ""              # Phase 4: the product id of the candidate as listed (exact identity; citations bind on it)
    product_version: str = ""
    meets_hard_constraints: bool
    claim_ids: list[str]
    strengths: list[str] = Field(max_length=4)
    risks: list[str] = Field(max_length=4)


class ComparisonOut(Strict):
    decision_subject: str
    options: list[OptionAssessment] = Field(max_length=3)   # may be empty when failure_reason is set
    recommended: str | None
    rationale: str = Field(max_length=600)
    unresolved: list[str] = Field(default_factory=list)
    failure_reason: str | None = Field(default=None, max_length=300)  # set only when the stage cannot comply


class ComponentDraft(Strict):
    key: str
    name: str
    responsibility: str
    decision_ids: list[str]
    data_leaves_device: bool


class TaskDraft(Strict):
    key: str
    component_key: str
    title: str
    depends_on: list[str] = Field(default_factory=list)
    effort_hours_low: float = Field(ge=0)
    effort_hours_high: float = Field(ge=0)


class AcceptanceDraft(Strict):
    task_key: str
    requirement_id: str
    criterion: str


FLOW_LABEL_WORDS, FLOW_LABEL_CHARS = 4, 40          # D-A511 (Package 19.11 round 2, R1 b): a step's label is the user's action in at most four words


def flow_labels(labels, steps) -> list[str]:
    """D-A511: the labels of the user flow's steps as the contract holds them: one a step, in the steps' order, each at most FLOW_LABEL_WORDS
    words and FLOW_LABEL_CHARS characters. A label over the bound, a label that is no text and a label beyond the last step are left out
    (the step is then a step without a label, which is valid), so an answer is never refused for its labels; no label at all is the empty
    list. The bound is the contract's, enforced here and never by the prompt's words."""
    out = []
    for i in range(len(steps or [])):
        x = labels[i] if isinstance(labels, list) and i < len(labels) else ""
        x = " ".join(x.split()).rstrip(".") if isinstance(x, str) else ""
        out.append(x if x and len(x.split()) <= FLOW_LABEL_WORDS and len(x) <= FLOW_LABEL_CHARS else "")
    return out if any(out) else []


class PlanOut(Strict):
    problem: str
    users: list[str]
    scope_in: list[str]
    scope_out: list[str]
    user_flow: list[str]
    user_flow_labels: list[str] = Field(default_factory=list)   # D-A511: one short label a step (the user's action); optional, bounded by flow_labels
    components: list[ComponentDraft]
    tasks: list[TaskDraft]
    acceptance: list[AcceptanceDraft]
    risks: list[str]
    unresolved_checks: list[str]
    failure_reason: str | None = Field(default=None, max_length=300)  # set only when the stage cannot comply

    @field_validator("user_flow_labels", mode="before")
    @classmethod
    def _label_texts(cls, v):
        return [x if isinstance(x, str) else "" for x in v] if isinstance(v, list) else []   # D-A511: an answer is never refused for its labels: what is no list of texts is read as no label

    @model_validator(mode="after")
    def _labels(self):
        self.user_flow_labels = flow_labels(self.user_flow_labels, self.user_flow)
        return self


class PlanScopeSection(Strict):
    """Phase 03b task 5: when the plan ceiling is reached, the plan is produced in sections and assembled by code."""
    problem: str
    users: list[str]
    scope_in: list[str]
    scope_out: list[str]
    user_flow: list[str]
    user_flow_labels: list[str] = Field(default_factory=list)   # D-A511: as PlanOut's
    risks: list[str]
    unresolved_checks: list[str]
    failure_reason: str | None = Field(default=None, max_length=300)

    @field_validator("user_flow_labels", mode="before")
    @classmethod
    def _label_texts(cls, v):
        return [x if isinstance(x, str) else "" for x in v] if isinstance(v, list) else []   # D-A511: an answer is never refused for its labels: what is no list of texts is read as no label

    @model_validator(mode="after")
    def _labels(self):
        self.user_flow_labels = flow_labels(self.user_flow_labels, self.user_flow)
        return self


class PlanComponentsSection(Strict):
    components: list[ComponentDraft]
    failure_reason: str | None = Field(default=None, max_length=300)


class PlanTasksSection(Strict):
    tasks: list[TaskDraft]
    failure_reason: str | None = Field(default=None, max_length=300)


class PlanAcceptanceSection(Strict):
    acceptance: list[AcceptanceDraft]
    failure_reason: str | None = Field(default=None, max_length=300)


PLAN_SECTIONS = {"scope": PlanScopeSection, "components": PlanComponentsSection, "tasks": PlanTasksSection, "acceptance": PlanAcceptanceSection}


class RevisionOut(Strict):
    decision_ids_to_redo: list[str]
    reuse_claim_ids: list[str]
    new_queries: list[QueryDraft] = Field(default_factory=list, max_length=4)
    change_summary: str = Field(max_length=600)
    scope_questions: list[QuestionDraft] = Field(default_factory=list, max_length=2)  # ask, never widen or narrow a requirement
    consistency_flags: list[str] = Field(default_factory=list, max_length=10)
    failure_reason: str | None = Field(default=None, max_length=300)  # set only when the stage cannot comply


STAGE_CONTRACTS = {
    "clarify": ClarifyOut,
    "research_plan": ResearchPlanOut,
    "extract_evidence": EvidenceExtractionOutV2,   # Phase 05b: version 2 (subject ids); version 1 stays for replay
    "compare_options": ComparisonOut,
    "plan_generate": PlanOut,
    "verify": VerificationOutV2,                  # Phase 05b: version 2 (one verdict per claim id of a group); version 1 stays for replay
    "revise": RevisionOut,
}


# ---------------------------------------------------------------- course concepts: tools, reasoning, workers, exploration
ToolName = Literal["search", "read", "re_fetch", "ask_user"]


class SearchArgs(Strict):
    query: str = Field(min_length=3, max_length=200)
    freshness: Freshness = Freshness.technical
    num_results: int = Field(default=8, ge=1, le=10)
    language: str = Field(default="en", max_length=8)   # Phase 9a (D-A50): the query's language; part of the request fingerprint (the search cache key) and the tool row
    search_type: Literal["auto", "deep"] | None = None   # Phase 9b (D-A52): deep for candidate discovery with the planner's variants as additionalQueries; auto (the default) for follow-ups
    additional_queries: list[str] | None = Field(default=None, max_length=10)


class ReadArgs(Strict):
    """read: fetch a page by url (page quota, Exa), or read stored text by source_id and source_version at an offset or
    for a query (no cost, stage tool limit only). Exactly one of url or source_id (Phase 03b task 2)."""
    url: str | None = Field(default=None, pattern=r"^https?://", max_length=2000)
    freshness: Freshness = Freshness.technical
    source_id: str | None = Field(default=None, max_length=120)
    source_version: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    offset: int = Field(default=0, ge=0)
    query: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _one_target(self):
        if (self.url is None) == (self.source_id is None):
            raise ValueError("read needs exactly one of url (fetch) or source_id (stored text)")
        if self.url is not None and (self.offset or self.query is not None or self.source_version is not None):
            raise ValueError("offset, query and source_version apply to stored reads (source_id), not to a url fetch")
        if self.source_id is not None and self.source_version is None:
            raise ValueError("a stored read names the source_version it expects")
        return self


class ReFetchArgs(Strict):
    url: str = Field(pattern=r"^https?://", max_length=2000)


class AskUserArgs(Strict):
    text: str = Field(max_length=300)
    why_blocking: str = Field(max_length=200)
    requirement_key: str = Field(max_length=80)


TOOL_ARGS = {"search": SearchArgs, "read": ReadArgs, "re_fetch": ReFetchArgs, "ask_user": AskUserArgs}


class DiscoveredCandidate(Strict):
    """Phase 9b (D-A52): a lead from OpenRouter's agentic web search (the Perplexity or the Parallel engine): never evidence."""
    vendor: str = Field(max_length=80)
    product: str = Field(max_length=120)
    plan_or_mode: str = Field(default="", max_length=80)
    product_class: str = Field(max_length=60)
    urls: list[str] = Field(default_factory=list, max_length=5)
    why: str = Field(default="", max_length=300)


class DiscoveryOut(Strict):
    """Phase 9b (D-A52): the candidates an agentic search pass found for one subject, and every URL it read."""
    candidates: list[DiscoveredCandidate] = Field(default_factory=list, max_length=20)
    urls_read: list[str] = Field(default_factory=list, max_length=40)
    failure_reason: str | None = Field(default=None, max_length=300)


class PanelStance(Strict):
    model: str = Field(max_length=80)
    stance: str = Field(max_length=300)


class PanelContradiction(Strict):
    topic: str = Field(max_length=200)
    stances: list[PanelStance] = Field(default_factory=list, max_length=8)


class PanelPartial(Strict):
    models: list[str] = Field(default_factory=list, max_length=8)
    point: str = Field(max_length=300)


class PanelInsight(Strict):
    model: str = Field(max_length=80)
    insight: str = Field(max_length=300)


class PanelProduct(Strict):
    vendor: str = Field(max_length=80)
    product: str = Field(max_length=120)
    product_class: str = Field(max_length=60)
    why: str = Field(default="", max_length=300)


class PanelResponse(Strict):
    """Phase 11 package 11.2 (D-A212): one analysis model's response as the panel recorded it: substantive (it answered the question with
    facts), tool_unavailable (it reported that the fusion tool or a tool it needed was unavailable), error (it failed) or empty (no answer)."""
    model: str = Field(max_length=120)
    outcome: Literal["substantive", "tool_unavailable", "error", "empty"]
    note: str = Field(default="", max_length=200)


class PanelOut(Strict):
    """Phase 9b (D-A52): the fusion panel's analysis, copied by the model into the answer: consensus, contradictions, partial
    coverage, unique insights, blind spots, and every product the panel named; code turns a named product that is not a
    candidate into a typed candidate and a contradiction into a research question; nothing here is evidence."""
    status: str = Field(default="ok", max_length=40)
    consensus: list[str] = Field(default_factory=list, max_length=12)
    contradictions: list[PanelContradiction] = Field(default_factory=list, max_length=8)
    partial_coverage: list[PanelPartial] = Field(default_factory=list, max_length=8)
    unique_insights: list[PanelInsight] = Field(default_factory=list, max_length=8)
    blind_spots: list[str] = Field(default_factory=list, max_length=8)
    products_named: list[PanelProduct] = Field(default_factory=list, max_length=12)
    models: list[str] = Field(default_factory=list, max_length=8)
    responses: list[PanelResponse] = Field(default_factory=list, max_length=8)      # D-A212: each analysis model's response outcome, from the panel's recorded responses; code counts the substantive answers
    failure_reason: str | None = Field(default=None, max_length=300)


class ToolObservation(Strict):
    tool_call_id: str
    tool: ToolName
    ok: bool
    summary: str = Field(max_length=600)
    error: str | None = None


class DecisionSummary(Strict):
    """Structured decision summary recorded per step: never raw chain of thought."""
    action_id: str = Field(min_length=1)
    purpose: str = Field(min_length=1, max_length=200)
    action: str = Field(min_length=1, max_length=200)
    observation: str = Field(min_length=1, max_length=300)
    change: str = Field(min_length=1, max_length=200)


class EvidenceProposal(Strict):
    """What a research worker returns; the coordinator validates and merges."""
    worker: str
    task_id: str
    source_url: str = Field(pattern=r"^https?://")
    quote: str = Field(max_length=600)
    claim_text: str
    kind: ClaimKind
    subject: str = ""
    attribute: str = ""
    value: str = ""
    conditions: list[str] = Field(default_factory=list, max_length=5)
    vendor: str = ""                                   # Phase 4: explicit product identity, part of the merge identity
    product: str = ""
    product_version: str = ""
    writes: list[str] = Field(default_factory=list)   # record ids a worker proposes to change; confirmed records are refused


class Alternative(Strict):
    label: str = Field(max_length=120)
    summary: str = Field(max_length=400)
    claim_ids: list[str] = Field(default_factory=list, max_length=10)
    product_id: str = Field(default="", max_length=120)                 # Phase 18 package 18.1 (D-A301, tree_alternatives.v2): the identity copied from the claims passed; code refuses a family slug where a versioned identity of the same vendor is among the claims, and resolves an empty or unknown pair to the identity most of the cited claims name (a stored v1 answer loads with the defaults)
    product_version: str = Field(default="", max_length=80)


class AlternativesOut(Strict):
    decision_subject: str
    alternatives: list[Alternative] = Field(max_length=3)
    failure_reason: str | None = Field(default=None, max_length=300)


class Variant(Strict):
    """Phase 18 package 18.1 (D-A301, tree_variants.v2): a variant of one alternative: kind product (a tier or version of the same product, a
    scored node of its own) or route or approach (an access route or a way of using the parent identity: a facet folded into the parent,
    never a scored sibling); access_route names the route. A stored v1 answer loads with the defaults (product, '')."""
    label: str = Field(max_length=120)
    summary: str = Field(max_length=400)
    claim_ids: list[str] = Field(default_factory=list, max_length=10)
    kind: Literal["product", "route", "approach"] = "product"
    access_route: str = Field(default="", max_length=120)


class VariantsOut(Strict):
    alternative_label: str
    variants: list[Variant] = Field(max_length=3)
    failure_reason: str | None = Field(default=None, max_length=300)


class BranchEvaluation(Strict):
    branch_id: str
    fit: int = Field(ge=0, le=10)          # meets the requirements, from the evidence cited
    evidence: int = Field(ge=0, le=10)     # strength and scope of supporting claims
    cost: int = Field(ge=0, le=10)         # 10 = cheapest
    risk: int = Field(ge=0, le=10)         # 10 = lowest risk
    claim_ids: list[str] = Field(default_factory=list, max_length=10)
    notes: str = Field(max_length=300)


class BranchEvaluationOut(Strict):
    evaluations: list[BranchEvaluation] = Field(max_length=12)
    failure_reason: str | None = Field(default=None, max_length=300)


TREE_CONTRACTS = {"alternatives": AlternativesOut, "variants": VariantsOut, "evaluate": BranchEvaluationOut}

# ---------------------------------------------------------------- Phase 9a contracts (D-A40 to D-A45): appended to schemas/models.py
class SubjectDraft(Strict):
    """Phase 9a (D-A41): a decision subject with its product class."""
    name: str = Field(max_length=120)
    product_class: str = Field(max_length=60)


class CandidateDraft(Strict):
    """Phase 9a (D-A41, D-A50): a typed candidate of a subject; specialised_for names the language it is specialised in, when one.
    Phase 18 package 18.1 (D-A299): the identity beside the label: kind (product or approach), vendor, product, version and access_route
    (the plan, tier, endpoint or deployment: a facet of the identity, never a candidate of its own); code composes the label from the vendor
    and the product when both are given and splits a bare label otherwise (app.core.discovery.insert_candidate)."""
    subject: str = Field(max_length=120)
    label: str = Field(max_length=120)
    product_class: str = Field(max_length=60)
    specialised_for: str | None = Field(default=None, max_length=20)
    kind: Literal["product", "approach"] = "product"
    vendor: str = Field(default="", max_length=80)
    product: str = Field(default="", max_length=120)
    version: str = Field(default="", max_length=80)
    access_route: str = Field(default="", max_length=120)


class ApplicabilityDraft(Strict):
    """Phase 9a (D-A40): the subjects a hard requirement binds, with a one-line reason."""
    requirement_id: str
    subjects: list[str] = Field(default_factory=list, max_length=10)
    reason: str = Field(max_length=200)


class QueryDraftV2(QueryDraft):
    """Phase 9a (D-A50): a query carries the language it is written in (a code such as en or he)."""
    language: str = Field(default="en", max_length=8)


class ResearchPlanOutV2(Strict):
    """Phase 9a (D-A40, D-A41, D-A50): research_plan.v2; ResearchPlanOut (version 1) stays for the replay of stored runs."""
    decision_subjects: list[str] = Field(max_length=10)
    subjects: list[SubjectDraft] = Field(default_factory=list, max_length=10)
    candidates: list[CandidateDraft] = Field(default_factory=list, max_length=40)
    applicability: list[ApplicabilityDraft] = Field(default_factory=list, max_length=10)
    queries: list[QueryDraftV2] = Field(max_length=8)
    failure_reason: str | None = Field(default=None, max_length=300)


class CoverageOut(Strict):
    """Phase 9a (D-A42): the coverage step's verdict for one product and one hard requirement (coverage.v1); Phase 11 package 11.1 (D-A190):
    contradicts, the fourth value, a proven violation under the disqualification rule (coverage.v3); none stays not proven."""
    verdict: Literal["entails", "partial", "none", "contradicts"]
    claim_ids_used: list[str] = Field(default_factory=list, max_length=20)
    note: str = Field(default="", max_length=300)
    failure_reason: str | None = Field(default=None, max_length=300)


class MemoWhy(Strict):
    text: str = Field(max_length=400)
    claim_ids: list[str] = Field(default_factory=list, max_length=10)


class MemoOption(Strict):
    option: str = Field(max_length=120)
    product_id: str = ""
    product_version: str = ""
    fit: str = Field(default="", max_length=300)
    price: str = Field(default="", max_length=200)
    monthly_cost: str = Field(default="", max_length=120)
    risk: str = Field(default="", max_length=300)
    verdict: Literal["recommended", "runner-up", "bake-off", "watch", "later", "not now", "rejected"] = "watch"
    claim_ids: list[str] = Field(default_factory=list, max_length=12)


class MemoPick(Strict):
    option: str = Field(max_length=120)
    product_id: str = ""
    product_version: str = ""


class ComparisonOutV2(Strict):
    """Phase 9a (D-A44): compare_options.v5, the decision memo; ComparisonOut (version 1 to 4) stays for the replay of stored runs."""
    decision_subject: str
    recommendation: MemoPick | None = None
    runner_up: MemoPick | None = None
    why: list[MemoWhy] = Field(default_factory=list, max_length=4)
    options: list[MemoOption] = Field(default_factory=list, max_length=6)
    change_mind: list[str] = Field(default_factory=list, max_length=4)
    open: list[str] = Field(default_factory=list, max_length=6)
    failure_reason: str | None = Field(default=None, max_length=300)


class EntityFieldDraft(Strict):
    name: str = Field(max_length=60)
    type: str = Field(max_length=40)
    key: str | None = Field(default=None, max_length=20)


class EntityRelationDraft(Strict):
    to: str = Field(max_length=60)
    cardinality: str = Field(max_length=20)
    kind: str = Field(max_length=40)


class EntityDraft(Strict):
    name: str = Field(max_length=60)
    fields: list[EntityFieldDraft] = Field(default_factory=list, max_length=16)
    relations: list[EntityRelationDraft] = Field(default_factory=list, max_length=8)


class InterfaceStepDraft(Strict):
    order: int = Field(ge=1)
    from_key: str = Field(max_length=60)
    to_key: str = Field(max_length=60)
    message: str = Field(max_length=200)
    condition: str | None = Field(default=None, max_length=200)


class InterfaceDraft(Strict):
    name: str = Field(max_length=80)
    from_key: str = Field(max_length=60)
    to_key: str = Field(max_length=60)
    direction: str = Field(default="request", max_length=20)
    kind: str = Field(default="call", max_length=40)
    description: str = Field(max_length=400)
    steps: list[InterfaceStepDraft] = Field(default_factory=list, max_length=8)


class ComponentRelationDraft(Strict):
    from_key: str = Field(max_length=60)
    to_key: str = Field(max_length=60)
    kind: str = Field(max_length=40)
    label: str = Field(max_length=120)


class EnrichmentOut(Strict):
    """Phase 9a (D-A45): enrich.v1, the planner enrichment of a plan version."""
    entities: list[EntityDraft] = Field(default_factory=list, max_length=8)
    interfaces: list[InterfaceDraft] = Field(default_factory=list, max_length=8)
    component_relations: list[ComponentRelationDraft] = Field(default_factory=list, max_length=12)
    failure_reason: str | None = Field(default=None, max_length=300)


class LeadDraft(Strict):
    """Phase 9b (C9): the plan on one page's lead, the system's synthesis (what we build, with which picks, for how much a month, the one risk that matters), with its claim ids."""
    text: str = Field(max_length=700)
    claim_ids: list[str] = Field(default_factory=list, max_length=8)


class TaskStepDraft(Strict):
    text: str = Field(max_length=160)
    hours: float = Field(ge=0.5, le=40)


class TaskStepsDraft(Strict):
    """Phase 9b (C8): two to four concrete sub-steps with effort for one task of the plan."""
    task_key: str = Field(max_length=60)
    steps: list[TaskStepDraft] = Field(min_length=1, max_length=4)


class EnrichmentOutV2(Strict):
    """Phase 9b (C8, C9): enrich.v2, the planner enrichment with the lead and the tasks' sub-steps; EnrichmentOut (version 1) stays for stored runs."""
    entities: list[EntityDraft] = Field(default_factory=list, max_length=8)
    interfaces: list[InterfaceDraft] = Field(default_factory=list, max_length=8)
    component_relations: list[ComponentRelationDraft] = Field(default_factory=list, max_length=12)
    lead: LeadDraft | None = None
    task_steps: list[TaskStepsDraft] = Field(default_factory=list, max_length=20)
    failure_reason: str | None = Field(default=None, max_length=300)


class EditedField(Strict):
    path: str = Field(max_length=80)
    text: str = Field(max_length=1200)   # the input field carries a third key, kind, its provenance class (editor.v3, D-A390); the Editor returns the path and the text


class MergedFields(Strict):
    paths: list[str] = Field(min_length=2, max_length=8)
    text: str = Field(max_length=1200)


class EditorFlag(Strict):
    path: str = Field(max_length=80)
    issue: str = Field(max_length=300)


class EditorOut(Strict):
    """Phase 9b (D-A55): editor.v1, the senior editor's pass over the version's reader fields: rewrites by path, duplicates merged, inconsistencies flagged.
    Code checks every rewrite (the same claim ids, the same numbers, no product outside the candidates, no new fact, no em dash, at most 1.3 times the length)."""
    fields: list[EditedField] = Field(default_factory=list, max_length=120)
    merged: list[MergedFields] = Field(default_factory=list, max_length=20)
    flags: list[EditorFlag] = Field(default_factory=list, max_length=30)
    failure_reason: str | None = Field(default=None, max_length=300)


class DroppedField(Strict):
    """Phase 17 package 17.3 (D-A55 amended, D-A279): editor.v2, a field the Editor leaves out of the document with its reason (irrelevant to the family's purpose, a duplicate of path, superseded by path)."""
    path: str = Field(max_length=80)
    reason: str = Field(max_length=300)


class EditorFlagV2(Strict):
    """editor.v2: a flag with its kind (contradiction, outdated, source_error, presentation), the issue and the records involved; editor.v3 (Phase 17 package 17.4, D-A390): the kind provenance, a text that states a proposal or an assumption as a fact, or a fact as a guess."""
    path: str = Field(max_length=80)
    kind: str = Field(default="presentation", max_length=20)   # contradiction, outdated, source_error, presentation, provenance (an unknown word is read as presentation by code)
    issue: str = Field(max_length=400)
    records: list[str] = Field(default_factory=list, max_length=8)


class EditorAssessment(Strict):
    """editor.v2: one paragraph per family: what repeated, what was irrelevant, what contradicted, what is outdated, what was left for the founder."""
    family: str = Field(max_length=20)
    text: str = Field(max_length=1600)


class SummaryStatement(Strict):
    """editor.v2: one statement of the plan on one page, the shortest complete statement of the field at path (derived copy under rewrite_ok)."""
    path: str = Field(max_length=80)
    text: str = Field(max_length=400)


class EditorOutV2(Strict):
    """Phase 17 package 17.3 (D-A55 amended, D-A279): editor.v2, the Editor's pass over the whole reader content of a version: the rewritten fields,
    the merges, the drops with their reasons, the flags with their kinds, the assessment per family and the one page's statements. Code applies the
    hard checks to every rewrite, merge and drop before anything is stored (app/core/editor.py)."""
    fields: list[EditedField] = Field(default_factory=list, max_length=200)
    merged: list[MergedFields] = Field(default_factory=list, max_length=30)
    dropped: list[DroppedField] = Field(default_factory=list, max_length=40)
    flags: list[EditorFlagV2] = Field(default_factory=list, max_length=40)
    assessment: list[EditorAssessment] = Field(default_factory=list, max_length=2)
    summary: list[SummaryStatement] = Field(default_factory=list, max_length=12)
    failure_reason: str | None = Field(default=None, max_length=300)


class VerificationCheckDraft(Strict):
    check: str = Field(max_length=200)
    how: str = Field(max_length=600)           # Phase 17 package 17.3 (D-A277): 600 whole-sentence characters; a text at the limit without a sentence end is a cut and the check is dropped at the store write, never cut again
    pass_rule: str = Field(max_length=300)     # the pass criterion (MODEL.md's "pass"; a Python keyword, so the wire field is pass_rule)
    cost: str = Field(max_length=120)
    requirement_id: str | None = None
    risk: str | None = Field(default=None, max_length=300)


class VerificationPlanOut(Strict):
    """Phase 9a (D-A45): verify_plan.v1, the verification plan of a plan version."""
    checks: list[VerificationCheckDraft] = Field(default_factory=list, max_length=10)
    failure_reason: str | None = Field(default=None, max_length=300)

STAGE_CONTRACTS.update({"research_plan": ResearchPlanOutV2, "compare_options": ComparisonOutV2, "coverage": CoverageOut, "enrich": EnrichmentOutV2, "verify_plan": VerificationPlanOut,
                        "discover": DiscoveryOut, "panel": PanelOut, "editor": EditorOutV2})   # Phase 9a (D-17): the current versions; the older contracts stay for the replay of stored runs; Phase 9b: enrich.v2, discover, panel, editor; Phase 17 package 17.3: editor.v2 (EditorOutV2; EditorOut stays for the stored runs)
