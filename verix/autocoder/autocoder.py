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
import datetime
from pathlib import Path
import re

from typing import Optional, Callable, Union, Any, Dict, List, Tuple, Literal

from pydantic import BaseModel, Field
from pydantic import ValidationError


from openai import AzureOpenAI, OpenAI

from verix.autocoder.base_validator import BaseValidator, ValidationResult

from verix.autocoder.clients.chatgpt.messages import Message, MessageList, normalize_messages
from verix.autocoder.prompts.repair_prompt import REPAIR_PROMPT, REPAIR_PROMPT_INSTRUCTIONS
from verix.autocoder.prompts.verification_prompt import VERIFICATION_PROMPT, VERIFICATION_PROMPT_INSTRUCTIONS
from verix.autocoder.prompts.generate_prompts import GENERATE_HEADER_PROMPT, GENERATE_TAIL_PROMPT

from verix.autocoder.clients.chatgpt.types.response_format import ResponseFormat
# from verix.autocoder.evaluate_code import evaluate_generated_code
from verix.autocoder.clients.base_ai_client import BaseAIClient

from verix.autocoder.types import (
    AutocoderProcessError,
    AutocoderBadAIResponseError,
    AutocoderProcessResult,
    AutocoderAIVerificationResult,
    AutocoderFailedError,
    AutocoderFailedErrorList,
    AutocoderOnEventCallback,
    
)

from verix.autocoder.prompts.prompt_schemas import (
    RepairPromptSchema,
    VerificationPromptSchema,
    GeneratePromptSchema,
)
from verix.environment.base_environment import BaseExecutionEnvironment
from verix.environment.types import (
    ExecutionCallSpec,
    ExecutionEnvironmentResult,
    ExecutionEnvironmentErrorDetails,
    ExecutionTrace
)
from verix.workspace.base_workspace import BaseWorkspace


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
                    
                    client: BaseAIClient,
                    environment: BaseExecutionEnvironment,
                    workspace: BaseWorkspace, # REQUIRED: We need to be able to save and load files. This is key to our workflow.
                    max_retries: Optional[int] = 5,
                    
                    configuration_directory: Optional[str] = None,
                    
                    
                    on_event: Optional[Callable[[AutocoderOnEventCallback], None]] = None,
                    on_status_message: Optional[Callable[[str], None]] = None,
                ):
        
        
        # Instantiation Guards ////////////////////////////////////////////////////////////////////////////
        if not isinstance(environment, BaseExecutionEnvironment) or issubclass(type(environment), BaseExecutionEnvironment):
            raise ValueError("environment must be an instance of a class that inherits from BaseExecutionEnvironment.")
        
        if not inspect.isclass(validator):
            raise AutocoderProcessError("Validator must be a class that inherits from BaseValidator, not an instance.")
        if not issubclass(validator, BaseValidator):
            raise AutocoderProcessError("Validator must be a class that inherits from BaseValidator.")
        
        if not isinstance(client, BaseAIClient) or issubclass(type(client), BaseAIClient):
            raise ValueError("client must be an instance of a class that inherits from BaseAIClient.")
        
        
        # End Instantiation Guards ////////////////////////////////////////////////////////////////////////////
        
        # Encapsulation of protected attributes.
        self._client = client
        self._environment = environment
        self._workspace = workspace
        
        
        self.max_retries = max_retries
        
        
        # SETUP CONFIG AND ENVIRONMENT SETTINGS /////////////////////////////////////////////////////////
        
        # Resolve configuration directory
        self._configuration_directory = configuration_directory
        config_dir_path = Path(self._configuration_directory)
        if not config_dir_path.is_absolute():
            config_dir_path = Path.cwd() / config_dir_path
                
        self._configuration_directory = os.path.abspath(config_dir_path)
        
        # Ensure configuration_directory exists
        os.makedirs(self._configuration_directory, exist_ok=True)
        
        # END CONFIG AND ENVIRONMENT SETTINGS /////////////////////////////////////////////////////////
        
        # SETUP EVENTS AND CALLBACKS ////////////////////////////////////////////////////////////////////////
        # General event callback for all stages of the process. Provides a unified interface for handling events.
        self._on_event = on_event 
         # Callback specifically for status messages, which can be used for real-time updates in a UI or websocket.
        self._on_status_message = on_status_message
        
        
        
        formatted_header_prompt = GENERATE_HEADER_PROMPT.format(
            # evaluation_environment=evaluate_code_source,
            evaluation_environment=environment.describe(),
            validation_requirements=self._get_validator_description(),
        )
        
        self._process_system_prompt = formatted_header_prompt + GENERATE_TAIL_PROMPT
        
        
        # ///////////////////////////////////////////////////////////////////////////
        # Setup messages history.
        self._message_history: List[dict] = []
        # Append the system prompt to the front of the messages history for context. This is important for the AI to understand the instructions and requirements.
        self.add_messages_to_history([{
            "role": "system",
            "content": self._process_system_prompt  
        }])
        
        
    # END CONSTURUCTOR ////////////////////////////////////////////////////////////////////////////
    
    
        
    def _get_validator_description(self) -> str:
        '''
        Gets the description of the validator class to be included in the prompt.
        '''
        if self._validator is None:
            return "No validator provided."
        
        # We will send the validator source code to the Agent for context.
        validator_module = sys.modules[self._validator.__module__]
        validator_source = inspect.getsource(validator_module)
        validator_prompt = f"""
        I need you to provide a summary of the validation code that is used to validate 
        code that is generated by an AI agent. Provide a text description of the validation code
        so that the AI agent can use that description to understand how the validation works to write
        the code to pass the validation requirements. Here is the validation code:
        
        {validator_source}
        """
        self._client.get_text(messages=[{
            "role": "user",
            "content": validator_prompt
        }])
    
    
    def _get_environment_description(self) -> str:
        '''
        Gets the description of the execution environment to be included in the prompt.
        '''
        description = "The code will be executed in a restricted Python environment with the following settings:\n\n"
        description += "Allowed Modules:\n"
        for module in self._environment_settings.allowed_modules:
            description += f"- {module}\n"
        
        description += "\nExposed Globals:\n"
        for global_var in self._environment_settings.exposed_globals:
            description += f"- {global_var}\n"
        
        description += "\nExposed Builtins:\n"
        for builtin in self._environment_settings.exposed_builtins:
            description += f"- {builtin}\n"
        
        return description
        
    @property
    def input_data(self) -> str:
        return self._input_data
    
    @input_data.setter
    def input_data(self, value: str):
        if not isinstance(value, str):
            raise ValueError("input_data must be a string.")
        self._input_data = value.strip()
        
        
    @property
    def message_history(self) -> List[dict]:
        # We must truncate the message history to keep this thing from blowing up. 
        MAX_HISTORY = 20
        _history = self._message_history
        
        if len(_history) > MAX_HISTORY:
            # Include the first message
            truncated_history = _history[:1] + _history[-(MAX_HISTORY-1):]
            return truncated_history
        
        return _history
    
    
    def process(self,
                process_prompt: str,
                filename: str,        
                
                
                # EVENTS AND CALLBACKS
                on_event: Optional[Callable[[AutocoderOnEventCallback], None]] = None, # Fired at key events of the process function, such as start and end of each stage, and on each retry attempt.
                on_status_message: Optional[Callable[[str], None]] = None, # Fired at key points in the lifecycle of the process function. Useful for websockets.
                on_save_code: Optional[Callable[[str], None]] = None, # Fired when new code is generated and ready to be saved. Provides the new code as a string. Useful for saving to disk or database, or for triggering other actions such as testing or deployment.
                
    ) -> AutocoderProcessResult:
        
        '''
        Uses the ChatGPTClient history every time. We must handle our own messages and history management. We leverage the ChatGPTClient interface
        '''
        
        # Setup events ////////////////////////////////////////////////////////////////////
        if on_event is not None:
            self._on_event = on_event
            
        if on_status_message is not None:
            self._on_status_message = on_status_message
        
        # Helper INNER functions /////////////////////////////////////////////////////////////////////////////////////////////////////////////////
        def return_result(original_code: str, output: Optional[str], code: Optional[str]) -> AutocoderProcessResult:
            
            # Hash the old code and the new code, and if they are different, save the new code to disk
            original_code_hash = hashlib.sha256(original_code.encode()).hexdigest() if original_code else None
            new_code_hash = hashlib.sha256(code.encode()).hexdigest() if code else None
            if not original_code or original_code_hash != new_code_hash:
                
                
                log("Saving new code to disk...")
                self._save_code_to_file(code, filename)
                
                if on_save_code is not None:
                    log("Saving new code via on_save_code callback...")
                    on_save_code(code)    
            else:
                log("Code unchanged, not saving.")
                
                
                
            return AutocoderProcessResult(
                code=code,
                output=output
            )
        
        # HELPER function to log status messages. For updates like to a websocket.
        def log(message: str):
            if on_status_message is not None:
                on_status_message(message)
                
        # END Helper INNER functions /////////////////////////////////////////////////////////////////////////////////////////////////////////////////
        
        
            
        # Setup code and code prompt.
        cached_code = None
        working_code = None
        call_spec = None
        formatted_process_prompt = ""
        formatted_process_prompt = process_prompt
        
        
        # Step 2: Load filename from workspace. If it doesn't exist, generate code with AI agent if enabled. If AI agent is not enabled, return error.
        if self._workspace.exists(filename=filename):
            log(f"Loading existing code for {filename} from workspace...")
            cached_code = self._workspace.read_file(path=filename)
            

            
        log("Requesting initial code from AI...")
        try:
            working_code, call_spec = self._generate_code(
                messages=[Message(role="user", content=formatted_process_prompt)]
            )
        
        except Exception as e:
            working_code = "ERROR: Failed to generate initial code from AI. " + str(e)
            
            
        
        # Step 3: Evaluation and repair loop
        for attempt in range(1, self.max_retries + 1):
            log(f"Evaluation attempt {attempt}/{self.max_retries}")

            evaluation = self._environment.execute(
                code=working_code,
                call_spec=call_spec
            )
            if not evaluation.get("success", False):
                error = ExecutionEnvironmentErrorDetails.from_dict(evaluation.get("error", {}), stage=evaluation.get("stage"))
                
                if error is None:
                    raise AutocoderProcessError("Code evaluation failed without providing an error message.")
                
                log(f"Code execution failed: {str(error)}")

                
                code = self._repair_code(
                    previous_code=code,
                    error_message=str(error),
                )
                
                continue

            output = evaluation.get("result")

            # Success
            log(f"Processing {filename} successful.")
            return return_result(original_code=cached_code, output=output, code=code)


        # Step 4: Exhausted retries
        raise AutocoderProcessError(
            f"Unable to produce valid code for {filename} after {self.max_retries} attempts."
        )

     
         
    # Helpers ////////////////////////////////////////////////////////////////////////////////
    
    # Generate Code ////////////////////////////////////////////////////////////////////////
    def _generate_code(self, messages: List[Message]) -> str:
        '''
        Gets a JSON response from the assistant and extracts the "code" field. Retries
        a few times if the response is not valid JSON or does not contain the expected fields.
        '''
        attempts = 3
        last_error = None
        
        
        text = None
        job_id = None
        
        self.add_messages_to_history(messages=messages)
        prompt_to_store = "\n".join([f"{m.role}: {m.content}" for m in messages])
        # ACTUAL CODE ///////////////////////////////////////////////////////////////////////////
        for attempt in range(1, attempts + 1):
            try:
                text, job_id, output_messages = self._client.get_text(
                    messages=self.message_history,
                    response_format=ResponseFormat.JSON_SCHEMA,
                    schema=GeneratePromptSchema
                    
                )
                # Add the messages to the history
                self.add_messages_to_history(output_messages)
                
                if not text or not text.strip():
                    continue
                
                text = clean_llm_json(text)
                json_response = json.loads(text)
                    
                
                
                out = self._strip_code_block(json_response.get("code", ""))
                self.emit(AutocoderOnEventCallback(
                    stage="generate",
                    status="success",
                    code=out,
                    prompt=prompt_to_store,
                    response=text,
                    input_data=self._input_data,
                    attempt=attempt
                ))
                return out
            
            except Exception as e:
                last_error = e
                continue
            
        
        self.emit(AutocoderOnEventCallback(
            stage="generate",
            status="failure",
            code=None,
            prompt=prompt_to_store,
            response=text,
            input_data=self._input_data,
            attempt=attempts
        ))
        
        raise AutocoderBadAIResponseError(
            "Assistant failed after multiple attempts.\n"
            f"Last error: {last_error}"
        )
    



    # Repair Loop ////////////////////////////////////////////////////////////////////////
    def _repair_code(self, previous_code: str, error_message: str) -> str:
        '''
        Gets a JSON response from the assistant and extracts the "code" field.
        
        Makes several attempts to get a valid response from the assistant, and
        raises an error if it fails after multiple attempts.
        '''

        if self._on_status_message is not None:
            self._on_status_message("Repairing code with error message: " + error_message)


        format_args = {
            "error_message": error_message,
            "previous_code": previous_code,
            "input_data": self._input_data,
        }
        if "{instructions}" in REPAIR_PROMPT:
            format_args["instructions"] = self._process_system_prompt

        safe_args = {
            k: v for k, v in format_args.items()
            if f"{{{k}}}" in REPAIR_PROMPT
        }

        repair_prompt = REPAIR_PROMPT.format(**safe_args)

        last_error = None

        # Add the user message to the message history for context in the repair attempts. 
        new_messages = [
            {
                "role": "user",
                "content": repair_prompt
            }
        ]
        self.add_messages_to_history(new_messages)
        
        
        for attempt in range(1, 4):
            new_code = None
            text = None
            job_id = None
            try:
                text, job_id, output_messages = self._client.get_text(
                    messages=self.message_history,
                    response_format=ResponseFormat.JSON_SCHEMA,
                    schema=RepairPromptSchema,
                )
                
                self.add_messages_to_history(output_messages)
                
                if not text or not text.strip():
                    last_error = "Empty response from AI assistant"
                    continue

                if self._on_status_message is not None:
                    self._on_status_message(f"Repair attempt {attempt}: response received.")

                try:
                    text = clean_llm_json(text)
                    json_response = json.loads(text)
                except json.JSONDecodeError as e:
                    last_error = e
                    continue

                
                new_code = self._strip_code_block(json_response.get("code", ""))

                if not new_code or not new_code.strip():
                    last_error = "AI returned empty code during repair."
                    continue


                self.emit(AutocoderOnEventCallback(
                    stage="repair",
                    status="success",
                    code=new_code,
                    prompt=repair_prompt,
                    response=text,
                    input_data=self._input_data,
                    attempt=attempt
                ))
                return new_code


            except AutocoderProcessError:
                self.emit(AutocoderOnEventCallback(
                    stage="repair",
                    status="failure",                     
                    code=new_code,
                    prompt=repair_prompt,
                    response=text,
                    input_data=self._input_data,
                    attempt=attempt
                ))
                raise

            except Exception as e:
                last_error = e
                self.emit(AutocoderOnEventCallback(
                    stage="repair",
                    status="failure",
                    code=new_code,
                    prompt=repair_prompt,
                    response=text,
                    input_data=self._input_data,
                    attempt=attempt
                ))
                continue


        self.emit(AutocoderOnEventCallback(
            stage="repair",
            status="failure",
            code=None,
            prompt=repair_prompt,
            response=text if 'text' in locals() else None,
            input_data=self._input_data,
            attempt=self.max_retries
        ))
        raise AutocoderBadAIResponseError(
            "Assistant failed to provide valid repaired code after multiple attempts.\n"
            f"Last error: {last_error}"
        )

    
    # def _ai_verify_code(self, verification_prompt: str, code: str, input_data: str, output: str) -> AutocoderAIVerificationResult:
    #     kwargs = {
    #         "instructions": self._process_system_prompt,
    #         "verification_prompt": verification_prompt,
    #         "input": input_data,
    #         "output": output,
    #         "code": code,
            
    #     }
        
    #     formatted_verification_prompt = VERIFICATION_PROMPT.format(**kwargs)
    #     new_messages = [
    #         {
    #             "role": "system",
    #             "content": VERIFICATION_PROMPT_INSTRUCTIONS
    #         },
    #         {
    #             "role": "user",
    #             "content": formatted_verification_prompt
    #         }
    #     ]
        
    #     # We WANT the repair loop to see the verification. BUT, don't send it to the AI agent verifier.
        
    #     for attempt in range(1, self.max_retries + 1):
    #         try:
    #             text, job_id, output_messages = self._client.get_text(
    #                 messages=new_messages,
    #                 response_format=ResponseFormat.JSON_SCHEMA,
    #                 schema=VerificationPromptSchema,
    #             )
                
    #             if not text or not text.strip():
    #                 continue
                
                
                
    #             text = clean_llm_json(text)
    #             json_response = json.loads(text)
                
    #             # ADD ONLY if json can load. Otherwise just iterate until valid response.
    #             # We JUST store the verification response so the repair agent can see it.
    #             self.add_messages_to_history([{
    #                 "role": "user",
    #                 "content": f"AI Verification Response:\n{text}"
    #             }])
                
    #             self.emit(AutocoderOnEventCallback(
    #                 stage="ai_verification",
    #                 status="success",
    #                 code=code,
    #                 prompt=verification_prompt,
    #                 response=text,
    #                 input_data=self._input_data,
    #                 attempt=attempt
    #             ))
    #             return AutocoderAIVerificationResult(
    #                 is_valid=json_response.get("is_valid", False),
    #                 code=json_response.get("code"),
    #                 errors=json_response.get("errors")
    #             )
            
    #         except Exception as e:
    #             continue
            
        
    #     self.emit(AutocoderOnEventCallback(
    #         stage="ai_verification",
    #         status="failure",
    #         code=code,
    #         prompt=verification_prompt,
    #         response=text if 'text' in locals() else None,
    #         input_data=self._input_data,
    #         attempt=self.max_retries
    #     ))
    #     raise AutocoderBadAIResponseError(
    #         "Assistant failed to provide valid verification response after multiple attempts."
    #     )
    
    
    # Caching Code //////////////////////////////////////////////////////////////////
    
    
    # Do all the backups in one place and saving here.
    def _save_code_to_file(self, code: str, filename: str):
        """
        Save code to the filename provided in the workspace. If a file with the same name already exists, back it up to a cached/ directory with a timestamp before saving the new code.
        
        """

        full_path = self._workspace.path(filename)
        file_dir = os.path.dirname(full_path)   

        cache_dir = os.path.join(file_dir, "cached")
        os.makedirs(cache_dir, exist_ok=True)

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

        cached_file_path = os.path.join(cache_dir, filename)

        # Backup existing file if it exists and is non-empty
        if os.path.exists(cached_file_path):
            with open(cached_file_path, "r") as f:
                existing_content = f.read()

            if existing_content.strip():
                timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
                backup_name = f"{timestamp}_{filename}"
                backup_path = os.path.join(cache_dir, backup_name)
                shutil.move(cached_file_path, backup_path)
                
        # Write new code
        with open(full_path, "w") as f:
            f.write(code)

        os.chmod(full_path, 0o666)

    
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




    def emit(self, event: AutocoderOnEventCallback):
        if self._on_event:
            self._on_event(event)



    def add_messages_to_history(self, messages: Union[List[Union[Message, dict]], MessageList], throw_error_on_invalid: bool = False):
        normalized_messages = normalize_messages(messages)
        # Only allow one system message at the beginning of the history. 
        
        current_system_messages = [m for m in self._message_history if m.get("role") == "system"]   
        
        for message in normalized_messages:
            is_valid: bool = True
            if not isinstance(message, dict):
                is_valid = False
              
            # ALLOW DUPLICATES - REPAIR CODE COULD LEGITIMATELY RETURN THE SAME CODE.  
            # if is_valid:
            #     # Check that the message does not already exist in the history to avoid duplicates
            #     duplicate_messages = [
            #         m for m in self._message_history
            #         if m.get("role") == message.get("role") and m.get("content") == message.get("content")
            #     ]        
            #     if len(duplicate_messages) > 0:
            #         is_valid = False
                    
            
            if is_valid:
                # Check that if the message is a system message, it is only added if there are no other system messages in the history.
                if message.get("role") == "system":
                    if len(current_system_messages) > 0:
                        is_valid = False
                    
            
            if is_valid:
                if message.get("role") == "system":
                    self._message_history.insert(0, message)
                else:
                    self._message_history.append(message)



def clean_llm_json(text: str) -> str:
    # Remove zero-width characters
    text = re.sub(r'[\u200B-\u200D\uFEFF]', '', text)

    # Trim whitespace
    text = text.strip()

    return text