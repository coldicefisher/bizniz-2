from abc import ABC, abstractmethod
import os

import shutil
import datetime

from typing import Optional, Callable, Union, Any, Dict, List, Tuple, Literal


from bizniz.core.types import Message, normalize_messages
from bizniz.core.client import BaseAIClient

from bizniz.environment.base_environment import BaseExecutionEnvironment
from bizniz.environment.types import ExecutionCallSpec
from bizniz.workspace.base_workspace import BaseWorkspace

from bizniz.utils.json import clean_llm_json
from bizniz.utils.code_metadata import build_metadata_block

class BaseAIAgent(ABC):
    '''
    '''

    def __init__(self,

                    client: BaseAIClient,
                    environment: BaseExecutionEnvironment,
                    workspace: BaseWorkspace, # REQUIRED: We need to be able to save and load files. This is key to our workflow.

                    max_message_history_length: Optional[int] = 40, # The maximum number of messages to keep in the message history.
                    max_retries: Optional[int] = 5,

                    # EVENTS AND CALLBACKS
                    on_event: Optional[Callable[..., None]] = None,
                    on_status_message: Optional[Callable[[str], None]] = None,

                ):


        # Instantiation Guards ////////////////////////////////////////////////////////////////////////////
        if not isinstance(environment, BaseExecutionEnvironment):
            raise ValueError("environment must be an instance of a class that inherits from BaseExecutionEnvironment.")

        if not isinstance(client, BaseAIClient):
            raise ValueError("client must be an instance of a class that inherits from BaseAIClient.")

        if not isinstance(max_message_history_length, int) or max_message_history_length <= 0:
            raise ValueError("max_message_history_length must be a positive integer.")

        if not isinstance(max_retries, int) or max_retries <= 0:
            raise ValueError("max_retries must be a positive integer.")
        # End Instantiation Guards ////////////////////////////////////////////////////////////////////////////

        # Encapsulation of protected attributes.
        self._client = client
        self._environment = environment
        self._workspace = workspace

        # Tag the client with the agent class name so cost-tracker records
        # show which agent each AI call belongs to (coder, tester,
        # engineer, architect, agentic_debugger, …). Best-effort —
        # if the client doesn't accept the attribute we silently skip.
        try:
            self._client._caller_agent = type(self).__name__.lower()
        except Exception:
            pass


        self.max_retries = max_retries
        self._max_message_history_length = max_message_history_length



        # SETUP EVENTS AND CALLBACKS ////////////////////////////////////////////////////////////////////////
        # General event callback for all stages of the process. Provides a unified interface for handling events.
        self._on_event = on_event
         # Callback specifically for status messages, which can be used for real-time updates in a UI or websocket.
        self._on_status_message = on_status_message



        # ///////////////////////////////////////////////////////////////////////////
        # Setup messages history.
        self._system_prompt_override: Optional[str] = None
        self._message_history: List[dict] = []
        # Append the system prompt to the front of the messages history for context. This is important for the AI to understand the instructions and requirements.
        self.add_messages_to_history([{
            "role": "system",
            "content": self._process_system_prompt
        }])


    # END CONSTURUCTOR ////////////////////////////////////////////////////////////////////////////

    def clear_message_history(self):
        """Reset message history to only the system prompt."""
        self._message_history = []
        self.add_messages_to_history([{
            "role": "system",
            "content": self._system_prompt_override or self._process_system_prompt
        }])

    def set_system_prompt_override(self, prompt: str):
        """Override the system prompt (used for language-conditional prompts)."""
        self._system_prompt_override = prompt
        # Re-initialize message history with the new system prompt
        self._message_history = []
        self.add_messages_to_history([{
            "role": "system",
            "content": prompt
        }])

    # ATTRIBUTES AND PROPERTIES ////////////////////////////////////////////////////////////////////////////
    @property
    def message_history(self) -> List[dict]:
        '''
        Returns history with truncation. The system prompt is always included at the front.
        '''
        MAX_HISTORY = self._max_message_history_length
        _history = self._message_history

        if len(_history) > MAX_HISTORY:
            # Include the first message
            truncated_history = _history[:1] + _history[-(MAX_HISTORY-1):]
            return truncated_history

        return _history



    # Caching Code //////////////////////////////////////////////////////////////////


    # Do all the backups in one place and saving here.
    def _save_code_to_file(self, code: str, filename: str, prompt: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None):
        """
        Save code to the filename in the workspace, optionally with a structured
        metadata header.  If a cached copy already exists it is timestamped and
        rotated before the new file is written.

        Parameters
        ----------
        code:
            The Python source to save.
        filename:
            Workspace-relative filename.
        prompt:
            The problem-statement / prompt that produced this code.  Written
            into the metadata block when provided.
        metadata:
            Additional key-value pairs to embed in the metadata block.
        """

        full_path = self._workspace.path(filename)
        file_dir = os.path.dirname(full_path)

        cache_dir = os.path.join(file_dir, "cached")
        os.makedirs(cache_dir, exist_ok=True)

        if not filename.endswith(".py"):
            filename += ".py"

        # Sanitize filename for the cache copy (safe-guard against bad chars)
        sanitized = (
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

        cached_file_path = os.path.join(cache_dir, sanitized)

        # Rotate existing cached file if it is non-empty
        if os.path.exists(cached_file_path):
            with open(cached_file_path, "r") as f:
                existing_content = f.read()

            if existing_content.strip():
                timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
                backup_path = os.path.join(cache_dir, f"{timestamp}_{sanitized}")
                shutil.move(cached_file_path, backup_path)

        # Build the metadata block
        meta = metadata.copy() if metadata else {}
        if prompt is not None:
            meta["problem_statement"] = prompt
        meta.setdefault("saved_at", datetime.datetime.now(datetime.timezone.utc).isoformat())

        with open(full_path, "w") as f:
            if meta:
                f.write(build_metadata_block(meta))
                f.write("\n\n")
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




    def emit(self, event):
        if self._on_event:
            self._on_event(event)




    def add_messages_to_history(self, messages: Union[List[Union[Message, dict]], list]):
        normalized_messages = normalize_messages(messages)
        # Only allow one system message at the beginning of the history.

        current_system_messages = [m for m in self._message_history if m.get("role") == "system"]

        for message in normalized_messages:
            is_valid: bool = True
            if not isinstance(message, dict):
                is_valid = False

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



    def get_metadata(self, prompt: str) -> Dict[str, Any]:
        return {
            "problem_statement": prompt,
        }

    @property
    @abstractmethod
    def _process_system_prompt(self) -> str:
        '''
        Returns the system prompt injected at the start of the message history.
        Subclasses must implement this to define their AI persona and instructions.
        '''
        ...


    def clean_llm_json(self, text: str) -> str:
        return clean_llm_json(text)
