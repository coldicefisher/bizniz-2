# JSON Schemas

Every JSON schema bizniz uses for structured AI output. All are passed via `ResponseFormat.JSON_SCHEMA` to the AI client.

The schema bodies live in:

- `bizniz/architect/prompts/schema.py`
- `bizniz/engineer/prompts/schema.py`
- `bizniz/agents/autocoder/prompts/{prompt_schemas,tool_action_schema}.py`
- `bizniz/autotester/prompts/{schema,tool_action_schema}.py`
- `bizniz/agents/debugger/prompts/{quick_schema,agentic_schema}.py`
- `bizniz/tools/schemas.py` (the schema builder for tool-loop schemas)

Most are constructed in OpenAI **strict mode** — every property is required, `additionalProperties: false`. This makes the schemas verbose but prevents the LLM from inventing extra fields.

---

## `AutoArchitectSchema`

Returned by `AutoArchitect.decompose`. Drives the entire system decomposition.

| Field | Type | Notes |
|-------|------|-------|
| `project_name` | string | Human-readable |
| `project_slug` | string | snake_case (e.g. `pet_groomer`) |
| `description` | string | Overall system description |
| `services[]` | array of objects | One per container |
| `services[].name` | string | |
| `services[].service_type` | enum | `backend` / `frontend` / `database` / `cache` / `proxy` / `worker` / `auth` |
| `services[].framework` | string | e.g. `fastapi`, `react`, `postgres` |
| `services[].language` | string | `python` / `typescript` / `yaml` / etc. |
| `services[].description` | string | |
| `services[].workspace_name` | string | Subdir under project root |
| `services[].port` | integer or null | host port (architect remaps if collision) |
| `services[].depends_on` | string[] | Names of other services |
| `services[].requirements` | string[] | pip / npm packages |
| `services[].skeleton` | enum | `fastapi` / `react` / `angular` / `teams-backend` / `teams-consumer` / `teams-frontend` / `none` |
| `docker_compose` | string | Full docker-compose.yml content |

The skeleton enum is the **source of truth for valid skeleton names** — adding to `_SKELETONS` without updating this enum will cause structured-output rejections.

---

## `AutoEngineerSchema`

Returned by `AutoEngineer.analyze`. Two AI calls use this schema (draft pass + refined pass with architecture context).

| Field | Type | Notes |
|-------|------|-------|
| `business_requirements[]` | string[] | High-level business needs |
| `use_cases[]` | array of `{title, description}` | User-facing scenarios |
| `functional_requirements[]` | string[] | What the system must do |
| `nonfunctional_requirements[]` | string[] | How the system must behave |
| `issues[]` | array of objects | Discrete coding tasks |

Per-issue object:

| Field | Type | Notes |
|-------|------|-------|
| `title` | string | |
| `description` | string | |
| `target_files[]` | array of `{filepath, action: "create" \| "modify" \| "delete"}` | Files this issue writes |
| `test_files[]` | string[] | Test files to write |
| `depends_on[]` | string[] | Titles of issues this depends on (resolved to db_ids later) |
| `suggested_model` | string | Cheaper for simple tasks, stronger for complex |
| `test_setup_hint` | string | Reusable "how to import the app, how to construct a TestClient" hint for integration issues. Empty for standalone units. |

---

## `ArchitecturePlanSchema`

Returned by `AutoEngineer.plan_architecture`.

| Field | Type | Notes |
|-------|------|-------|
| `package_name` | string | snake_case |
| `root_namespace` | string | Same as `package_name` |
| `namespaces[]` | `[{namespace_path, purpose}]` | Sub-packages within the project |
| `domain_models[]` | array | Shared types/classes |
| `domain_models[].class_name` | string | |
| `domain_models[].filepath` | string | |
| `domain_models[].namespace_path` | string | |
| `domain_models[].fields[]` | `[{name, type_hint, description}]` | |
| `domain_models[].methods[]` | `[{name, signature, description}]` | |
| `domain_models[].docstring` | string | |
| `modules[]` | array | Implementation modules |
| `modules[].filepath` | string | |
| `modules[].class_name` | string | Empty string for module-level functions |
| `modules[].namespace_path` | string | |
| `modules[].methods[]` | `[{name, signature, description}]` | |
| `modules[].docstring` | string | |
| `dependencies[]` | array | Import edges |
| `dependencies[].source_filepath` | string | |
| `dependencies[].target_filepath` | string | |
| `dependencies[].import_symbols[]` | string[] | Names imported (empty = star) |

---

## `ArchitectureGovernanceSchema`

Returned by `AutoEngineer.review_drift`.

| Field | Type | Notes |
|-------|------|-------|
| `decision` | enum | `approve` / `reject` / `modify` |
| `reason` | string | |
| `plan_updates` | string | JSON string of partial plan updates if `decision == "modify"`, else empty string |

`plan_updates` is a string (not an object) because OpenAI strict mode doesn't support optional sub-objects gracefully — the engineer parses it as JSON if non-empty.

---

## Autocoder schemas

### `GeneratePromptSchema` and `RepairPromptSchema`

`bizniz/agents/autocoder/prompts/prompt_schemas.py`. Used by single-file `generate` and `repair`.

Both contain a `changes[]` array of `{filepath, code, action}` and an optional `dependencies[]` list.

### `AutocoderGenerateActionSchema` and `AutocoderRepairActionSchema`

`bizniz/agents/autocoder/prompts/tool_action_schema.py`. Built by `bizniz.tools.schemas.build_tool_action_schema(...)`.

Common fields (every tool-loop schema has these):

- `thinking` — chain of thought.
- `action` — enum that includes `view_file`, `list_directory`, `search_files`, plus `submit_code`.
- `path` — argument for the discovery tool.

Terminal action (`submit_code`) adds:

- `changes[]` — `{filepath, code, action}`.
- `dependencies[]` — pip / npm packages to install.
- `test_scaffold` — optional fixture / import scaffolding hint for the autotester.

---

## Autotester schemas

### `AutotesterSchema`

`bizniz/autotester/prompts/schema.py`. Used by single-file `process_from_*` modes.

- `test_files[]` — `{filepath, tests}` array.
- `tests` (legacy) — single test code string. The agent handles both shapes.
- `dependencies[]` — pip packages.

### `AutotesterGenerateActionSchema`

`bizniz/autotester/prompts/tool_action_schema.py`. Tool-loop schema with terminal action `submit_tests`.

Same `thinking` / `action` / `path` shell, plus:

- `test_files[]` — `{filepath, tests}`.
- `dependencies[]`.

---

## Debugger schemas

### `AutodebuggerSchema` (QuickDebugger)

`bizniz/agents/debugger/prompts/quick_schema.py`.

| Field | Type | Notes |
|-------|------|-------|
| `diagnosis` | string | Root cause |
| `fix_target` | enum | `code` or `tests` |
| `relevant_files[]` | `[{filename, summary}]` | Files relevant to the diagnosis |
| `suggested_approach` | string | Steps for the repair agent |
| `affected_files[]` | string[] | Files that should be modified |

### `AgenticDebuggerActionSchema`

`bizniz/agents/debugger/prompts/agentic_schema.py`. Tool-loop schema; terminal action `submit_fix`.

| Field | Type | Notes |
|-------|------|-------|
| `thinking` | string | |
| `action` | enum | `view_file` / `list_directory` / `search_files` / `run_command` / `run_tests` / `submit_fix` |
| `path` | string | Multi-purpose argument |
| `fix_target` | enum | `code` / `tests` / `both` |
| `diagnosis` | string | |
| `root_cause_category` | enum | `logic_error` / `interface_mismatch` / `missing_implementation` / `dependency_issue` / `architectural_flaw` / `test_issue` / `import_error` / `other` |
| `fix_plan[]` | string[] | Ordered fix steps |
| `suggested_approach` | string | |
| `missing_packages[]` | string[] | pip packages to install |
| `confidence` | enum | `high` / `medium` / `low` |
| `code_fixes[]` | `[{filepath, new_content}]` | Direct file rewrites |

Every field is required — the LLM emits empty strings / arrays for the fields irrelevant to the current action.

---

## Schema builder

`bizniz/tools/schemas.py:build_tool_action_schema(name, terminal_action, terminal_properties, terminal_required, extra_actions=None)` constructs OpenAI strict-mode tool-loop schemas. It always includes `view_file`, `list_directory`, `search_files`. The terminal action's properties are merged in, all listed in `required`, and `additionalProperties: false`.

This is how the autocoder/autotester/agentic-debugger schemas are kept consistent.
