import os

from dotenv import load_dotenv
import requests

load_dotenv()  # automatically finds .env in current directory or parents

import bs4


from autocoder.autocoder import (
    AutocoderProcessError, 
    AutocoderBadAIResponseError, 
    AutocoderProcessResult, 
    Autocoder, 
    AutocoderConfig, 
    AutocoderEnvironment
)

from autocoder.clients.chatgpt.chatgpt_client_factory import ChatGPTClientFactory
from autocoder.clients.chatgpt.chatgpt_client_config import ChatGPTClientConfig

from typing import Optional, Callable, Any, Dict, List
from pydantic import ValidationError
from autocoder.tests.mock_validator import MockValidator

from autocoder.base_validator import BaseValidator, ValidationResult



'''
CALCULATOR EXAMPLE ///////////////////////////////////////////////////////////////////////
We are going to create a simple calculator example that takes in a 
string of two numbers and returns their sum.

The Validator will check that the output contains a float or integer.
'''
# CREATE VALIDATOR /////////////////////////////////////////////////////////////
'''
The Validator is used to validate the input data versus the output data. You will
override the `validate` method to implement your custom validation logic. 
'''
api_key = os.getenv("OPENAI_API_KEY")

config = ChatGPTClientConfig(
    is_azure=False,
    api_base=None,
    available_models=None,
    default_model='gpt-4o-mini',
    config_file_path=None,
)

class CalculatorValidator(BaseValidator):
    def validate(self, input_data: str, output_data: str = "", *args, **kwargs) -> ValidationResult:
        # List cities from the webpage, so we expect a list of cities as output
        print(f"Validating output: ")
        print(f"{output_data}")
        
        if not isinstance(output_data, list):
            return ValidationResult(
                is_valid=False,
                errors=["Output data is not a list."]
            )
        return ValidationResult(is_valid=True)
    
        # Calculate the sum of two numbers, so we expect a number as output
        try:
            float(output_data)
            return ValidationResult(is_valid=True)
        except (ValueError, TypeError):
            pass

        if output_data is None:
            return ValidationResult(is_valid=True)

        return ValidationResult(
            is_valid=False,
            errors=["Output data is not a number or None."]
        )
    
# nuke the code directory if it exists from previous runs
import shutil
code_directory = "/tmp/autocoder/code_generator"
if os.path.exists(code_directory):
    shutil.rmtree(code_directory)
    

url = "https://en.wikipedia.org/wiki/List_of_municipalities_in_Tennessee"
autocoder = Autocoder(
    # input_data="24, 6, 8",
    input_data=url,
    # process_prompt="Generate a function that can give me the cities off of a given webpage. It should return a list of cities. The function should be able to handle any webpage, so it should not be hardcoded to the structure of the wikipedia page. You will be given a URL and you can use `bs4` and `requests` modules to accomplish this.",
    process_prompt="Generate a function that can give me the URLs off of a given webpage. It should return a list of URLs.",
    max_retries=50,
    client=ChatGPTClientFactory.create_client(config=config, api_key=api_key),
    validator=CalculatorValidator,
    config=AutocoderConfig(
        code_directory="/tmp/autocoder/code_generator",
        environment_settings=AutocoderEnvironment(
            exposed_globals={
                "requests": requests, 
                "bs4": bs4
            },
        )
    ),
)

response = requests.get(url)
    
webpage_snippet = "\n".join(response.text.splitlines()[:500])
verify_prompt = f"""
Please verify that the code is deisgned to extract URLs from a webpage and return them as a list. You will not know the structure of the webpage.
Please use the following snippet of the webpage as a reference:

{webpage_snippet}
"""


res = autocoder.process(on_event=lambda event: print(f"Event: {event}"), ai_verification_prompt=verify_prompt)