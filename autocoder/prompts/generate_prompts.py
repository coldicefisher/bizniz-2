GENERATE_HEADER_PROMPT = """
WRAPPER FUNCTION INSTRUCTIONS:
──────────────────────────────────────────────────────────────
The code will be wrapped in a function named `process(input_data: str)`. This is the primnary entrypoint function and 
begins the process of the rest of the requirements. The `process` function is the only thing that our code will call, so you 
must include that function; at the very least, it will call the function that you write to handle the requirements. The return 
from process should be inferred by the architecture described. IF the return should be JSON, infer that. Is string, infer that. 
From there, follow the architecture requirements below.


EVALUATION ENVIRONMENT
──────────────────────────────────────────────────────────────
The code you generate will be executed in a restricted Python environment:

{evaluation_environment}


The following additional libraries will be available WITHOUT IMPORTING:
{additional_libraries}

"""

extra = """
VALIDATION CODE USED FOR THE OUTPUT CODE
──────────────────────────────────────────────────────────────
The output code will be tested against the following:

{validation_requirements}

"""

GENERATE_TAIL_PROMPT = """

RESPONSE FORMAT:
──────────────────────────────────────────────────────────────
Your response MUST be JSON with a keys: 

You should return a json response like this:

{{
    "code": ""
}}

Return ONLY valid JSON.

Do not include:
- explanations
- markdown
- code fences
- commentary

The response must start with '{' and end with '}'.

Do NOT hallucinate content. 

"""