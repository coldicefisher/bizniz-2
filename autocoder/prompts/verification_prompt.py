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