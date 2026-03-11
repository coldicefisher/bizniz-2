"""Base class for language-specific pre-flight validators."""

from abc import ABC, abstractmethod
from typing import Dict, List

from bizniz.preflight.types import PreflightResult
from bizniz.workspace.base_workspace import BaseWorkspace


class BasePreflightValidator(ABC):
    """
    Pre-flight validator that checks generated code for structural issues
    (missing imports, missing modules, missing init files) and auto-stubs
    missing files to prevent import chain failures.

    Runs between code generation and test execution.
    """

    language: str = ""

    def __init__(self, workspace: BaseWorkspace):
        self._workspace = workspace

    @abstractmethod
    def validate(
        self,
        generated_files: Dict[str, str],
        declared_dependencies: List[str],
    ) -> PreflightResult:
        """
        Validate generated files and auto-stub missing modules.

        Parameters
        ----------
        generated_files:
            Dict of {filepath: content} for all files generated so far.
        declared_dependencies:
            List of third-party package names declared by the LLM.

        Returns
        -------
        PreflightResult with issues found and stubs created.
        """
        ...
