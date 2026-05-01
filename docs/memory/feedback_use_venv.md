---
name: Run Python via the project venv
description: System Python is too bare to run bizniz; always use .venv
type: feedback
originSessionId: 44c643bd-6fd0-4168-b18b-8f23a5343205
---
Always invoke `/home/jamey/bizniz/.venv/bin/python` to run anything in this project.

**Why:** System Python on this Arch box is 3.14 with almost nothing installed (no openai, anthropic, google-genai, dotenv, yaml). PEP 668 also blocks `pip install --user`. The project venv at `/home/jamey/bizniz/.venv` has the editable install plus all required deps (openai, anthropic, google-genai, python-dotenv, pyyaml, pydantic, fastapi, etc.).

**How to apply:** Whenever running a script in `examples/` or testing imports, use `/home/jamey/bizniz/.venv/bin/python -u …`. Don't suggest `python3 examples/foo.py` — it'll fail with ModuleNotFoundError. If new deps are needed, install via `/home/jamey/bizniz/.venv/bin/pip install …`.
