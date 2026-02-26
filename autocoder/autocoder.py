from abc import ABC, abstractmethod
import os
import shutil
from enum import Enum
from os import environ as env
import json
import uuid
import inspect
import sys
import hashlib
from datetime import datetime
from pathlib import Path

from typing import Optional, Callable, Union, Any, Dict, List, Tuple

from pydantic import BaseModel, Field
from pydantic import ValidationError


from openai import AzureOpenAI, OpenAI

from autocoder.base_validator import BaseValidator, ValidationResult

from autocoder.prompts.repair_prompt import REPAIR_PROMPT
from autocoder.prompts.verification_prompt import VERIFICATION_PROMPT
from autocoder.prompts.generate_prompts import GENERATE_HEADER_PROMPT, GENERATE_TAIL_PROMPT

from autocoder.clients.chatgpt.types.response_format import ResponseFormat
from autocoder.evaluate_code import evaluate_generated_code
from autocoder.clients.base_ai_client import BaseAIClient


class AutocoderProcessError(Exception):
    pass


class AutocoderBadAIResponseError(AutocoderProcessError):
    pass


class AutocoderProcessResult(BaseModel):
    cannot_process: bool
    code: Optional[str] = None
    output: Optional[Any] = None



class AutocoderEnvironment(BaseModel):
    exposed_globals: dict = Field(default_factory=dict)
    exposed_builtins: dict = Field(default_factory=dict)
    allowed_modules: dict = Field(default_factory=dict)

    

class AutocoderConfig(BaseModel):
    code_directory: str
    filename: str = "generated_code.py"
    module_name: Optional[str] = "code"
    
    configuration_directory: Optional[str] = "/etc/autocoder/autocoder_config"
    environment_settings: Optional[AutocoderEnvironment] = None
    build_on_current_code: bool = True
    
    

class Autocoder:
    '''
    Autocoder is a class that manages the process of generating, evaluating, validating, and repairing code using an AI assistant. It is 
    designed to be flexible and configurable for various use cases.
    
    The assistants API is gone in August 2026. We now use the responses API. We OWN the memory. More complex because we have to manage 
    the threads and messages. We must ensure the following workflow:
        - We use a "system" message for the initial prompt and instructions. This is static and set at initialization.
        - We use "user" messages for the code generation and repair prompts. These are dynamic and set at runtime.
        - We store the messages in a "thread" in our configuration folder. This thread is then loaded for each autcoder.
        - autocoder is "defined" as the `module_name`.
        
    Code configuration and interacting in a production architecture:
    - We have a configuration directory (default ~/.autocoder_config) where we store:
        - A folder for each module_name (e.g. ~/.autocoder_config/code/)
        - `module_name` is the directory in which the code will be stored.
        - `filename` is the actual file. We would set this so our code can be used e.g. a FastAPI route could auto 
            generate and then be imported and used directly using the `module_name` and `filename` to find the code. If `filename` is not set, we generate a unique filename each time.
    '''

    def __init__(self, 
                    input_data: str,
                    process_prompt: str,
                    
                    validator: BaseValidator,
                    client: BaseAIClient,
                    
                    
                    
                    on_status_message: Optional[Callable[[str], None]] = None,
                    on_save_history: Optional[Callable[[str], None]] = None,
                    process_filter_function: Optional[Callable[[str], bool]] = None,
                    
                    verification_prompt: Optional[str] = None,
                    max_retries: Optional[int] = 3,
                    config: Optional[AutocoderConfig] = None,
                ):
        
        
        self._input_data = input_data.strip()
        
        
        self._client = client
        
        self._validator = validator
        
        # SETUP CONFIG AND ENVIRONMENT SETTINGS /////////////////////////////////////////////////////////
        # Normalize config ONCE
        if config is None:
            config = AutocoderConfig()
        elif isinstance(config, dict):
            config = AutocoderConfig.model_validate(config)

        self._config = config

        # Extract environment settings WITHOUT recreating
        if self._config.environment_settings is None:
            self._environment_settings = AutocoderEnvironment()
        else:
            self._environment_settings = self._config.environment_settings

                
        self._verification_prompt = verification_prompt or ""
        self.max_retries = max_retries
        
        # Ensure filename is not None
        if self._config.filename is None:
            self._config.filename = f"generated_code.py"
        # Set the filename.
        if not self._config.filename.endswith(".py"):
            self._config.filename += ".py"
        # Replace stupid characters in filename
        self._config.filename = (
            self._config.filename
                .replace(" ", "_")
                .replace("/", "_")
                .replace("\\", "_")
                .replace("..", "_")
                .replace("~", "_")
                .replace(":", "_")
                .replace("*", "_")
                .replace("?", "_")
                .replace("\"", "_")
                .replace("<", "_")
                .replace(">", "_")
                .replace("|", "_")
                .replace("-", "_")
                .replace("--", "_")
        )
        
        # Resolve code directory
        config_path = Path(config.code_directory)
        if not config_path.is_absolute():
            config_path = Path.cwd() / config_path
                
        self._config.code_directory = os.path.abspath(config_path)
            
        
        # Ensure code_directory directory exists
        os.makedirs(self._config.code_directory, exist_ok=True)
        # Ensure module_name directory exists
        if config.module_name is None:
            raise AutocoderProcessError("module_name must be provided in the config. Cannot be None.")
        
        module_path = os.path.join(self._config.code_directory, self._config.module_name)
        os.makedirs(module_path, exist_ok=True)

        # Create the code file if it does not exist
        code_file_path = os.path.join(module_path, self._config.filename)
        if not os.path.exists(code_file_path):
            with open(code_file_path, "w") as f:
                f.write("")
            os.chmod(code_file_path, 0o666)
            
            
        
        # Resolve configuration directory
        config_dir_path = Path(self._config.configuration_directory)
        if not config_dir_path.is_absolute():
            config_dir_path = Path.cwd() / config_dir_path
                
        self._config.configuration_directory = os.path.abspath(config_dir_path)
            
        
        # Ensure configuration_directory exists
        os.makedirs(self._config.configuration_directory, exist_ok=True)
        
        # END CONFIG AND ENVIRONMENT SETTINGS /////////////////////////////////////////////////////////
        
        self._on_status_message = on_status_message
        self._on_save_history = on_save_history
        self._process_filter_function = process_filter_function
        
        
        
        # We need to dump the validator and add it to the prompt
        validator_module = sys.modules[self._validator.__class__.__module__]
        validator_source = inspect.getsource(validator_module)
        validator_source = validator_source.replace("{", "{{").replace("}", "}}")  # Escape braces for format string
        
        # We also need to get the code environment and add that to the prompt
        evaluate_code_module = sys.modules[evaluate_generated_code.__module__]
        evaluate_code_source = inspect.getsource(evaluate_code_module)
        evaluate_code_source = evaluate_code_source.replace("{", "{{").replace("}", "}}")  # Escape braces for format string
        
        # The code should be added during the `process` method. We have to repair the code.
        
        # We need to add the allowed modules and exposed globals and builtins to the prompt as well
        additional_libraries = ""
        for k, v in self._environment_settings.allowed_modules.items():
            additional_libraries += f"- {k}\n"
            
        for k, v in self._environment_settings.exposed_globals.items():
            additional_libraries += f"- {k}\n"
            
        formatted_header_prompt = GENERATE_HEADER_PROMPT.format(
            evaluation_environment=evaluate_code_source,
            additional_libraries=additional_libraries,
            validation_requirements=validator_source,
        )
        
        self._process_system_prompt = formatted_header_prompt + "\n" + process_prompt + "\n" + GENERATE_TAIL_PROMPT
        
        
        # ///////////////////////////////////////////////////////////////////////////
        # Setup messages history. The client should manage the message history.
        
        
        
        
    @property
    def input_data(self) -> str:
        return self._input_data
    
    
    
    def process(self,
                engage_ai_agent: bool = True,
                ai_verification: bool = False,
                on_status_message: Optional[Callable[[str], None]] = None,
                on_save_history: Optional[Callable[[str, str], None]] = None,
                process_filter_function: Optional[Callable[[str], bool]] = None,
                ) -> AutocoderProcessResult:
        '''
        Uses the ChatGPTClient history every time. We must handle our own messages and history management. We leverage the ChatGPTClient interface
        '''
        # Helper INNER functions /////////////////////////////////////////////////////////////////////////////////////////////////////////////////
        def return_result(cannot_process: bool,  original_code: str, output: Optional[str], code: Optional[str]) -> AutocoderProcessResult:
            
            # Hash the old code and the new code, and if they are different, save the new code to disk
            original_code_hash = hashlib.sha256(original_code.encode()).hexdigest() if original_code else None
            new_code_hash = hashlib.sha256(code.encode()).hexdigest() if code else None
            if not original_code or original_code_hash != new_code_hash:
                
                if on_save_history is None:
                    log("Saving new code to disk...")
                    self._save_code_to_file(code)
                else:
                    log("Saving new code via on_save_history callback...")
                    on_save_history(code)    
            else:
                log("Code unchanged, not saving.")
                
                
                
            return AutocoderProcessResult(
                cannot_process=cannot_process,
                code=code,
                output=output
            )
        
        # Method primary data structures ///////////////////////////////////////////////////////////////////////////////////////////////////////////
        original_input_data = self._input_data
        
        
        if self._on_status_message is not None:
            on_status_message = self._on_status_message
            
        if self._on_save_history is not None:
            on_save_history = self._on_save_history
            
        if self._process_filter_function is not None:
            process_filter_function = self._process_filter_function
            
        def log(message: str):
            if on_status_message is not None:
                on_status_message(message)
                
            

        
        # Step 1: Native support short circuit
        if process_filter_function is not None:
            if process_filter_function(original_input_data):
                log("Process filter function indicates native support. Bypassing AI processing.")
                return AutocoderProcessResult(
                    output=original_input_data,
                    code=None,
                    cannot_process=False
                )

        # Setup
        original_code = None
        code_prompt = ""
        if "{input}" not in self._process_system_prompt:
            code_prompt = self._process_system_prompt
        else:
            code_prompt = self._process_system_prompt.format(
                input=original_input_data
            )
        
        
        # Step 2: Load cached processor OR request initial generation
        code, code_filename = self.retrieve_saved_code()
        original_code = code or ""


        if not code or not code.strip():
            # If NOT engageing AI agent, cannot proceed
            if not engage_ai_agent:
                raise AutocoderProcessError(f"No cached {self._config.module_name} available, and AI agent engagement is disabled.")
            
            log("Requesting initial code from AI...")
            code = self._generate_code(
                system_prompt=code_prompt,
            )

        
        # Step 3: Evaluation and repair loop
        for attempt in range(1, self.max_retries + 1):
            log(f"Evaluation attempt {attempt}/{self.max_retries}")

            evaluation = evaluate_generated_code(
                code=code, input_data=original_input_data,
                exposed_builtins=self._environment_settings.exposed_builtins if self._environment_settings else None,
                exposed_globals=self._environment_settings.exposed_globals if self._environment_settings else None
            )

            if not evaluation.get("success", False):
                error = evaluation.get("result", "Unknown execution error")
                log(f"Code execution failed: {error}")

                if not engage_ai_agent:
                    return return_result(cannot_process=True, original_code=original_code, output=None, code=code)

                code = self._repair_code(
                    previous_code=code,
                    error_message=str(error),
                )
                
                continue

            output = evaluation.get("result")

            
            # Manual validation
            validation = self._validator.validate(
                output,
            )

            if not validation.is_valid:
                log(f"Manual validation failed: {validation.errors}")

                if not engage_ai_agent:
                    return return_result(cannot_process=True, original_code=original_code, output=None, code=code)

                log(f"Repairing {self._config.module_name} based on validation errors using AI agent...")
                code = self._repair_code(
                    previous_code=code,
                    error_message=str(validation.errors)
                )
                
                continue

            
            # AI verification IF enabled
            if ai_verification:
                log("Running AI verification...")

                
                verification_response = self._ai_verify_code(
                    code=code,
                    input_data=original_input_data,
                    output=output
                )                
                if not verification_response:
                    log("AI verification failed.")

                    code = self._repair_code(
                        previous_code=code,
                        error_message="AI verification failed",
                    )
                    
                    continue


            # Success
            log(f"Processing {self._config.module_name} successful.")
            return return_result(cannot_process=False, original_code=original_code, output=output, code=code)


        # Step 4: Exhausted retries
        raise AutocoderProcessError(
            f"Unable to produce valid {self._config.module_name} after {self.max_retries} attempts."
        )

     
         
    # Helpers ////////////////////////////////////////////////////////////////////////////////
    
    # Generate Code ////////////////////////////////////////////////////////////////////////
    def _generate_code(self, system_prompt: str, messages: List[str] = None) -> str:
        '''
        Gets a JSON response from the assistant and extracts the "code" field. If the response is `cannot_process`, raises an error. Retries
        a few times if the response is not valid JSON or does not contain the expected fields.
        '''
        attempts = 3
        last_error = None
        
        text = None
        job_id = None
        for attempt in range(1, attempts + 1):
            try:
                text, job_id = self._client.get_text(
                    instruction_messages=[{
                        "role": "system",
                        "content": system_prompt
                    }],
                    messages=messages or []
                )
                
                if not text or not text.strip():
                    continue
                
                json_response = json.loads(text)
                
                if json_response.get("cannot_process"):
                    raise AutocoderProcessError("Assistant determined input data cannot be processed.")
                
                return self._strip_code_block(json_response.get("code", ""))
            
            except Exception as e:
                last_error = e
                continue
            
        raise AutocoderBadAIResponseError(
            "Assistant failed after multiple attempts.\n"
            f"Last error: {last_error}"
        )
    
    
    # Repair Loop ////////////////////////////////////////////////////////////////////////
    def _repair_code(self, previous_code: str, error_message: str) -> str:
        '''
        Gets a JSON response from the assistant and extracts the "code" field. If the response is `cannot_process`, raises
        an error. Retries a few times if the response is not valid JSON or does not contain the expected fields.
        
        Makes several attempts to get a valid response from the assistant, and raises an error if it fails after multiple attempts.
        '''
        
        
        if self._on_status_message is not None:
            self._on_status_message("Repairing code with error message: " + error_message)
        
            
        repair_prompt = REPAIR_PROMPT.format(
            error_message=error_message,
            previous_code=previous_code
        )
        last_error = None
        for attempt in range(1, self.max_retries + 1):
            try:
                text, job_id = self._client.get_text(
                    instruction_messages=[{
                        "role": "system",
                        "content": repair_prompt
                    }],
                    messages=[{
                        "role": "user",
                        "content": repair_prompt
                    }]
                )
            except Exception as e:
                last_error = e
                continue
        
            if self._on_status_message is not None:
                self._on_status_message("Repaired code obtained.")
            
            json_response = None
            try:
                json_response = json.loads(text)
            except json.JSONDecodeError as e:
                continue
            except Exception as e:
                raise AutocoderBadAIResponseError(f"AI assistant returned invalid response during repair: {e}\nResponse text: {text}")
            
            
            if json_response.get("cannot_process"):
                raise AutocoderProcessError("Assistant determined input data cannot be processed during repair.")
            
            new_code = self._strip_code_block(json_response.get("code", ""))    
            return new_code
        
        raise AutocoderBadAIResponseError(
            "Assistant failed to provide valid repaired code after multiple attempts.\n"
            f"Last error: {last_error}"
        )

    
    def _ai_verify_code(self, code: str, input_data: str, output: str) -> bool:
        verification_prompt = VERIFICATION_PROMPT.format(
            input=input_data,
            output=output
        )
        
        for attempt in range(1, self.max_retries + 1):
            try:
                text, job_id = self._client.get_text(
                    instruction_messages=[{
                        "role": "system",
                        "content": verification_prompt
                    }],
                    messages=[{
                        "role": "user",
                        "content": verification_prompt
                    }]
                )
                
                if not text or not text.strip():
                    continue
                
                json_response = json.loads(text)
                return json_response.get("is_valid", False)
            
            except Exception as e:
                continue
            
        raise AutocoderBadAIResponseError(
            "Assistant failed to provide valid verification response after multiple attempts."
        )
    
    
    # Caching Code //////////////////////////////////////////////////////////////////
    
    # DONT create files. Just return latest.
    def retrieve_saved_code(self):
        module_path = os.path.join(self._config.code_directory, self._config.module_name)

        if not os.path.exists(module_path):
            return None, None

        for filename in os.listdir(module_path):
            _check_filename = self._config.filename
            if not _check_filename.endswith(".py"):
                _check_filename += ".py"

            if filename == _check_filename:
                path = os.path.join(module_path, filename)
                content = open(path).read()
                if not content.strip():
                    return None, None
                return content, filename

        return None, None


    
    # Do all the backups in one place and saving here.
    def _save_code_to_file(self, code: str):
        """
        Save new code into the module directory.
        Existing file is moved into a cached/ folder with timestamp.
        """

        module_path = os.path.join(
            self._config.code_directory,
            self._config.module_name
        )
        os.makedirs(module_path, exist_ok=True)

        cache_dir = os.path.join(module_path, "cached")
        os.makedirs(cache_dir, exist_ok=True)

        filename = self._config.filename
        if not filename.endswith(".py"):
            filename += ".py"

        # Sanitize filename once (safe-guard)
        filename = (
            filename
                .replace(" ", "_")
                .replace("/", "_")
                .replace("\\", "_")
                .replace("..", "_")
                .replace("~", "_")
                .replace(":", "_")
                .replace("*", "_")
                .replace("?", "_")
                .replace("\"", "_")
                .replace("<", "_")
                .replace(">", "_")
                .replace("|", "_")
                .replace("-", "_")
                .replace("--", "_")
        )

        file_path = os.path.join(module_path, filename)

        # Backup existing file if it exists and is non-empty
        if os.path.exists(file_path):
            with open(file_path, "r") as f:
                existing_content = f.read()

            if existing_content.strip():
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_name = f"{timestamp}_{filename}"
                backup_path = os.path.join(cache_dir, backup_name)
                shutil.move(file_path, backup_path)

        # Write new code
        with open(file_path, "w") as f:
            f.write(code)

        os.chmod(file_path, 0o666)

    
    def _strip_code_block(self, text: str) -> str:
        if "```" not in text:
            return text.strip()
        inside = None
        parts = text.split("```")
        if len(parts) >= 3:
            inside = parts[1]

        if inside is None:
            return text.strip()
        
        return inside.replace("python", "").strip()
