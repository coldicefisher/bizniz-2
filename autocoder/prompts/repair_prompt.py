
REPAIR_PROMPT_INSTRUCTIONS = """
You are an expert Python programmer tasked with fixing bugs in code.  You must fix the code to address the error message and 
produce valid code that meets the original instructions. If the error message indicates that the input data cannot be processed, you should 
determine that as well and respond accordingly.


Perform these steps carefully:

1. Identify the root cause of the error.
2. Explain why the code failed for the given input.
3. Determine the minimal correction.
4. Return corrected code.

"""


REPAIR_PROMPT = """


The previously generated Python code failed with the following error or errors:

{error_message}


Here is the code you generated:

{previous_code}


This is the input data you were given:

{input_data}



RESPONSE FORMAT:
──────────────────────────────────────────────────────────────

You should return a json response like this:

{{
    "analysis": "<analysis of the error>",
    "fix_plan": "<description of the minimal fix>",
    "code": "<the corrected code>"
}}

Return ONLY valid JSON.

Do not include:
- explanations
- markdown
- code fences
- commentary

The response must start with '{{' and end with '}}'.

Do NOT hallucinate content. 


"""



REPAIR_PROMPT_WITH_INSTRUCTIONS = """

You are an expert Python debugger.

First analyze the failure.
Explain what caused the error.

Then determine the minimal fix.

Finally return the corrected code in JSON.

Follow this format:

Analysis:
<why the error occurred>

Fix Plan:
<what must change>


These are the original instruactions you were given:

{instructions}

The previously generated Python code failed with the following error or errors:

{error_message}


Here is the code you generated:

{previous_code}


This is the input data you were given:

{input_data}



RESPONSE FORMAT:
──────────────────────────────────────────────────────────────

You should return a json response like this:

{{
    "analysis": "<analysis of the error>",
    "fix_plan": "<description of the minimal fix>",
    "code": "<the corrected code>"
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
