"""
Utilities to read and write structured metadata in saved code files.

New format (machine-readable, also human-readable):

    # BIZNIZ_METADATA_START
    # {
    #     "problem_statement": "...",
    #     "saved_at": "2026-03-07T00:00:00Z"
    # }
    # BIZNIZ_METADATA_END

Legacy format (backwards-compatible, produced by older _save_code_to_file):

    \"\"\"
    Problem Statement:
    ===========================
    # line one of the prompt
    # line two of the prompt
    ====...====
    \"\"\"
"""

import json
import re
from typing import Optional

METADATA_START = "# BIZNIZ_METADATA_START"
METADATA_END = "# BIZNIZ_METADATA_END"


def read_code_metadata(code: str) -> dict:
    """
    Parse metadata from a saved code file.

    Tries the new structured format first; falls back to the legacy
    triple-quoted docstring format for files written by older versions.

    Returns
    -------
    dict
        Always contains at least {"problem_statement": str | None}.
        The new format may contain additional keys (e.g. "saved_at", "agent").
    """
    result = _parse_new_format(code)
    if result is not None:
        return result
    return _parse_legacy_format(code)


def build_metadata_block(metadata: dict) -> str:
    """
    Serialize a metadata dict into the structured comment block.

    Example output::

        # BIZNIZ_METADATA_START
        # {
        #     "problem_statement": "Add two numbers"
        # }
        # BIZNIZ_METADATA_END

    """
    json_lines = json.dumps(metadata, indent=4, ensure_ascii=False).splitlines()
    commented = "\n".join(f"# {line}" for line in json_lines)
    return f"{METADATA_START}\n{commented}\n{METADATA_END}"


# ---------------------------------------------------------------------------
# Internal parsers
# ---------------------------------------------------------------------------

def _parse_new_format(code: str) -> Optional[dict]:
    """
    Extract and JSON-parse the block between BIZNIZ_METADATA_START /
    BIZNIZ_METADATA_END sentinel comments.

    Returns None if the sentinels are not present.
    """
    start_idx = code.find(METADATA_START)
    end_idx = code.find(METADATA_END)

    if start_idx == -1 or end_idx == -1 or end_idx <= start_idx:
        return None

    block = code[start_idx + len(METADATA_START):end_idx]
    # Strip the leading "# " from each line
    stripped_lines = []
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            stripped_lines.append(stripped[2:])
        elif stripped == "#":
            stripped_lines.append("")
        elif stripped:
            stripped_lines.append(stripped)

    json_text = "\n".join(stripped_lines).strip()
    if not json_text:
        return {"problem_statement": None}

    try:
        return json.loads(json_text)
    except json.JSONDecodeError:
        return {"problem_statement": None}


def _parse_legacy_format(code: str) -> dict:
    """
    Extract the problem statement from the legacy triple-quoted docstring.

        \"\"\"
        Problem Statement:
        ===========================
        # line one
        # line two
        ====...====
        \"\"\"

    Returns {"problem_statement": str | None}.
    """
    # Match the opening docstring section that contains "Problem Statement:"
    pattern = re.compile(
        r'"""\s*Problem Statement:\s*={3,}\s*(.*?)={3,}\s*"""',
        re.DOTALL,
    )
    match = pattern.search(code)
    if not match:
        return {"problem_statement": None}

    raw_block = match.group(1)
    lines = []
    for line in raw_block.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            lines.append(stripped[2:])

    problem_statement = " ".join(lines).strip() or None
    return {"problem_statement": problem_statement}
