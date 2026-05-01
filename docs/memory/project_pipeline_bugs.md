---
name: Two pipeline bugs surfaced 2026-04-29
description: Bugs caught in Gemini end-to-end run that need fixing
type: project
originSessionId: 44c643bd-6fd0-4168-b18b-8f23a5343205
---
Two real bugs surfaced during a 2026-04-29 `auto_architect.py` end-to-end run on the `refactor/agent-specialization` branch with full Gemini config.

**Bug 0 (FIXED 2026-04-29, commit 155cb7e): Invalid \escape in LLM JSON.**
Gemini sometimes emits Python regex source like `r"\W+"` inside a JSON string field without doubling the backslash, producing invalid JSON. Fix: `bizniz/utils/json/llm.py:fix_string_escapes()` walks JSON text and inside string literals doubles any backslash preceding an invalid-escape char so it decodes as literal. Called from `clean_llm_json()` as the final repair step, and from `GeminiClient._sanitize_json()`. Verified end-to-end with 9 test cases.

**Bug 1: Collection-error misclassification (test-side vs source-side).**
Backend Layer 2 (routers) hit `pet_groomer_backend/app.py:7: NameError: FastAPI` — the source was missing the FastAPI import. Pytest exit code 2 (collection error). The orchestrator routed this as "regenerate tests" instead of "repair source," even though the trace clearly points at source. Commit `6d8833b` ("fix collection error classification") tried to address this but the case still slips through. Look at `bizniz/orchestrator/coding_orchestrator.py` around the collection-error branch (~line 317-385) and improve the `is_source_error` detection — match `pet_groomer_backend/.../*.py:` patterns in the traceback.

**Bug 2: Read-only filter blocks valid config fixes.**
Frontend Layer 0 (models) hit a Jest validation error: `roots: ["tests"]` in jest config but no `tests/` directory. gemini-pro identified the right fix target (the jest config) but the orchestrator filtered the change as "read-only" because the config wasn't in the issue's writable `target_files`. Issue eventually passed via a workaround on iter 5. The right fix is to add config files (jest.config, pyproject.toml, package.json) to a permissive list when the failure mode is "config file misconfigured."

**Why:** Both bugs cost real iterations and money — Bug 1 wasted ~5 iterations re-spinning tests when the source was wrong; Bug 2 wasted iterations because the AI knew the fix but couldn't execute.

**How to apply:** When working on the orchestrator failure-classification branch, fix these. Don't fix them prematurely — they may go away once skeletons are wired (FastAPI skeleton would have correct app.py imports; React skeleton would have a working jest config).

**STATUS 2026-04-30**: Both bugs are protected by unit tests AND end-to-end orchestrator-loop recovery tests:

  - Bug 1 fix: `CodingOrchestrator._is_source_import_error` (triple-signal classifier) + branching at line 978 in `coding_orchestrator.py` to `coder.repair_multi` instead of test regeneration.
  - Bug 2 fix: `_CONFIG_FILENAMES` allowlist + writable-scope expansion at line 1649 in `_inline_repair` so config files are handed to the coder as writable AND the read-only filter recognizes them.
  - Recovery tests: `bizniz/orchestrator/tests/test_pipeline_bug_recovery.py` drives `run_multi` end-to-end and asserts the right branch fires (source repair on source-side collection errors; config writes through on jest-config-style failures). Negative controls guard against over-correcting.
  - Classifier unit tests: `bizniz/orchestrator/tests/test_collection_error_classification.py`.
