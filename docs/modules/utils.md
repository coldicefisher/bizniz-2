# Utils

`bizniz/utils/`. Small helpers shared across the package.

## Files

| File | Purpose |
|------|---------|
| `code_metadata.py` | Read / write structured metadata blocks at the top of generated code files |
| `json/__init__.py` | Re-exports `clean_llm_json` from `json/llm.py` |
| `json/llm.py` | `clean_llm_json` and `fix_string_escapes` — repair malformed LLM JSON |

## `code_metadata.py`

Saved code files start with a metadata block so `Tester` and others can re-discover the original problem statement when only the file is available (no DB context).

New format:

```python
# BIZNIZ_METADATA_START
# {
#     "problem_statement": "Add two numbers",
#     "saved_at": "2026-04-29T18:00:00+00:00"
# }
# BIZNIZ_METADATA_END

def add(a, b):
    return a + b
```

Public API:

| Function | Purpose |
|----------|---------|
| `read_code_metadata(code) -> dict` | Tries new format first; falls back to a legacy triple-quoted-docstring format that older bizniz versions wrote. Always returns at least `{"problem_statement": str | None}`. |
| `build_metadata_block(metadata) -> str` | Serializes a dict into the `BIZNIZ_METADATA_START`/`END` comment block. |

Constants:

```python
METADATA_START = "# BIZNIZ_METADATA_START"
METADATA_END   = "# BIZNIZ_METADATA_END"
```

Internals:

| Function | Purpose |
|----------|---------|
| `_parse_new_format(code)` | Locate sentinels, strip leading `# `, JSON-decode |
| `_parse_legacy_format(code)` | Regex-match the old `Problem Statement:` triple-quoted block for backward compatibility |

`BaseAIAgent._save_code_to_file` is the writer; `Tester._lookup_problem_statement` is one of the readers.

## `clean_llm_json` (`utils/json/llm.py`)

The repair routine that turns "this looks like JSON but Python's `json.loads` chokes on it" output into valid JSON. Used by every agent right before parsing AI output.

Steps applied in order:

1. Remove zero-width unicode characters (`​`–`‍`, `﻿`).
2. Strip whitespace.
3. Try `json.loads` — if it parses, return as-is.
4. Strip Markdown code fences (```` ```json ... ``` ```` or plain ```` ``` ```).
5. Trim leading text before the first `{` or `[`.
6. Trim trailing text after the last `}` or `]`.
7. Remove trailing commas before `}` / `]`.
8. If still invalid, run `fix_string_escapes` to repair raw control chars and bad backslash sequences inside string literals.

`fix_string_escapes(text)` walks the text, tracking whether the cursor is inside a string. Inside strings:

- Raw `\n`, `\t`, etc. become `\\n`, `\\t`.
- Other control chars become `\uXXXX`.
- Backslashes followed by characters that aren't valid JSON escapes (`"`, `\`, `/`, `b`, `f`, `n`, `r`, `t`, `u<hex><hex><hex><hex>`) are doubled. This handles Gemini emitting Python regex literals like `\W` directly.

## Example

```python
from bizniz.utils.json import clean_llm_json
from bizniz.utils.code_metadata import build_metadata_block, read_code_metadata
import json

# Repair LLM output
raw = '```json\n{"a": 1, "b": "regex \\W+",}\n```'
clean = clean_llm_json(raw)
data = json.loads(clean)
# {'a': 1, 'b': 'regex \\W+'}

# Embed metadata in code
header = build_metadata_block({
    "problem_statement": "Add two numbers",
    "saved_at": "2026-04-29T18:00:00Z",
})
print(header)

# Read it back
meta = read_code_metadata(header + "\n\ndef add(a, b): return a + b\n")
print(meta["problem_statement"])  # "Add two numbers"
```

## Interactions

- **`code_metadata` calls into:** stdlib `json` and `re`.
- **`code_metadata` is called by:** `BaseAIAgent._save_code_to_file`, `Tester._lookup_problem_statement`.
- **`clean_llm_json` is called by:** every agent (`BaseAIAgent.clean_llm_json` delegates here), the tool loop, the agentic debugger.

## Gotchas

- **`read_code_metadata` always succeeds.** Even if the file has no metadata, it returns `{"problem_statement": None}`. Distinguish "found but empty" from "missing" by the value, not the key.
- **The legacy format parser only catches the first matching block.** If a file has multiple `Problem Statement:` blocks for some reason, you get the first.
- **`clean_llm_json` doesn't validate.** It returns repaired text; the caller still calls `json.loads`. If the repair fails, the parse fails and the agent retries.
- **`fix_string_escapes` runs LAST.** Structural fixes (fences, commas) happen first, then escape repair. This order matters because the escape walker is sensitive to outer braces/quotes already being correct.
