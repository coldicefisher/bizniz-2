# Workspace

`bizniz/workspace/`. The filesystem abstraction every agent uses for reading and writing files.

## Why a workspace abstraction

Agents must not call `open()` on the host filesystem directly. Centralizing file I/O behind a `BaseWorkspace`:

- Lets us swap in `LocalWorkspace`, `TempWorkspace`, or future remote workspaces without touching agents.
- Pairs the workspace with a per-service SQLite database, lazily created at `<workspace_root>/.bizniz/bizniz.db`.
- Keeps the path resolution rules (relative-to-root) in one place.

## Files

| File | Class | Purpose |
|------|-------|---------|
| `base_workspace.py` | `BaseWorkspace` | Abstract base — file/dir operations, git wrappers, lazy DB |
| `local_workspace.py` | `LocalWorkspace` | Persistent workspace at any host path |
| `temp_workspace.py` | `TempWorkspace` | Auto-cleanup `tempfile.mkdtemp`-based workspace |
| `naming.py` | `slugify(name)` | Human-readable name → snake-case slug |
| `workspace_db.py` | `WorkspaceDB` | Per-workspace SQLite database |

## `BaseWorkspace`

| Method | Purpose |
|--------|---------|
| `path(rel)` | Resolve a path relative to workspace root |
| `read_file(path)` / `write_file(path, content)` / `delete_file(path)` | File I/O |
| `make_dir(path)` | Recursive mkdir |
| `list_files()` / `list_relative_files()` | Walk the workspace |
| `init_git()` / `git_add_all()` / `git_commit(msg)` / `git_diff()` | Subprocess git |
| `exists(path)` | Existence check |
| `init_as_package(package_name, description="")` | Create `pyproject.toml`, `<pkg>/__init__.py`, `tests/__init__.py` |
| `create_namespace(namespace_path)` | Create `pkg/sub/__init__.py` chain |
| `tree()` | Returns the relative file list |

Properties:

| Property | Type | Notes |
|----------|------|-------|
| `root` | `Path` | Resolved workspace root |
| `db` | `WorkspaceDB \| WorkspaceScope` | Lazy. If `bizniz_db` is set, returns a `WorkspaceScope`; otherwise creates a per-workspace SQLite file. |

Constructor parameters:

| Param | Notes |
|-------|-------|
| `root` | Path to use; created if missing |
| `bizniz_db` | Optional unified `BiznizDB` — if provided, the `db` property returns a `WorkspaceScope` instead of standalone SQLite |
| `project_id` / `service_name` | Required when `bizniz_db` is set so the scope is unique |

## `LocalWorkspace`

A persistent directory. Adds:

| Method | Purpose |
|--------|---------|
| `LocalWorkspace.from_name(name, parent="~", **kwargs)` | Build from a human name (slugified) |

Constructor enforces:

- If `root` exists, it must be a directory (not a file).
- If `root` does not exist and `create=False`, raises `FileNotFoundError`.

## `TempWorkspace`

Auto-cleanup directory. Used as a context manager:

```python
with TempWorkspace() as ws:
    ws.write_file("foo.py", "print('hi')")
# directory deleted on exit
```

Constructor:

- `prefix="bizniz_"` — temp directory prefix.
- `root=None` — pass an explicit directory to use; in that case `cleanup()` does nothing (the workspace doesn't own it).

## `slugify(name)`

```python
from bizniz.workspace.naming import slugify

slugify("Fraydit Solutions")  # "fraydit_solutions"
slugify("Dog Breeder App")    # "dog_breeder_app"
slugify("My Cool Project!")   # "my_cool_project"
slugify("cafe-systeme")       # "cafe_systeme"
```

Rules: NFKD-normalize → lowercase → spaces/hyphens → underscores → strip non-`[a-z0-9_]` → collapse repeats → trim. Empty inputs return `"workspace"`.

## `WorkspaceDB`

Standalone SQLite-only database lazily created at `<workspace_root>/.bizniz/bizniz.db`. See [modules/db.md](db.md) for the schema. Tables include:

- `problems`, `requirements`, `use_cases`, `issues`
- `architecture_plans`, `architecture_namespaces`, `architecture_domain_models`, `architecture_modules`, `architecture_dependencies`
- `test_results`, `environment_packages`, `environment_config`

It chmods the DB and journal/WAL files to `0o666` (and the `.bizniz` dir to `0o777`) so Docker containers running as different UIDs can still read/write.

When `BiznizDB` is configured (via `BiznizConfig.database_url`), `WorkspaceDB` is bypassed and `WorkspaceScope` is used instead — same API surface, but routed to the unified store.

## Example

```python
from bizniz.workspace.local_workspace import LocalWorkspace

ws = LocalWorkspace.from_name("Pet Groomer")
ws.write_file("calc.py", "def add(a, b): return a + b\n")
ws.init_as_package(package_name="pet_groomer", description="Pet groomer scheduler")
ws.db.save_problem("...")  # creates .bizniz/bizniz.db on first access
```

## Interactions

- **Used by:** every agent. `BaseAIAgent.__init__` requires it.
- **Calls into:** `WorkspaceDB` (or `BiznizDB.for_workspace(...)`), `subprocess` for git commands, `pathlib`.

## Gotchas

- **`db` is lazy AND cached.** First access creates either the SQLite file or the scope. After that, the same instance is returned every time.
- **`init_as_package` is Python-only.** It always writes a `pyproject.toml`. Don't call it for TypeScript services — `Engineer.analyze` already skips it for `language == "typescript"`.
- **`create_namespace` is for sub-namespaces.** The engineer deliberately avoids calling it pre-emptively because it would conflict with single-file modules the coder sometimes generates.
- **Permissions reset matters.** When the runner container creates files inside the workspace as root, `WorkspaceDB._ensure_writable` is the only thing keeping subsequent host-side reads working. If you see `permission denied` on `.bizniz/bizniz.db`, that's why.
- **`TempWorkspace` doesn't take a `bizniz_db` parameter.** It's intentionally a single-purpose scratch area without a unified DB hookup.
- **`from_name` slugifies but doesn't enforce uniqueness.** Two projects named "My App" both resolve to `my_app`.
