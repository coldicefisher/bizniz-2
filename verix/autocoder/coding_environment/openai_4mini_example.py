import os

from dotenv import load_dotenv

load_dotenv()  # automatically finds .env in current directory or parents


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
    
    
autocoder = Autocoder(
    input_data="24, 6, 8",
    # input_data="https://en.wikipedia.org/wiki/List_of_municipalities_in_Tennessee",
    process_prompt="Generate a function that can give me the cities off of a given webpage. It should return a list of cities.",
    max_retries=20,
    client=ChatGPTClientFactory.create_client(config=config, api_key=api_key),
    validator=CalculatorValidator,
    config=AutocoderConfig(
        code_directory="/tmp/autocoder/code_generator",
        
    ),
)
    

res = autocoder.process(on_event=lambda event: print(f"Event: {event}"))