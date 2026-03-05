
REPAIR_PROMPT = """
These are the original instruactions you were given:

{instructions}


The previously generated Python code failed with the following error or errors:

{error_message}


Here is the code you generated:

{previous_code}


This is the input data you were given:

{input_data}
"""
