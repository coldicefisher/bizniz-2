---
name: Branch state as of 2026-04-29
description: Two branches; refactor/agent-specialization has Gemini support and is ahead of main by 5 commits
type: project
originSessionId: 44c643bd-6fd0-4168-b18b-8f23a5343205
---
Repo `/home/jamey/bizniz` has two branches.

**`main`** — `30d25f8` (Merge refactor/agent-specialization 2026-04-29). 15 commits ahead of `origin/main` — needs `git push` to ship. Now includes Gemini + skeletons + framing + three-phase + cost tracking + bug fixes + tests + docs. **545 tests pass.**

**`refactor/agent-specialization`** — was the integration branch. Now fully merged into main; safe to delete or reuse for the next iteration. Last 14 commits before merge:
- `8ddfb22` Fix npm package handling (scoped imports, PyPI bypass, package.json dedup)
- `17e3378` Add Google Gemini as third AI provider with 4-tier model support
- `89635e3` Extract core abstractions, merge debuggers, add language strategy pattern
- `31a698b` Move autocoder under agents/, rename clients/chatgpt to clients/openai
- `aa91e7b` Rename dockerfiles/ to infra/ across architect, project, and prompts

The Gemini config the user prefers (set 2026-04-29 in `bizniz.yaml`):
- `architect_model: gemini-flash`, `engineer_model: gemini-flash`, `default_model: gemini-flash-lite`
- progression: `[gemini-flash-lite, gemini-flash, gemini-pro]`

**Why:** Gemini was working better than OpenAI/Claude in the user's prior testing. The refactor branch is the only place Gemini lives.

**How to apply:** Default to `refactor/agent-specialization` for new work unless explicitly told otherwise. Don't merge to main until the refactor stabilizes.
