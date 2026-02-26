



import re
import html
import unicodedata
import hashlib


# Safe import factory
def make_safe_import(allowed_modules: dict):
    """
    Returns a restricted __import__ function that only allows
    explicitly whitelisted modules.
    """
    def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name in allowed_modules:
            return allowed_modules[name]
        raise ImportError(
            f"Import of module '{name}' is not allowed in safe execution environment."
        )
    return safe_import


# Code evaluation sandbox
def evaluate_generated_code(
    code: str,
    input_data: str,
    *,
    exposed_globals: dict | None = None,
    exposed_builtins: dict | None = None,
    allowed_modules: dict | None = None,
):
    """
    Execute generated code in a restricted sandbox and invoke
    process(input_data: str).

    The generated code MUST define:
        def process(input_data: str) -> str | dict
    """
    # print("Evaluating generated code:")
    # print(code)

    default_allowed_modules = {
        "hashlib": hashlib,
        # "re": re,
        # "html": html,
        # "unicodedata": unicodedata,
    }

    if allowed_modules:
        default_allowed_modules.update(allowed_modules)


    safe_builtins = {
        "__import__": make_safe_import(default_allowed_modules),

        # Introspection
        "hasattr": hasattr,
        "getattr": getattr,
        "setattr": setattr,
        "isinstance": isinstance,
        "type": type,

        
        
        # Core exceptions / debugging
        "Exception": Exception,
        "print": print,
        "repr": repr,

        # Collections & iteration
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


        # Types
        "str": str,
        "int": int,
        "float": float,
        "bool": bool,
        "dict": dict,
        "list": list,
        "set": set,
        "isinstance": isinstance,

        # Math helpers
        "min": min,
        "max": max,
        "sum": sum,
    }

    
    if exposed_builtins:
        safe_builtins.update(exposed_builtins)

    
    # Exposed globals
    exec_globals = {
        "re": re,
        "html": html,
        "unicodedata": unicodedata,
        "hashlib": hashlib
    }

    if exposed_globals:
        exec_globals.update(exposed_globals)



    # Guard against imports in generated code
    if "import " in code:
        return {
            "success": False,
            "result": "Generated code must not contain import statements. Use provided globals instead. Available globals are: " + ", ".join(exec_globals.keys()) + ". Allowed modules are: " + ", ".join(default_allowed_modules.keys())
        }

    # Attach builtins explicitly
    exec_globals["__builtins__"] = safe_builtins

    
    # Primary evaluation step
    try:
        exec(code, exec_globals)
    except Exception as e:
        if "Import of module" in str(e):
            return {
                "success": False,
                "result": f"Import error: {str(e)}. Allowed modules are: {list(default_allowed_modules.keys())}. Globals are: {list(exec_globals.keys())}",
            }
            
        return {
            "success": False,
            "result": f"Error during code execution: {e}",
        }

    
    # Validate presence of process()
    process_fn = exec_globals.get("process")
    if not callable(process_fn):
        return {
            "success": False,
            "result": "Generated code does not define a callable `process(input_data: str)` function",
        }

    # Call process() and capture output
    try:
        result = process_fn(input_data)
    except Exception as e:
        return {
            "success": False,
            "result": f"The process function raised an error: {e}",
        }

    
    # Normalize output
    if isinstance(result, dict) and "xml" in result:
        return {"success": True, "result": result["xml"]}

    if isinstance(result, str):
        return {"success": True, "result": result}

    return {
        "success": False,
        "result": f"The process function produced an invalid result: {result!r}",
    }
