"""
Shared tool-use conversation loop.

All agentic agents (coder, tester, agentic debugger) use this loop
to iteratively explore the workspace via discovery tools before submitting
their final output.
"""

import json
import re
import time
from typing import Optional, Callable, Dict

from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.clients.chatgpt.types.response_format import ResponseFormat
from bizniz.workspace.base_workspace import BaseWorkspace
from bizniz.utils.json import clean_llm_json

from bizniz.tools.discovery_tools import (
    tool_view_file,
    tool_list_directory,
    tool_search_files,
)


def _parse_retry_after(error_msg: str) -> float:
    """Extract retry-after seconds from a rate limit error message."""
    match = re.search(r"try again in (\d+\.?\d*)\s*s", str(error_msg).lower())
    if match:
        return float(match.group(1))
    return 5.0  # default backoff


def _trim_messages_for_context(messages: list) -> bool:
    """Trim the message list to fit within context limits.

    Strategy: keep system prompt (index 0) and initial user message (index 1),
    drop the oldest tool-use turns from the middle. Returns True if trimming
    was possible, False if already minimal.
    """
    # Need at least system + initial user + latest user = 3 messages
    if len(messages) <= 3:
        return False

    # Remove the oldest pair of assistant+user messages after the initial prompt
    # (i.e. the earliest tool-use turn)
    removed = 0
    idx = 2  # start after system + initial user
    while idx < len(messages) - 1 and removed < 2:
        messages.pop(idx)
        removed += 1

    return removed > 0


class ToolLoopError(Exception):
    pass


class ToolLoopTimeoutError(ToolLoopError):
    pass


class ToolLoopBadResponseError(ToolLoopError):
    pass


def run_tool_loop(
    client: BaseAIClient,
    workspace: BaseWorkspace,
    system_prompt: str,
    initial_user_message: str,
    action_schema: dict,
    terminal_action: str,
    max_turns: int = 10,
    timeout_seconds: int = 300,
    on_status_message: Optional[Callable[[str], None]] = None,
    extra_tool_handlers: Optional[Dict[str, Callable]] = None,
    agent_name: str = "ToolLoop",
) -> dict:
    """
    Run a tool-use conversation loop.

    The LLM returns a JSON action each turn. Discovery tools (view_file,
    list_directory, search_files) are handled automatically. When the LLM
    returns the terminal_action, the parsed action dict is returned.

    Parameters
    ----------
    client:
        AI client instance (ChatGPT or Claude).
    workspace:
        Workspace for file operations.
    system_prompt:
        Full system prompt (agent-specific + discovery appendix).
    initial_user_message:
        The initial task description sent as the first user message.
    action_schema:
        JSON schema for structured output (must include discovery tool actions
        and the terminal action).
    terminal_action:
        The action name that signals the loop should return (e.g. "submit_code").
    max_turns:
        Maximum number of tool-use turns before forcing submission.
    timeout_seconds:
        Wall-clock timeout before forcing submission.
    on_status_message:
        Optional callback for status logging.
    extra_tool_handlers:
        Optional dict mapping action names to handler functions.
        Each handler receives (action_dict, messages) and returns a result string.
    agent_name:
        Name prefix for log messages.

    Returns
    -------
    dict: The parsed action dict from the terminal action.
    """

    def log(msg: str):
        if on_status_message:
            on_status_message(msg)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": initial_user_message},
    ]

    start_time = time.time()
    parse_failures = 0
    max_parse_failures = 5
    rate_limit_backoff = 0.0  # accumulates across turns to prevent rapid re-hitting

    for turn in range(1, max_turns + 1):
        elapsed = time.time() - start_time
        if elapsed > timeout_seconds:
            log(f"{agent_name}: timeout after {int(elapsed)}s — forcing submission")
            messages.append({
                "role": "user",
                "content": (
                    f"TIME LIMIT REACHED. You must submit your output NOW. "
                    f"Use action '{terminal_action}' with your best output based on "
                    f"what you've learned so far."
                ),
            })

        # If we recently hit a rate limit, wait before the next call
        if rate_limit_backoff > 0:
            log(f"{agent_name}: rate limit cooldown — waiting {rate_limit_backoff:.1f}s")
            time.sleep(rate_limit_backoff)
            rate_limit_backoff = 0.0  # reset after waiting

        # Call LLM (with rate-limit retry + exponential backoff)
        try:
            text, _, _ = client.get_text(
                messages=messages,
                use_message_history=False,
                response_format=ResponseFormat.JSON_SCHEMA,
                schema=action_schema,
            )
        except Exception as e:
            from bizniz.clients.chatgpt.errors import OpenAIRateLimit
            from bizniz.clients.errors import AIContextLengthExceeded
            if isinstance(e, AIContextLengthExceeded):
                # Context too large — trim older tool-use turns and retry
                if _trim_messages_for_context(messages):
                    log(f"{agent_name}: context too large — trimmed history ({len(messages)} messages remaining)")
                    continue  # retry with shorter context
                else:
                    log(f"{agent_name}: context too large and cannot trim further")
                    raise ToolLoopBadResponseError(f"Context length exceeded with minimal messages: {e}")
            elif isinstance(e, OpenAIRateLimit):
                wait = _parse_retry_after(str(e))
                log(f"{agent_name}: rate limited — waiting {wait:.1f}s")
                time.sleep(wait + 1.0)
                # Retry once
                try:
                    text, _, _ = client.get_text(
                        messages=messages,
                        use_message_history=False,
                        response_format=ResponseFormat.JSON_SCHEMA,
                        schema=action_schema,
                    )
                except Exception as retry_e:
                    from bizniz.clients.chatgpt.errors import OpenAIRateLimit as RL2
                    if isinstance(retry_e, AIContextLengthExceeded):
                        if _trim_messages_for_context(messages):
                            log(f"{agent_name}: context too large on retry — trimmed history")
                            continue
                    elif isinstance(retry_e, RL2):
                        # Still rate limited — set escalating backoff for next turn
                        rate_limit_backoff = min(wait * 2, 60.0)
                        log(f"{agent_name}: still rate limited, will wait {rate_limit_backoff:.1f}s next turn")
                    else:
                        log(f"{agent_name}: retry failed ({type(retry_e).__name__}: {retry_e})")
                    parse_failures += 1
                    if parse_failures >= max_parse_failures:
                        raise ToolLoopBadResponseError(
                            f"LLM call failed {max_parse_failures} times: {retry_e}"
                        )
                    continue
            else:
                log(f"{agent_name}: LLM call failed ({type(e).__name__}: {e})")
                parse_failures += 1
                if parse_failures >= max_parse_failures:
                    raise ToolLoopBadResponseError(
                        f"LLM call failed {max_parse_failures} times: {e}"
                    )
                continue

        if not text or not text.strip():
            parse_failures += 1
            if parse_failures >= max_parse_failures:
                raise ToolLoopBadResponseError("LLM returned empty response")
            continue

        # Parse action
        try:
            text = clean_llm_json(text)
            action = json.loads(text)
        except (json.JSONDecodeError, Exception) as e:
            parse_failures += 1
            log(f"{agent_name}: failed to parse response ({e})")
            if parse_failures >= max_parse_failures:
                raise ToolLoopBadResponseError(
                    f"Failed to parse LLM response after {max_parse_failures} attempts"
                )
            messages.append({"role": "assistant", "content": text})
            messages.append({
                "role": "user",
                "content": "Your response was not valid JSON. Please try again.",
            })
            continue

        messages.append({"role": "assistant", "content": text})

        action_type = action.get("action", "")
        path = action.get("path", "")

        # Terminal action — return the parsed result
        if action_type == terminal_action:
            return action

        # Discovery tools
        if action_type == "view_file":
            log(f"{agent_name}: viewing {path}")
            result = tool_view_file(workspace, path)
        elif action_type == "list_directory":
            log(f"{agent_name}: listing {path or '.'}")
            result = tool_list_directory(workspace, path)
        elif action_type == "search_files":
            log(f"{agent_name}: searching for '{path}'")
            result = tool_search_files(workspace, path)
        elif extra_tool_handlers and action_type in extra_tool_handlers:
            log(f"{agent_name}: {action_type} {path[:80] if path else ''}")
            result = extra_tool_handlers[action_type](action, messages)
        else:
            result = f"Unknown action '{action_type}'."

        # Build tool result with turn budget warning
        tool_result = f"[TOOL RESULT: {action_type}(\"{path}\")]\n{result}"

        remaining = max_turns - turn
        if remaining <= 2:
            tool_result += (
                f"\n\n⚠️ WARNING: You have {remaining} turn(s) remaining. "
                f"You MUST use action '{terminal_action}' on your next turn with "
                f"your complete output. Do NOT use any more discovery tools."
            )
        elif remaining <= 4:
            tool_result += (
                f"\n\nNote: {remaining} turns remaining. Start preparing your "
                f"'{terminal_action}' submission."
            )

        messages.append({
            "role": "user",
            "content": tool_result,
        })

    # Exhausted turns — force submission
    log(f"{agent_name}: max turns reached — forcing submission")
    messages.append({
        "role": "user",
        "content": (
            f"You have used all available turns. You MUST submit now. "
            f"Use action '{terminal_action}' with your best output."
        ),
    })

    # Final forced submission — try up to 3 times, trimming context on overflow
    for _final_attempt in range(3):
        try:
            text, _, _ = client.get_text(
                messages=messages,
                use_message_history=False,
                response_format=ResponseFormat.JSON_SCHEMA,
                schema=action_schema,
            )
            text = clean_llm_json(text)
            action = json.loads(text)

            if action.get("action") == terminal_action:
                return action
            break  # got a response but wrong action — don't retry
        except Exception as e:
            from bizniz.clients.chatgpt.errors import OpenAIRateLimit
            from bizniz.clients.errors import AIContextLengthExceeded
            if isinstance(e, AIContextLengthExceeded):
                if _trim_messages_for_context(messages):
                    log(f"{agent_name}: context too large on final submission — trimmed history")
                    continue
                raise ToolLoopBadResponseError(f"Final submission failed — context too large: {e}")
            elif isinstance(e, OpenAIRateLimit):
                wait = _parse_retry_after(str(e))
                log(f"{agent_name}: rate limited on final submission — waiting {wait:.1f}s")
                time.sleep(wait + 1.0)
                continue
            raise ToolLoopBadResponseError(f"Final forced submission failed: {e}")

    raise ToolLoopTimeoutError(
        f"LLM did not submit '{terminal_action}' after {max_turns} turns + forced attempt"
    )
