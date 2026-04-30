# Execution Environments

`bizniz/environment/`. Where generated code is actually run. Every environment implements `BaseExecutionEnvironment` so agents can swap them.

## The contract

```python
class BaseExecutionEnvironment(ABC):
    name: str
    timeout: int

    @abstractmethod
    def execute(self, code: str, call_spec: ExecutionCallSpec) -> ExecutionEnvironmentResult:
        ...

    def describe(self) -> str:
        # Human-readable summary injected into prompts.
```

`ExecutionCallSpec(symbol, args, kwargs)` defines what to invoke; `ExecutionEnvironmentResult(success, result, error, execution_time, stdout, stderr, ...)` is the return.

## Concrete environments

| Class | File | What it runs | Used by |
|-------|------|--------------|---------|
| `PythonSandboxExecutionEnvironment` | `python_environment.py` | Sandboxed `exec` of code with restricted globals/builtins/modules | tiny demos, unit tests of agents themselves |
| `PytestEnvironment` | `pytest_environment.py` | `pytest` against a workspace path on the host | local-only tests where Docker isn't desired |
| `DockerExecutionEnvironment` | `docker_environment.py` | One-shot `docker run` of a script in a `bizniz-python-runner` image | legacy single-shot Python execution |
| `DockerPytestEnvironment` | `docker_pytest_environment.py` | Persistent container, `docker exec pytest` | the orchestrator's primary Python test environment |
| `DockerJestEnvironment` | `docker_jest_environment.py` | Persistent container, `docker exec npx jest` | the orchestrator's TypeScript test environment |

## `PythonSandboxExecutionEnvironment`

Restricted in-process Python eval. Useful for agent-side demos but never used by the production pipeline (no isolation from the host).

| Constructor option | Notes |
|-------------------|-------|
| `exposed_globals` | Names available globally in the eval'd code |
| `exposed_builtins` | Builtins to allow (default: full Python) |
| `allowed_modules` | Mapping from module name → module instance for `import` |
| `timeout` | Wall-clock limit |

`execute(code, call_spec)`:
- Compiles the code, populates the sandbox dict, walks `call_spec.symbol` (e.g. `Calculator().add`), invokes with `args`/`kwargs`, captures stdout/stderr.

## `PytestEnvironment`

Runs pytest on the host as a subprocess. Useful for local debugging without Docker. The orchestrator does not use this in the normal pipeline.

## `DockerExecutionEnvironment`

The original Docker runner. Builds `bizniz-python-runner` from `bizniz/docker/Dockerfile.runner` (Python 3.11 + the curated pip list in `requirements.txt`) lazily on first use, optionally extends it with `additional_packages`. `execute(code, call_spec)`:

1. Writes the code to a tempdir under `.bizniz/exec/`.
2. Runs `docker run --rm -v <tempdir>:/work ... <image> python ...` with the call spec.
3. Captures stdout, stderr, exit code.
4. Tears down the container after every call.

Ideal for one-shots; too slow for the iterative orchestrator loop.

## `DockerPytestEnvironment`

The orchestrator's main Python environment. Key design point: the container is started **lazily on first `execute()`** and reused for every subsequent run via `docker exec`. This eliminates the ~5–10s container startup overhead that otherwise compounds across 20 iterations.

| Constructor param | Notes |
|-------------------|-------|
| `workspace_root` | Bind-mounted at `/workspace` |
| `image` | Service-specific image (e.g. `pet_groomer-backend:dev`) |
| `timeout` | Per-test-run wall clock |
| `extra_pytest_args` | Appended to every pytest invocation |
| `network_enabled` | False disables network (security) |

State it tracks:

- `_container_id`, `_container_name` — set on first start.
- `_installed_packages` — pip packages installed via `install_package(...)`.

Methods of interest:

- `_ensure_container()` — verifies running container or starts a new one named `bizniz-pytest-<uuid>`. Cleans up stale containers from prior crashed runs.
- `_cleanup_stale_containers()` — sweep `bizniz-pytest-*` containers from the docker daemon.
- `install_package(pkg)` — `docker exec ... pip install pkg`. Survives across iterations.
- `stop()` — `docker rm -f` the container. Called by the orchestrator at end of run, or via `__exit__` if used as a context manager.

`execute(code, call_spec)`:
- `code` is intentionally ignored — the test file is already on disk in the workspace.
- `call_spec.symbol` should be `"pytest"`; `call_spec.args` is the list of test files (absolute paths under the bind-mount).
- Runs `docker exec <container> pytest <args>` with `PYTHONPATH=/workspace`.

## `DockerJestEnvironment`

Same persistent-container pattern but for Jest. Workspace bind-mounted at `/workspace`. `execute` runs `npx jest` (or the test path passed in `call_spec.args`).

## `ExecutionCallSpec` and `ExecutionEnvironmentResult`

Defined in `environment/types.py`. Models:

```python
class ExecutionCallSpec(BaseModel):
    symbol: str          # "add" | "Calculator().add" | "pytest"
    args: List[Any] = []
    kwargs: Dict[str, Any] = {}

class ExecutionEnvironmentErrorDetails(BaseModel):
    stage: Optional[str] = None
    type: str
    message: str
    line: Optional[int] = None
    code_line: Optional[str] = None
    traceback: Optional[str] = None

class ExecutionEnvironmentResult(BaseModel):
    success: bool
    result: Optional[Any] = None
    error: Optional[ExecutionEnvironmentErrorDetails] = None
    execution_time: Optional[float] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    traces: Optional[List[ExecutionTrace]] = None
```

## `Dockerfile.runner`

`bizniz/docker/Dockerfile.runner` — Python 3.11 slim, build-essential + libffi/libssl, then `pip install -r requirements.txt`. The requirements list (`bizniz/docker/requirements.txt`) covers the common server stack: FastAPI, uvicorn, pydantic, pytest, redis, requests, lxml, openai, etc. `DockerExecutionEnvironment._ensure_base_image` builds it on first use; subsequent runs reuse the cached image.

For the per-service images (FastAPI service, React service), the architect builds those separately — the runner Dockerfile is only for one-shot host execution.

## Example

```python
from bizniz.environment.docker_pytest_environment import DockerPytestEnvironment
from bizniz.environment.types import ExecutionCallSpec

env = DockerPytestEnvironment(
    workspace_root="/projects/pet_groomer/backend",
    image="pet_groomer-backend:dev",
    timeout=120,
)

result = env.execute(
    code="",  # ignored
    call_spec=ExecutionCallSpec(
        symbol="pytest",
        args=["/workspace/tests/test_appointments.py"],
    ),
)

print(result.success, result.stdout)
env.stop()
```

## Interactions

- **Used by:** the orchestrator (Docker pytest/jest), the autocoder (single-file `generate` only).
- **Calls into:** `subprocess` to drive Docker, the workspace for path resolution.

## Gotchas

- **`code` is ignored in pytest/jest environments.** All test environments expect the file to already be on disk in the workspace; only sandbox/python environments use `code`.
- **The persistent container is per-environment-instance.** If you build two `DockerPytestEnvironment` instances in the same Python process, you get two containers. The orchestrator builds one and reuses it.
- **`PYTHONPATH=/workspace` is set on container start.** That's why bare `import module_name` works as long as `module_name.py` is at the workspace root.
- **`network_enabled=False` blocks pip installs.** If you disable networking on `DockerPytestEnvironment`, then `install_package()` can't reach PyPI. The orchestrator's missing-package detector will retry up to `MAX_PACKAGE_INSTALL_ATTEMPTS = 3` times before giving up.
- **`bizniz-pytest-*` is the cleanup pattern.** The architect's `_cleanup_existing_project` and the env's `_cleanup_stale_containers` both target this name prefix. Don't reuse it for unrelated containers.
- **`stop()` is your responsibility.** The orchestrator calls it; if you use the environment manually, do it yourself or via context manager.
