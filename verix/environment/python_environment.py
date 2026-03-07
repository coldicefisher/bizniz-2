import re
import html
import unicodedata
import hashlib
import traceback
import time
import io
import contextlib
import types
import inspect
import builtins

from typing import Dict, Any, Optional

from .base_environment import BaseExecutionEnvironment
from .types import (
    ExecutionEnvironmentErrorDetails,
    ExecutionEnvironmentResult,
    ExecutionCallSpec
)


# --------------------------------------------
# Safe import factory
# --------------------------------------------

def make_safe_import(allowed_modules: dict):

    def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
        root_module = name.split(".")[0]

        if root_module in allowed_modules:
            return allowed_modules[root_module]

        if name in allowed_modules:
            return allowed_modules[name]

        raise ImportError(f"Import of module '{name}' is not allowed in the sandbox.")

    return safe_import


# --------------------------------------------
# Error builder
# --------------------------------------------

def build_error(error: Exception, code: str):

    tb = traceback.TracebackException.from_exception(error)

    line_no = None
    code_line = None

    for frame in tb.stack:

        if frame.filename == "<string>":

            line_no = frame.lineno

            code_lines = code.splitlines()

            if 0 < line_no <= len(code_lines):
                code_line = code_lines[line_no - 1]

            break

    return {
        "type": type(error).__name__,
        "message": str(error),
        "line": line_no,
        "code_line": code_line,
        "traceback": "".join(tb.format())
    }


# --------------------------------------------
# Symbol resolver
# --------------------------------------------

def resolve_symbol(namespace: Dict[str, Any], symbol: str):

    parts = symbol.split(".")

    obj = namespace.get(parts[0])

    if obj is None:
        raise NameError(f"Symbol '{parts[0]}' not found")

    for part in parts[1:]:

        if part.endswith("()"):
            name = part[:-2]
            obj = getattr(obj, name)()
        else:
            obj = getattr(obj, part)

    return obj


# --------------------------------------------
# Python Execution Environment
# --------------------------------------------

class PythonSandboxExecutionEnvironment(BaseExecutionEnvironment):

    name = "python-sandbox-environment"



    @staticmethod   
    def trace_event(traces: list, stage: str, message: str, metadata: Optional[dict] = None):

        traces.append(
            {
                "stage": stage,
                "message": message,
                "timestamp": time.time(),
                "metadata": metadata or {}
            }
        )
        
    

    def execute(
        self,
        code: str,
        call_spec: ExecutionCallSpec
    ) -> ExecutionEnvironmentResult:

        # Convert call_spec args to class if dict (for backward compatibility)
        if isinstance(call_spec, dict):
            call_spec = ExecutionCallSpec(**call_spec)

        traces = []    
        self.trace_event(traces, "sandbox_started", "Starting sandbox execution")
        
        if not call_spec.symbol:
            return ExecutionEnvironmentResult(
                success=False,
                error=ExecutionEnvironmentErrorDetails(
                    stage="validation",
                    type="InvalidCallSpec",
                    message="call_spec.symbol cannot be empty"
                ),
                traces=traces
            )


        # Extract args and kwargs with defaults            
        args = call_spec.args or ()
        kwargs = call_spec.kwargs or {}

        # --------------------------------------------
        # Allowed modules
        # --------------------------------------------

        default_allowed_modules = {
            "hashlib": hashlib
        }

        default_allowed_modules.update((self.allowed_modules or {}))

        if self.exposed_globals:

            for name, module in (self.exposed_globals or {}).items():

                # if isinstance(module, type(re)):
                if isinstance(module, types.ModuleType):
                    default_allowed_modules[name] = module

        # --------------------------------------------
        # Safe builtins
        # --------------------------------------------

        safe_builtins = {

            "__import__": make_safe_import(default_allowed_modules),
            "__build_class__": builtins.__build_class__,

            "hasattr": hasattr,
            "getattr": getattr,
            "setattr": setattr,
            "isinstance": isinstance,
            "type": type,

            "Exception": Exception,
            "print": print,
            "repr": repr,

            "len": len,
            "range": range,
            "enumerate": enumerate,
            "zip": zip,
            "map": map,
            "filter": filter,
            "any": any,
            "all": all,
            "reversed": reversed,
            "sorted": sorted,

            "str": str,
            "int": int,
            "float": float,
            "bool": bool,
            "dict": dict,
            "list": list,
            "set": set,

            "min": min,
            "max": max,
            "sum": sum,
        }

        safe_builtins.update((self.exposed_builtins or {}))

        # --------------------------------------------
        # Globals
        # --------------------------------------------

        exec_globals = {
            "__name__": "__sandbox__",
            "re": re,
            "html": html,
            "unicodedata": unicodedata,
            "hashlib": hashlib
        }

        exec_globals.update((self.exposed_globals or {}))

        exec_globals["__builtins__"] = safe_builtins

        # --------------------------------------------
        # Security Guards
        # --------------------------------------------

        forbidden_patterns = [
            r"\bexec\s*\(",
            r"\beval\s*\(",
            r"\bcompile\s*\("
        ]

        for pattern in forbidden_patterns:
            match = re.search(pattern, code)
            if match:
                
                return ExecutionEnvironmentResult(
                    success=False,
                    error=ExecutionEnvironmentErrorDetails(
                        stage="security",
                        type="SecurityViolation",
                        message="Use of forbidden construct detected.",
                        code_line=f"Forbidden construct detected: {match.group(0)}"
                    ),
                    traces=traces
                )

        # --------------------------------------------
        # Capture stdout / stderr
        # --------------------------------------------

        stdout_buffer = io.StringIO()
        stderr_buffer = io.StringIO()

        # --------------------------------------------
        # Compile / load code
        # --------------------------------------------
        self.trace_event(traces, "compile_started", "Compiling generated code")

        try:

            with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):
                exec(code, exec_globals)
                
            

            self.trace_event(traces, "compile_finished", "Code compiled successfully")

        except Exception as e:

            return ExecutionEnvironmentResult(
                success=False,
                error=ExecutionEnvironmentErrorDetails(
                    stage="compile",
                    **build_error(e, code)
                ),
                stdout=stdout_buffer.getvalue(),
                stderr=stderr_buffer.getvalue(),
                traces=traces
            )
            
    
        # --------------------------------------------
        # Resolve call target
        # --------------------------------------------

        try:
            self.trace_event(traces, "symbol_resolution", f"Resolving symbol {call_spec.symbol}")
            target = resolve_symbol(exec_globals, call_spec.symbol)
            self.trace_event(
                traces,
                "symbol_resolved",
                f"Symbol {call_spec.symbol} resolved",
                {"callable": str(target)}
            )            
            if not callable(target):

                self.trace_event(
                    traces,
                    "validation",
                    "Resolved symbol is not callable",
                    {"symbol": call_spec.symbol}
                )

                return ExecutionEnvironmentResult(
                    success=False,
                    error=ExecutionEnvironmentErrorDetails(
                        stage="validation",
                        type="SymbolNotCallable",
                        message=f"Symbol '{call_spec.symbol}' is not callable"
                    ),
                    stdout=stdout_buffer.getvalue(),
                    stderr=stderr_buffer.getvalue(),
                    traces=traces
                )

        except Exception as e:

            return ExecutionEnvironmentResult(
                success=False,
                error=ExecutionEnvironmentErrorDetails(
                    stage="validation",
                    type="EntrypointNotFound",
                    message=str(e)
                ),
                stdout=stdout_buffer.getvalue(),
                stderr=stderr_buffer.getvalue(),
                traces=traces
            )



        # --------------------------------------------
        # Validate arguments and signature
        # --------------------------------------------
        self.trace_event(
            traces,
            "signature_validation",
            "Validating function signature"
        )
        try:
            sig = inspect.signature(target)
            sig.bind(*args, **kwargs)
        except TypeError as e:
            self.trace_event(
                traces,
                "validation",
                "Function signature validation failed",
                {"error": str(e)}
            )
            return ExecutionEnvironmentResult(
                success=False,
                error=ExecutionEnvironmentErrorDetails(
                    stage="validation",
                    type="InvalidArguments",
                    message=str(e)
                ),
                stdout=stdout_buffer.getvalue(),
                stderr=stderr_buffer.getvalue(),
                traces=traces
            )
        # --------------------------------------------
        # Execute target
        # --------------------------------------------

        try:

            self.trace_event(
                traces,
                "execution_started",
                f"Calling {call_spec.symbol}",
                {
                    "args": args,
                    "kwargs": kwargs
                }
            )

            start = time.perf_counter()

            with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):
                
                result = target(*args, **kwargs)


            duration = time.perf_counter() - start

            self.trace_event(
                traces,
                "execution_finished",
                "Execution completed successfully",
                {"duration": duration}
            )

        except Exception as e:

            self.trace_event(
                traces,
                "runtime_error",
                "Execution raised exception",
                {"error": str(e)}
            )

            return ExecutionEnvironmentResult(
                success=False,
                error=ExecutionEnvironmentErrorDetails(
                    stage="runtime",
                    **build_error(e, code)
                ),
                stdout=stdout_buffer.getvalue(),
                stderr=stderr_buffer.getvalue(),
                traces=traces
            )

        
        # --------------------------------------------
        # Return success
        # --------------------------------------------

        return ExecutionEnvironmentResult(
            success=True,
            result=result,
            execution_time=duration,
            stdout=stdout_buffer.getvalue(),
            stderr=stderr_buffer.getvalue(),
            traces=traces
        )
        
        
    