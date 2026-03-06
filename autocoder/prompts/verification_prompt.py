VERIFICATION_PROMPT = """
You must verify whether the code correctly implements the instructions.

INSTRUCTIONS:

{instructions}


INPUT:

{input}


OUTPUT:

{output}


GENERATED CODE:

{code}


Determine whether the output correctly follows the instructions and the output is what is expected. Adjust the code if needed to meet the requirements. 


Return JSON:

{
    "is_valid": true/false,
    "errors": ["list of problems"],
    "code": "corrected code if needed"
}
"""


VERIFICATION_PROMPT_INSTRUCTIONS = "You are an expert code reviewer. You will be given a piece of code, the input data it was run with, and the output it produced. You will check the code, input, and output to ensure it matches the instructions and expectations of quality code. You will check the output against the input and verify it looks correct."