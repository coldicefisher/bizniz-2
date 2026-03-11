import re
import json


def clean_llm_json(text: str) -> str:
    """Clean LLM output to extract valid JSON.

    Handles common LLM output issues:
    - Zero-width Unicode characters
    - Markdown code fences (```json ... ```)
    - Leading/trailing non-JSON text
    - Trailing commas before closing brackets
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

    return text
