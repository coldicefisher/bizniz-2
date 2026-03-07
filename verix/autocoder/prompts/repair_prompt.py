
REPAIR_PROMPT = """
The previous code failed. You must fix the code to address the error message and produce valid code that meets the original instructions. 


Perform these steps carefully:

1. Identify the root cause of the error.
2. Explain why the code failed for the given input.
3. Determine the minimal correction.
4. Return corrected code.


You may decompose the problems into multiple functions or classes as needed, but you should return all the code in a single string in 
the "code" field of the JSON response. The User should have a single function call to make to execute your code, and that should be specified 
in the "call_spec" field of the JSON response. This means you may use helper functions or classes, and you may return class methods, such as:
- "symbol": "my_function"
- "symbol": "MyClass.my_method"
- "symbol": "MyClass().my_method"

You can use Class methods, static methods, or instance methods, but you should specify how to call them in the "symbol" field of the "call_spec".



The previously generated Python code failed with the following error or errors:

{error_message}


Here is the code you generated:

{previous_code}




RESPONSE FORMAT:
──────────────────────────────────────────────────────────────

You should return a json response like this:

{{
    "analysis": "<analysis of the error>",
    "fix_plan": "<description of the minimal fix>",
    "code": "<the corrected code>",
    "call_spec": {{
        "symbol": "<the symbol to call to execute the code>",
        "args": [<the positional arguments to call the symbol with>],
        "kwargs": {{<the keyword arguments to call the symbol with>}}
    }}
}}

Return ONLY valid JSON. All fields are required.

Do not include:
- explanations
- markdown
- code fences
- commentary

The response must start with '{{' and end with '}}'.

Do NOT hallucinate content. 


"""

