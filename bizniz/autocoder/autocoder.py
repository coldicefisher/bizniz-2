from abc import ABC, abstractmethod
import os

import shutil
import textwrap
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


from bizniz.autocoder.clients.chatgpt.messages import Message, MessageList, normalize_messages
from bizniz.autocoder.prompts.repair_prompt import REPAIR_PROMPT
from bizniz.autocoder.prompts.verification_prompt import VERIFICATION_PROMPT, VERIFICATION_PROMPT_INSTRUCTIONS
from bizniz.autocoder.prompts.generate_prompts import GENERATE_SYSTEM_INSTRUCTIONS_PROMPT, GENERATE_RETURN_FORMAT_PROMPT

from bizniz.autocoder.clients.chatgpt.types.response_format import ResponseFormat
# from bizniz.autocoder.evaluate_code import evaluate_generated_code
from bizniz.autocoder.clients.base_ai_client import BaseAIClient

from bizniz.autocoder.types import (
    AutocoderProcessError,
    AutocoderBadAIResponseError, 
    AutocoderProcessResult,
    AutocoderAIVerificationResult,
    AutocoderFailedError,
    AutocoderFailedErrorList,
    AutocoderOnEventCallback,
    
)

from bizniz.autocoder.prompts.prompt_schemas import (
    RepairPromptSchema,
    VerificationPromptSchema,
    GeneratePromptSchema,
)
from bizniz.environment.base_environment import BaseExecutionEnvironment
from bizniz.environment.types import (
    ExecutionCallSpec,
    ExecutionEnvironmentResult,
    ExecutionEnvironmentErrorDetails,
    ExecutionTrace
)
from bizniz.workspace.base_workspace import BaseWorkspace

from bizniz.base_ai_agent import BaseAIAgent


class Autocoder(BaseAIAgent):
    '''
    
    '''

    def __init__(self, 
                    
                    client: BaseAIClient,
                    environment: BaseExecutionEnvironment,
                    workspace: BaseWorkspace, # REQUIRED: We need to be able to save and load files. This is key to our workflow.
                    max_retries: Optional[int] = 5,
                    
                    # EVENTS AND CALLBACKS
                    on_event: Optional[Callable[[AutocoderOnEventCallback], None]] = None,
                    on_status_message: Optional[Callable[[str], None]] = None,
                ):
        
        
        super().__init__(
            client=client,
            environment=environment,
            workspace=workspace,
            max_retries=max_retries,
            on_event=on_event,
            on_status_message=on_status_message,
        )
        
        
    # END CONSTURUCTOR ////////////////////////////////////////////////////////////////////////////
    
        
    
    
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
        def return_result(original_code: str, prompt: str, output: Optional[str], new_code: Optional[str]) -> AutocoderProcessResult:
            
            # Hash the old code and the new code, and if they are different, save the new code to disk
            original_code_hash = hashlib.sha256(original_code.encode()).hexdigest() if original_code else None
            new_code_hash = hashlib.sha256(new_code.encode()).hexdigest() if new_code else None
            if not original_code or original_code_hash != new_code_hash:
                
                
                log("Saving new code to disk...")
                self._save_code_to_file(code=new_code, filename=filename, prompt=prompt)
                
                if on_save_code is not None:
                    log("Saving new code via on_save_code callback...")
                    on_save_code(new_code)    
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

            evaluation, call_spec = self._environment.execute(
                code=working_code,
                call_spec=call_spec
            )
            if not evaluation.get("success", False):
                error = ExecutionEnvironmentErrorDetails.from_dict(evaluation.get("error", {}), stage=evaluation.get("stage"))
                
                if error is None:
                    raise AutocoderProcessError("Code evaluation failed without providing an error message.")
                
                log(f"Code execution failed: {str(error)}")

                
                code, call_spec = self._repair_code(
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
                
                text = self.clean_llm_json(text)
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
        attempts = 3
        last_error = None
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
        
        
        for attempt in range(1, attempts + 1):
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
                    text = self.clean_llm_json(text)
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
            attempt=attempts
        ))
        raise AutocoderBadAIResponseError(
            "Assistant failed to provide valid repaired code after multiple attempts.\n"
            f"Last error: {last_error}"
        )

    
    
    # Caching Code //////////////////////////////////////////////////////////////////
    
    
    
    @property
    def get_metadata(self, prompt: str) -> Dict[str, Any]:
        return {
            "environment_description": self._environment.describe(),
            "workspace_description": self._workspace.describe(),
            "message_history_length": len(self._message_history),
            "problem_statement": prompt,
        }
        

