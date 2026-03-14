"""
LanguageStrategy — abstract base for language-specific behavior.

Each language (Python, TypeScript, C#) provides its own strategy that
encapsulates prompts, file conventions, import scanning, and package management.
"""

from abc import ABC, abstractmethod
from typing import Set

from bizniz.workspace.base_workspace import BaseWorkspace


class LanguageStrategy(ABC):
    """Strategy interface for language-specific orchestrator behavior."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Language identifier (e.g. 'python', 'typescript')."""
        ...

    @property
    @abstractmethod
    def test_symbol(self) -> str:
        """Test runner command (e.g. 'pytest', 'jest')."""
        ...

    @property
    @abstractmethod
    def code_fence_lang(self) -> str:
        """Markdown code fence language tag."""
        ...

    @property
    @abstractmethod
    def language_prefix(self) -> str:
        """Prompt prefix for language context (empty string for default language)."""
        ...

    @abstractmethod
    def is_test_file(self, filepath: str) -> bool:
        """Return True if filepath is a test file for this language."""
        ...

    @abstractmethod
    def strip_extension(self, filepath: str) -> str:
        """Strip the file extension(s) from filepath."""
        ...

    @abstractmethod
    def scan_imports(self, files: dict) -> Set[str]:
        """Scan source files and return set of imported package names."""
        ...

    @abstractmethod
    def filter_third_party(self, imports: Set[str], workspace_modules: Set[str]) -> Set[str]:
        """Filter imports to only third-party packages."""
        ...

    @abstractmethod
    def get_autocoder_system_prompt(self, evaluation_environment: str = "") -> str:
        """Return the autocoder system prompt for this language."""
        ...

    @abstractmethod
    def get_autotester_system_prompt(self) -> str:
        """Return the autotester system prompt for this language."""
        ...

    @abstractmethod
    def get_autotester_user_prompt(self) -> str:
        """Return the autotester user prompt template for this language."""
        ...

    @abstractmethod
    def is_stdlib(self, module_name: str) -> bool:
        """Return True if module_name is a standard library module."""
        ...

    @abstractmethod
    def detect_project_file(self, workspace: BaseWorkspace) -> bool:
        """Return True if the workspace contains this language's project file."""
        ...

    @abstractmethod
    def get_installed_packages(self, workspace: BaseWorkspace) -> str:
        """Read installed packages from the workspace's project/requirements file."""
        ...
