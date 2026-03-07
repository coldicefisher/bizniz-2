# verix/environment/base_environment.py

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional

from .types import (
    ExecutionEnvironmentErrorDetails,
    ExecutionEnvironmentResult,
    ExecutionCallSpec
)


class BaseExecutionEnvironment(ABC):
    """
    Base class for all code execution environments.

    Defines the contract AI agents use to execute generated code.
    """

    name: str = "base-environment"

    def __init__(
        self,
        exposed_globals: Optional[Dict[str, Any]] = None,
        exposed_builtins: Optional[Dict[str, Any]] = None,
        allowed_modules: Optional[Dict[str, Any]] = None,
        timeout: int = 600,
    ):
        self.exposed_globals = exposed_globals or {}
        self.exposed_builtins = exposed_builtins or {}
        self.allowed_modules = allowed_modules or {}
        self.timeout = timeout


    @abstractmethod
    def execute(
        self,
        code: str,
        call_spec: ExecutionCallSpec
    ) -> ExecutionEnvironmentResult:
        """
        Execute generated code and invoke a symbol.

        Parameters
        ----------
        code:
            Generated Python code to load into the environment.

        call_spec:
            Defines which symbol to call and with what arguments.

        Returns
        -------
        ExecutionEnvironmentResult
        """
        pass


    def describe(self) -> str:
        """
        Returns a human-readable description of the environment.
        Used to inject into LLM prompts.
        """

        globals_list = ", ".join(self.exposed_globals.keys())
        builtins_list = ", ".join(self.exposed_builtins.keys())
        modules_list = ", ".join(self.allowed_modules.keys())

        return f"""
Execution Environment: {self.name}

The generated code will be executed in a restricted Python environment.

The system will then call a function or method using the provided call specification.

Available globals:
{globals_list or "None"}

Allowed builtins:
{builtins_list or "Default Python"}

Allowed modules:
{modules_list or "None"}

Execution timeout:
{self.timeout} seconds
"""