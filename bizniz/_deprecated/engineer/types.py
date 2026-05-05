from typing import Optional, List, Literal, Dict
from pydantic import BaseModel, Field


# ── Architecture Planning ──────────────────────────────────────────────────────

class DomainModelField(BaseModel):
    name: str
    type_hint: str
    description: str = ""


class MethodSignature(BaseModel):
    name: str
    signature: str  # e.g. "def total(self) -> float"
    description: str = ""


class DomainModelDefinition(BaseModel):
    db_id: Optional[int] = None
    class_name: str
    filepath: str
    namespace_path: str = ""
    fields: List[DomainModelField] = []
    methods: List[MethodSignature] = []
    docstring: str = ""


class ModuleDefinition(BaseModel):
    db_id: Optional[int] = None
    filepath: str
    class_name: Optional[str] = None
    namespace_path: str = ""
    methods: List[MethodSignature] = []
    docstring: str = ""


class ArchitectureNamespace(BaseModel):
    db_id: Optional[int] = None
    namespace_path: str  # e.g. "expense_tracker/models"
    purpose: str


class DependencyEdge(BaseModel):
    source_filepath: str
    target_filepath: str
    import_symbols: List[str] = []


class ArchitecturePlan(BaseModel):
    db_id: Optional[int] = None
    problem_id: int
    package_name: str
    root_namespace: str
    namespaces: List[ArchitectureNamespace] = []
    domain_models: List[DomainModelDefinition] = []
    modules: List[ModuleDefinition] = []
    dependencies: List[DependencyEdge] = []
    version: int = 1


# ── Governance ─────────────────────────────────────────────────────────────────

class DriftItem(BaseModel):
    filepath: str
    drift_type: str = "unplanned_file"  # "unplanned_file", "new_class", "new_import"
    class_name: Optional[str] = None
    reason: str


class DriftReport(BaseModel):
    items: List[DriftItem]


class GovernanceDecision(BaseModel):
    decision: Literal["approve", "reject", "modify"]
    reason: str
    plan_updates: Optional[Dict] = None


# ── Engineering Analysis ───────────────────────────────────────────────────────

class EngineeringRequirement(BaseModel):
    db_id: Optional[int] = None
    type: Literal["business", "functional", "nonfunctional"]
    text: str


class EngineeringUseCase(BaseModel):
    db_id: Optional[int] = None
    title: str
    description: str


class TargetFile(BaseModel):
    filepath: str
    action: Literal["create", "modify", "delete"]


class EngineeringIssue(BaseModel):
    db_id: Optional[int] = None
    title: str
    description: str
    target_files: List[TargetFile] = []
    test_files: List[str] = []
    depends_on_issues: List[int] = []
    depends_on_titles: List[str] = []
    suggested_model: Optional[str] = None
    test_setup_hint: Optional[str] = None
    # Production-grade enrichment from IssueEnrichmentAgent. Optional
    # because it's a separate phase after the Engineer's analyze pass;
    # if enrichment failed or was skipped, downstream still works on
    # the raw issue. Uses ``Any`` to keep the import surface clean —
    # the actual type is ``bizniz.agents.issue_enrichment.EnrichedIssue``.
    enriched: Optional[object] = None


class DependencyLayer(BaseModel):
    """A group of issues with no inter-dependencies, safe to batch together."""
    layer_index: int
    issues: List[EngineeringIssue]


class EngineeringAnalysis(BaseModel):
    problem_id: int
    requirements: List[EngineeringRequirement] = []
    use_cases: List[EngineeringUseCase] = []
    issues: List[EngineeringIssue] = []
    architecture: Optional[ArchitecturePlan] = None


# ── Errors ─────────────────────────────────────────────────────────────────────

class EngineerError(Exception):
    pass


class EngineerBadAIResponseError(EngineerError):
    pass
