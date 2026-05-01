"""
BaseDebugger — abstract base for all debugger agents.

QuickDebugger (one-shot, no tools) and AgenticDebugger (iterative tool-use)
both inherit from this to share a common interface.
"""

from abc import ABC, abstractmethod
from typing import Optional, Callable, Dict, List

from bizniz.core.client import BaseAIClient
from bizniz.environment.base_environment import BaseExecutionEnvironment
from bizniz.workspace.base_workspace import BaseWorkspace


class BaseDebugger(ABC):
    """
    Common interface for debugger agents.

    Parameters
    ----------
    client:
        AI client instance.
    workspace:
        The workspace to explore files in.
    environment:
        Execution environment for running tests.
    on_status_message:
        Optional callback for human-readable status updates.
    """

    def __init__(
        self,
        client: BaseAIClient,
        workspace: BaseWorkspace,
        environment: BaseExecutionEnvironment,
        on_status_message: Optional[Callable[[str], None]] = None,
    ):
        self._client = client
        self._workspace = workspace
        self._environment = environment
        self._on_status_message = on_status_message

    def _log(self, msg: str):
        """Emit a status message if a callback is configured."""
        if self._on_status_message:
            self._on_status_message(msg)

    @property
    def _ai_client(self):
        """Return ``self._client`` after stamping ``_caller_agent``
        with this debugger's class name. Mirrors ``BaseAIAgent``'s
        per-call attribution fix so cost-tracker rollups attribute
        debugger calls correctly. Without this, ``self._ai_client``
        attribute lookups in AgenticDebugger raise AttributeError
        (regression from the BaseAIAgent property addition — debuggers
        don't subclass BaseAIAgent)."""
        try:
            self._client._caller_agent = type(self).__name__.lower()
        except Exception:
            pass
        return self._client

    @abstractmethod
    def diagnose(self, **kwargs):
        """
        Run the debugger and return a diagnosis.

        Subclasses define their own parameter signatures but all return
        a diagnosis model (QuickDebuggerDiagnosis or AgenticDiagnosis).
        """
        ...
