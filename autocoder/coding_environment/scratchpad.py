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
    default_model=None,
    config_file_path=None,
)

class CalculatorValidator(BaseValidator):
    def validate(self, input_data: str, output_data: str = "", *args, **kwargs) -> ValidationResult:
        try:
            result = float(output_data)
            return ValidationResult(is_valid=True)
        except ValueError:
            pass
            # return ValidationResult(is_valid=False, errors=["Output data is not a number."])
    
        if isinstance(output_data, None):
            return ValidationResult(is_valid=True)
        
        return ValidationResult(is_valid=False, errors=["Output data is not a number or None."])
    
autocoder = Autocoder(
    input_data="25, 17",
    process_prompt="Generate Python code to add numbers. You must figure out how to parse the input data and return the result.",
    max_retries=2,
    client=ChatGPTClientFactory.create_client(config=config, api_key=api_key),
    validator=CalculatorValidator,
    config=AutocoderConfig(
        code_directory="/tmp/autocoder/code_generator"
    ),
)
    

