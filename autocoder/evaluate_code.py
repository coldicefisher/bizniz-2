import re
import html
import unicodedata
import hashlib
import traceback
import time
import io
import contextlib


# --------------------------------------------
# Safe import factory
# --------------------------------------------
def make_safe_import(allowed_modules: dict):
    def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name in allowed_modules:
            return allowed_modules[name]

        raise ImportError(
            f"Import of module '{name}' is not allowed in the sandbox. "
            f"Allowed modules: {list(allowed_modules.keys())}"
        )

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
# Sandbox Execution
# --------------------------------------------
def evaluate_generated_code(
    code: str,
    input_data: str,
    *,
    exposed_globals: dict | None = None,
    exposed_builtins: dict | None = None,
    allowed_modules: dict | None = None,
):

    default_allowed_modules = {
        "hashlib": hashlib
    }

    if allowed_modules:
        default_allowed_modules.update(allowed_modules)

    # --------------------------------------------
    # Safe builtins
    # --------------------------------------------

    safe_builtins = {

        "__import__": make_safe_import(default_allowed_modules),

        # introspection
        "hasattr": hasattr,
        "getattr": getattr,
        "setattr": setattr,
        "isinstance": isinstance,
        "type": type,

        # debugging
        "Exception": Exception,
        "print": print,
        "repr": repr,

        # iteration
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

        # types
        "str": str,
        "int": int,
        "float": float,
        "bool": bool,
        "dict": dict,
        "list": list,
        "set": set,

        # math
        "min": min,
        "max": max,
        "sum": sum,
    }

    if exposed_builtins:
        safe_builtins.update(exposed_builtins)

    # --------------------------------------------
    # Globals
    # --------------------------------------------

    exec_globals = {
        "re": re,
        "html": html,
        "unicodedata": unicodedata,
        "hashlib": hashlib
    }

    if exposed_globals:
        exec_globals.update(exposed_globals)

    exec_globals["__builtins__"] = safe_builtins

    # --------------------------------------------
    # Security Guards
    # --------------------------------------------

    forbidden_patterns = [
        # "import ",
        "exec(",
        "eval(",
        # "__import__",
        # "open(",
        "compile("
    ]

    for pattern in forbidden_patterns:
        if pattern in code:
            return {
                "success": False,
                "stage": "security",
                "error": {
                    "type": "SecurityViolation",
                    "message": f"Use of forbidden construct '{pattern}' detected.",
                    "code_line": pattern
                }
            }

    # --------------------------------------------
    # Capture stdout / stderr
    # --------------------------------------------

    stdout_buffer = io.StringIO()
    stderr_buffer = io.StringIO()

    # --------------------------------------------
    # Execute generated code
    # --------------------------------------------

    try:

        with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):

            exec(code, exec_globals)

    except Exception as e:

        return {
            "success": False,
            "stage": "compile",
            "error": build_error(e, code),
            "environment": {
                "allowed_modules": list(default_allowed_modules.keys()),
                "globals": list(exec_globals.keys()),
                "builtins": list(safe_builtins.keys())
            },
            "stdout": stdout_buffer.getvalue(),
            "stderr": stderr_buffer.getvalue()
        }

    # --------------------------------------------
    # Validate process() exists
    # --------------------------------------------

    process_fn = exec_globals.get("process")

    if not callable(process_fn):

        return {
            "success": False,
            "stage": "validation",
            "error": {
                "type": "MissingProcessFunction",
                "message": "Generated code must define process(input_data: str)",
            }
        }

    # --------------------------------------------
    # Execute process()
    # --------------------------------------------

    try:

        start = time.time()

        with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):

            result = process_fn(input_data)

        duration = time.time() - start

    except Exception as e:

        return {
            "success": False,
            "stage": "runtime",
            "error": build_error(e, code),
            "input_data": input_data,
            "stdout": stdout_buffer.getvalue(),
            "stderr": stderr_buffer.getvalue()
        }

    # --------------------------------------------
    # Validate return format
    # --------------------------------------------

    if isinstance(result, dict) and "result" in result:

        return {
            "success": True,
            "result": result["result"],
            "execution_time": duration,
            "stdout": stdout_buffer.getvalue()
        }

    return {
        "success": False,
        "stage": "validation",
        "error": {
            "type": "InvalidReturnFormat",
            "message": "process() must return {'result': value}",
            "returned_value": repr(result)
        }
    }