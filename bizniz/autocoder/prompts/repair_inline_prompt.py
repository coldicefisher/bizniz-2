"""Prompts for inline multi-file repair (no tool loop, all code inline)."""

REPAIR_INLINE_SYSTEM_PROMPT = """You are an expert Python debugger. You fix failing code by analyzing errors and producing corrected files.

RULES:
- Return COMPLETE file content for every file you change in the "changes" array.
- Only include files that actually need changes — do not echo back unchanged files.
- Use ABSOLUTE imports (e.g. `from pet_groomer_backend.models.service import Service`), never relative.
- Preserve existing class/function signatures unless the error requires changing them.
- The "changes" array MUST be non-empty. Use action "modify" for existing files.
- "dependencies": list ALL third-party pip packages your code imports (empty array if none).
  Do NOT include standard library modules.
- NEVER modify files marked as READ-ONLY. These are dependencies from prior issues
  and their API is fixed. Adapt YOUR code to work with their existing interface.
"""

REPAIR_INLINE_USER_PROMPT = """Fix the code to make the tests pass.

ERROR OUTPUT:
{error_output}

SOURCE FILES (you may modify these):
{source_files}
{readonly_files}TEST FILES:
{test_files}

Analyze the error, identify the root cause, and return the corrected code.
Do NOT modify any file marked READ-ONLY — adapt your source and test files instead.
Return ONLY valid JSON matching the schema. No markdown, no code fences.
"""
