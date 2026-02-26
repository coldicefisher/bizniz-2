GENERATE_HEADER_PROMPT = """
WRAPPER FUNCTION INSTRUCTIONS:
──────────────────────────────────────────────────────────────
The code will be wrapped in a function named `process(input_data: str)`. This is the primnary entrypoint function and 
begins the process of the rest of the requirements. The `process` function is the only thing that our code will call, so you 
must include that function; at the very least, it will call the function that you write to handle the requirements. The return 
from process should be inferred by the architecture described. IF the return should be JSON, infer that. Is string, infer that. 
From there, follow the architecture requirements below.

IMPORTANT RULES:
- Do NOT use import statements.
- Do NOT call globals(), locals(), or vars().

EVALUATION ENVIRONMENT
──────────────────────────────────────────────────────────────
The code you generate will be executed in a restricted Python environment:

{evaluation_environment}


The following additional libraries will be available:
{additional_libraries}


VALIDATION CODE USED FOR THE OUTPUT CODE
──────────────────────────────────────────────────────────────
The output code must validate against the following requirements, using the provided `validator.validate` function:

{validation_requirements}


"""


GENERATE_TAIL_PROMPT = """

RESPONSE FORMAT:
──────────────────────────────────────────────────────────────
Your response MUST be JSON with a keys: 

"code" containing the Python code generated.
"cannot_process": indicate True if you can process the input according to the specs, otherwise False.


If the code cannot be produced to process the input data, you must return "cannot_process": true and an empty string for "code".

You should return a json response like this:

{{
    "code": "",
    "cannot_process": true    
}}

Do NOT hallucinate content. 

"""