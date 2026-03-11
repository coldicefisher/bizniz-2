"""
Shared schema builder for tool-use action schemas.

Builds strict JSON schemas that include discovery tool actions plus
agent-specific terminal actions, compatible with OpenAI's strict mode.
"""


# Discovery tool actions available to all agents
DISCOVERY_ACTIONS = ["view_file", "list_directory", "search_files"]


def build_tool_action_schema(
    name: str,
    terminal_action: str,
    terminal_properties: dict,
    terminal_required: list,
    extra_actions: list = None,
) -> dict:
    """
    Build a strict JSON schema for tool-use actions.

    The schema has a flat structure with all properties required (OpenAI strict mode).
    Discovery tool fields (path) and terminal action fields coexist — the LLM
    fills in the relevant ones based on the action type.

    Parameters
    ----------
    name:
        Schema name (e.g. "autocoder_action").
    terminal_action:
        Name of the terminal action (e.g. "submit_code").
    terminal_properties:
        Dict of property_name -> JSON schema type definition for the terminal action.
    terminal_required:
        List of required property names for the terminal action.
    extra_actions:
        Additional non-discovery, non-terminal actions (e.g. ["run_command", "run_tests"]).
    """
    all_actions = DISCOVERY_ACTIONS + [terminal_action]
    if extra_actions:
        all_actions = DISCOVERY_ACTIONS + extra_actions + [terminal_action]

    properties = {
        "thinking": {
            "type": "string",
            "description": "Your reasoning about what to do next.",
        },
        "action": {
            "type": "string",
            "enum": all_actions,
            "description": f"The action to take. Use discovery tools to explore, then '{terminal_action}' to submit.",
        },
        "path": {
            "type": "string",
            "description": "File path, directory path, or search pattern (depending on action). Empty string if not applicable.",
        },
    }

    # Add terminal-action-specific properties
    properties.update(terminal_properties)

    # All properties are required for strict mode
    required = ["thinking", "action", "path"] + terminal_required

    return {
        "name": name,
        "strict": True,
        "schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }
