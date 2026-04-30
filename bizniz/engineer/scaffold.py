"""
Scaffold generator — creates stub files from an ArchitecturePlan.

Deterministic, no AI calls. Runs between analyze() and run_layered() to
ensure every file in the dependency graph exists with valid imports before
the coder/tester touch anything.

The coder then MODIFIES these stubs instead of creating from scratch,
eliminating the entire class of import-chain and missing-file failures.
"""

from pathlib import Path
from typing import List, Dict, Optional, Callable

from bizniz.engineer.types import (
    ArchitecturePlan,
    DomainModelDefinition,
    ModuleDefinition,
    DependencyEdge,
    EngineeringIssue,
)
from bizniz.workspace.base_workspace import BaseWorkspace


def scaffold_from_plan(
    workspace: BaseWorkspace,
    plan: ArchitecturePlan,
    issues: List[EngineeringIssue],
    on_status_message: Optional[Callable[[str], None]] = None,
) -> Dict[str, str]:
    """
    Create stub source files and test files from the architecture plan.

    Returns a dict of filepath -> content for all scaffolded files (the import map).
    Every file is written to the workspace so the coder sees them as existing.
    """

    def log(msg: str):
        if on_status_message:
            on_status_message(msg)

    import_map: Dict[str, str] = {}

    # 1. Create namespace directories with __init__.py
    for ns in plan.namespaces:
        _ensure_package_dirs(workspace, ns.namespace_path)

    # 2. Scaffold domain model files
    for model in plan.domain_models:
        content = _generate_domain_model_stub(model, plan)
        _write_stub(workspace, model.filepath, content, import_map)

    # 3. Scaffold module files
    for module in plan.modules:
        content = _generate_module_stub(module, plan)
        _write_stub(workspace, module.filepath, content, import_map)

    # 4. Scaffold test files from issues
    for issue in issues:
        for test_fp in issue.test_files:
            content = _generate_test_stub(
                test_fp, issue, plan,
            )
            _write_stub(workspace, test_fp, content, import_map)

    # 5. Ensure __init__.py for every directory that has a .py file
    _ensure_all_init_files(workspace, import_map)

    # 6. Flip target_file actions from "create" to "modify" since stubs exist
    for issue in issues:
        for tf in issue.target_files:
            if tf.filepath in import_map and tf.action == "create":
                tf.action = "modify"

    log(
        f"Scaffold: created {len(import_map)} stub file(s) "
        f"({sum(1 for k in import_map if k.startswith('tests/'))} test, "
        f"{sum(1 for k in import_map if not k.startswith('tests/'))} source)"
    )

    return import_map


def _write_stub(
    workspace: BaseWorkspace,
    filepath: str,
    content: str,
    import_map: Dict[str, str],
):
    """Write a stub file to workspace, tracking it in the import map."""
    # Don't overwrite existing files (e.g. from a previous layer)
    full_path = workspace.root / filepath
    if full_path.exists():
        existing = full_path.read_text()
        if existing.strip():
            import_map[filepath] = existing
            return

    workspace.write_file(filepath, content)
    import_map[filepath] = content


def _ensure_package_dirs(workspace: BaseWorkspace, namespace_path: str):
    """Create directory and __init__.py for each level of a namespace path."""
    parts = Path(namespace_path).parts
    for i in range(len(parts)):
        dir_path = workspace.root / Path(*parts[: i + 1])
        dir_path.mkdir(parents=True, exist_ok=True)
        init_path = dir_path / "__init__.py"
        if not init_path.exists():
            init_path.write_text("")


def _ensure_all_init_files(
    workspace: BaseWorkspace,
    import_map: Dict[str, str],
):
    """Ensure __init__.py exists in every directory containing a .py file."""
    dirs_seen = set()
    for filepath in import_map:
        if not filepath.endswith(".py"):
            continue
        parts = Path(filepath).parts
        # Walk up from the file's parent to the root
        for i in range(len(parts) - 1):
            dir_path = Path(*parts[: i + 1])
            if str(dir_path) in dirs_seen:
                continue
            dirs_seen.add(str(dir_path))
            init_path = workspace.root / dir_path / "__init__.py"
            if not init_path.exists():
                init_path.parent.mkdir(parents=True, exist_ok=True)
                init_path.write_text("")


def _generate_domain_model_stub(
    model: DomainModelDefinition,
    plan: ArchitecturePlan,
) -> str:
    """Generate a stub Python file for a domain model class."""
    module_path = _filepath_to_module(model.filepath)
    lines = [
        f'"""{module_path} -- {model.docstring or model.class_name} stub."""',
        "",
    ]

    # Imports from dependency edges
    imports = _collect_imports_for(model.filepath, plan.dependencies)
    if imports:
        lines.extend(imports)
        lines.append("")

    # Check if this looks like a Pydantic model
    is_pydantic = any(
        "BaseModel" in edge.import_symbols
        for edge in plan.dependencies
        if edge.source_filepath == model.filepath
    )
    if is_pydantic:
        lines.append("from pydantic import BaseModel")
        lines.append("")
        lines.append("")
        lines.append(f"class {model.class_name}(BaseModel):")
    else:
        lines.append("")
        lines.append(f"class {model.class_name}:")

    # Docstring
    lines.append(f'    """{model.docstring or model.class_name}."""')

    # Fields
    if model.fields:
        for field in model.fields:
            lines.append(f"    {field.name}: {field.type_hint}")
    elif not model.methods:
        lines.append("    pass")

    # Methods
    if model.methods:
        lines.append("")
        for method in model.methods:
            lines.append(f"    {method.signature}:")
            lines.append(f'        """{method.description}"""')
            lines.append("        raise NotImplementedError")

    lines.append("")
    return "\n".join(lines)


def _generate_module_stub(
    module: ModuleDefinition,
    plan: ArchitecturePlan,
) -> str:
    """Generate a stub Python file for a module (class or functions)."""
    module_path = _filepath_to_module(module.filepath)
    lines = [
        f'"""{module_path} -- {module.docstring or module.class_name or "Module"} stub."""',
        "",
    ]

    # Imports from dependency edges
    imports = _collect_imports_for(module.filepath, plan.dependencies)
    if imports:
        lines.extend(imports)
        lines.append("")

    if module.class_name:
        lines.append("")
        lines.append(f"class {module.class_name}:")
        lines.append(f'    """{module.docstring or module.class_name}."""')

        if module.methods:
            for method in module.methods:
                lines.append("")
                lines.append(f"    {method.signature}:")
                lines.append(f'        """{method.description}"""')
                lines.append("        raise NotImplementedError")
        else:
            lines.append("    pass")
    else:
        # Module-level functions
        if module.methods:
            for method in module.methods:
                lines.append("")
                lines.append(f"{method.signature}:")
                lines.append(f'    """{method.description}"""')
                lines.append("    raise NotImplementedError")
        else:
            lines.append("")
            lines.append("# TODO: implement")

    lines.append("")
    return "\n".join(lines)


def _generate_test_stub(
    test_filepath: str,
    issue: EngineeringIssue,
    plan: ArchitecturePlan,
) -> str:
    """Generate a stub test file with correct imports for the target files."""
    lines = [
        f'"""Tests for: {issue.title}."""',
        "import pytest",
        "",
    ]

    # Import targets from the issue's target files
    for tf in issue.target_files:
        module_path = _filepath_to_module(tf.filepath)
        # Find what class/function this file defines
        class_name = _find_class_for_filepath(tf.filepath, plan)
        if class_name:
            lines.append(f"from {module_path} import {class_name}")
        else:
            lines.append(f"import {module_path}")

    # Add test_setup_hint as a comment if provided
    if issue.test_setup_hint:
        lines.append("")
        for hint_line in issue.test_setup_hint.split("\n"):
            lines.append(f"# {hint_line}")

    lines.append("")
    lines.append("")
    lines.append(f"def test_{_slugify(issue.title)}_placeholder():")
    lines.append(f'    """Placeholder — coder will replace with real tests."""')
    lines.append("    pass")
    lines.append("")

    return "\n".join(lines)


def _collect_imports_for(
    filepath: str,
    dependencies: List[DependencyEdge],
) -> List[str]:
    """Collect import statements for a file based on the dependency graph."""
    imports = []
    for edge in dependencies:
        if edge.source_filepath == filepath:
            target_module = _filepath_to_module(edge.target_filepath)
            if edge.import_symbols:
                symbols = ", ".join(edge.import_symbols)
                imports.append(f"from {target_module} import {symbols}")
            else:
                imports.append(f"import {target_module}")
    return imports


def _find_class_for_filepath(
    filepath: str,
    plan: ArchitecturePlan,
) -> Optional[str]:
    """Find the primary class name defined in a given filepath."""
    for model in plan.domain_models:
        if model.filepath == filepath:
            return model.class_name
    for module in plan.modules:
        if module.filepath == filepath and module.class_name:
            return module.class_name
    return None


def _filepath_to_module(filepath: str) -> str:
    """Convert a file path to a Python module path.

    pet_groomer/models/service.py -> pet_groomer.models.service
    """
    path = filepath
    if path.endswith(".py"):
        path = path[:-3]
    if path.endswith("/__init__"):
        path = path[:-9]
    return path.replace("/", ".")


def _slugify(title: str) -> str:
    """Convert an issue title to a valid Python identifier."""
    import re
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", title.lower()).strip("_")
    return slug[:60]
