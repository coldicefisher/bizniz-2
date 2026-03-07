
import json
import traceback
import generated_code

try:

    args = []
    kwargs = {}

    fn = getattr(generated_code, "process")

    result = fn(*args, **kwargs)

    print(json.dumps({
        "success": True,
        "result": result
    }))

except Exception as e:

    print(json.dumps({
        "success": False,
        "error": {
            "type": type(e).__name__,
            "message": str(e),
            "traceback": traceback.format_exc()
        }
    }))
