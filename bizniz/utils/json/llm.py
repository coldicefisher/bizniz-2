import re
import json


_VALID_ESCAPE_CHARS = set('"\\/bfnrt')
_HEX_DIGITS = set('0123456789abcdefABCDEF')


def fix_string_escapes(text: str) -> str:
    """Repair raw control characters and invalid backslash escapes inside JSON
    string literals.

    Two common LLM (Gemini especially) failure modes:
      1. Raw newlines/tabs/control characters embedded in a string value
         instead of being escaped (``\\n``, ``\\t``).
      2. Backslash sequences that are valid in the LLM's source language but
         not in JSON (e.g. ``\\W`` and ``\\d`` from a Python regex). JSON only
         allows ``\\"  \\\\  \\/  \\b  \\f  \\n  \\r  \\t  \\uXXXX``.

    Walks the text and only mutates content inside string literals, leaving
    structural tokens (braces, commas, etc.) untouched.
    """
    result = []
    in_string = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '\\' and in_string:
            if i + 1 >= n:
                result.append('\\\\')
                i += 1
                continue
            nxt = text[i + 1]
            if nxt in _VALID_ESCAPE_CHARS:
                result.append(ch)
                result.append(nxt)
                i += 2
                continue
            if nxt == 'u':
                if i + 5 < n and all(c in _HEX_DIGITS for c in text[i + 2:i + 6]):
                    result.append(text[i:i + 6])
                    i += 6
                    continue
                result.append('\\\\')
                i += 1
                continue
            # Invalid escape sequence \u2014 double the backslash so it decodes
            # as a literal "\\X".
            result.append('\\\\')
            i += 1
            continue
        if ch == '"':
            in_string = not in_string
            result.append(ch)
            i += 1
            continue
        if in_string and ord(ch) < 0x20:
            escape_map = {
                '\n': '\\n',
                '\r': '\\r',
                '\t': '\\t',
                '\x08': '\\b',
                '\x0c': '\\f',
            }
            result.append(escape_map.get(ch, f'\\u{ord(ch):04x}'))
        else:
            result.append(ch)
        i += 1
    return ''.join(result)


def clean_llm_json(text: str) -> str:
    """Clean LLM output to extract valid JSON.

    Handles common LLM output issues:
    - Zero-width Unicode characters
    - Markdown code fences (```json ... ```)
    - Leading/trailing non-JSON text
    - Trailing commas before closing brackets
    - Raw control chars and invalid backslash escapes inside strings
    """
    # Remove zero-width characters
    text = re.sub(r'[\u200B-\u200D\uFEFF]', '', text)

    # Trim whitespace
    text = text.strip()

    # If it's already valid JSON, return as-is
    try:
        json.loads(text)
        return text
    except (json.JSONDecodeError, ValueError):
        pass

    # Strip markdown code fences: ```json ... ``` or ``` ... ```
    fence_match = re.search(r'```(?:json)?\s*\n?(.*?)```', text, re.DOTALL)
    if fence_match:
        candidate = fence_match.group(1).strip()
        # Only use the fence content if it looks like JSON
        if candidate and candidate[0] in ('{', '['):
            text = candidate

    # If text doesn't start with { or [, try to find the first JSON object
    if text and text[0] not in ('{', '['):
        first_brace = text.find('{')
        first_bracket = text.find('[')
        starts = [i for i in (first_brace, first_bracket) if i >= 0]
        if starts:
            text = text[min(starts):]

    # If text doesn't end with } or ], trim trailing non-JSON text
    if text:
        last_brace = text.rfind('}')
        last_bracket = text.rfind(']')
        ends = [i for i in (last_brace, last_bracket) if i >= 0]
        if ends:
            text = text[:max(ends) + 1]

    # Remove trailing commas before } or ] (common LLM mistake)
    text = re.sub(r',\s*([}\]])', r'\1', text)

    # Repair bad escape sequences and raw control chars inside string literals
    # (e.g. Gemini emitting Python regex \W without doubling the backslash).
    # Applied last so the structural fixes above can run on the raw text first.
    try:
        json.loads(text)
    except (json.JSONDecodeError, ValueError):
        text = fix_string_escapes(text)

    # Last-resort: response was truncated mid-string (Gemini hit max_tokens
    # while emitting a long file body). Try to close the open string and any
    # unbalanced braces/brackets so partial content can still be salvaged.
    try:
        json.loads(text)
    except (json.JSONDecodeError, ValueError):
        repaired = _close_truncated_json(text)
        if repaired is not None:
            text = repaired

    return text


def _close_truncated_json(text: str) -> str | None:
    """Best-effort fix for JSON truncated mid-value.

    Walks the text tracking string state and brace/bracket depth. If the text
    ends inside an open string, closes it. Then balances trailing ``}`` / ``]``
    in last-opened-first-closed order. Returns the repaired text only if it
    parses; otherwise returns None.
    """
    stack: list = []
    in_string = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_string:
            if ch == '\\':
                i += 2  # skip escape and its target
                continue
            if ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == '{':
                stack.append('}')
            elif ch == '[':
                stack.append(']')
            elif ch in ('}', ']'):
                if stack and stack[-1] == ch:
                    stack.pop()
        i += 1

    candidate = text
    if in_string:
        candidate += '"'
    # Strip a trailing comma if present (we may now need to close after it)
    candidate = candidate.rstrip()
    if candidate.endswith(','):
        candidate = candidate[:-1]
    while stack:
        candidate += stack.pop()
    try:
        json.loads(candidate)
        return candidate
    except (json.JSONDecodeError, ValueError):
        return None
