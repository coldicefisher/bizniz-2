"""v2 pipeline driver — orchestrates Planner → Architect → Provisioner →
AuthAgent → per-milestone (enrich → implement → review → repair →
integration) end-to-end.

Entry point for CLI is ``examples/v2_build.py`` which constructs the
clients + cost tracker + state directory and hands them to
``V2Pipeline.run(...)``. Driver internals are fully decoupled from CLI
concerns so they can be tested with mocked clients.
"""
from bizniz.driver.gates import GateAction, GatePolicy, GateViolation
from bizniz.driver.integration_phase import IntegrationPhase, IntegrationPhaseResult
from bizniz.driver.milestone_loop import MilestoneLoop, MilestoneOutcome
from bizniz.driver.pipeline import V2Pipeline, V2PipelineResult
from bizniz.driver.state import MilestoneState, RunState, SubPhase

__all__ = [
    "V2Pipeline",
    "V2PipelineResult",
    "MilestoneLoop",
    "MilestoneOutcome",
    "IntegrationPhase",
    "IntegrationPhaseResult",
    "RunState",
    "MilestoneState",
    "SubPhase",
    "GatePolicy",
    "GateAction",
    "GateViolation",
]
