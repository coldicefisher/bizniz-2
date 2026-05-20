"""WorkspaceContext data types — the snapshot the agent sees."""
from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class DeclaredPackage(BaseModel):
    """One package the project declared as a dependency."""

    name: str = Field(
        ...,
        description=(
            "Distribution name (PyPI/npm). For Python this is what "
            "appears in requirements.txt — e.g., 'pyjwt' not 'jwt'."
        ),
    )
    version: str = Field(
        default="",
        description="Version specifier, e.g., '==2.10.0' or '^3.0.0'.",
    )
    import_name: str = Field(
        default="",
        description=(
            "How the package is imported in code — e.g., 'jwt' for "
            "pyjwt, 'jose' for python-jose, 'cv2' for opencv-python. "
            "Same as ``name`` when distribution and import names "
            "match (most cases)."
        ),
    )
    language: str = Field(
        default="python",
        description="'python' or 'typescript'.",
    )


class WorkspaceContext(BaseModel):
    """Per-issue preventive-context snapshot, built fresh before
    every CoderTesterAgent / debugger call."""

    # File state (live disk, NOT planner-frozen seed).
    target_files_content: Dict[str, str] = Field(
        default_factory=dict,
        description="path → content for the issue's target_files.",
    )
    test_files_content: Dict[str, str] = Field(
        default_factory=dict,
        description="path → content for the issue's test_files.",
    )
    missing_paths: List[str] = Field(
        default_factory=list,
        description=(
            "Paths in the issue's declared file set that don't exist "
            "on disk yet. In edit-mode these need to be created via "
            "new_files; in whole-file mode they're written fresh."
        ),
    )

    # Dependency state.
    declared_python_packages: List[DeclaredPackage] = Field(
        default_factory=list,
        description="Python packages from requirements.txt + pyproject.toml.",
    )
    declared_node_packages: List[DeclaredPackage] = Field(
        default_factory=list,
        description="npm packages from package.json (frontend).",
    )

    # Notes for the prompt renderer.
    workspace_root: str = Field(
        default="",
        description="Absolute path to the workspace root (for log clarity).",
    )

    def all_packages(self) -> List[DeclaredPackage]:
        return list(self.declared_python_packages) + list(
            self.declared_node_packages
        )

    def to_prompt_section(self) -> str:
        """Render as a markdown section to drop into agent prompts."""
        from bizniz.workspace_context.render import render_context_section
        return render_context_section(self)
