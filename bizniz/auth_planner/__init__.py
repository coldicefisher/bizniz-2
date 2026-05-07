"""AuthPlanner — single-call LLM agent that emits an AuthSpec from a
problem statement + architecture.

Replaces the planning portion of the legacy AuthAgent. The LLM only
emits structured intent (roles, applications, test users); it never
talks to FusionAuth directly. ``FusionAuthOperator`` (deterministic)
materializes the spec.
"""
from bizniz.auth_planner.agent import AuthPlanner, AuthPlannerError

__all__ = ["AuthPlanner", "AuthPlannerError"]
