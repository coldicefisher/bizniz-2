# NOT USED - USER PROVIDED INSTEAD
VERIFICATION_PROMPT = """

Original instructions and requirements:
{instructions}

Additional context for verification:
{verification_prompt}

Function Input:
{input}

Function Output:
{output}

Code to verify:
{code}


RESPONSE FORMAT:
──────────────────────────────────────────────────────────────
Return JSON:

{{
    "assesssment": "correct / incorrect / uncertain",
    "errors": ["list of problems"],
    "code": "corrected code if needed"
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


VERIFICATION_PROMPT_INSTRUCTIONS = """

You are an expert code reviewer. Your task is to review the following code and determine whether it is valid or not based on the provided input and output.
The code is meant to solve a programming problem, and you must verify that it does so correctly.

I need you to verify whether the following code meets the requirements and is correct based on the provided input and output. You may not have 
the information about the output format or the exact requirements, but do your best to determine if the code is likely to be correct or not
based on the context and your understanding of programming and the problem domain. If you cannot determine whether the code is correct or not, 
you should flag it as uncertain. Be honest about your assessment and provide reasoning for why you think the code is correct or not. If there are 
specific issues or potential problems with the code, please list them out in detail.
        
"""