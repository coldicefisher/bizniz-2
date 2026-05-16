"""Storybook story discovery for ProUXDesigner — Phase 1.

Walks a frontend workspace for ``*.stories.tsx`` (also ``.ts``,
``.jsx``, ``.js``, ``.mdx`` per the skeleton's `.storybook/main.ts`
glob) and extracts the metadata needed to drive a per-story UX
loop in later phases:

  - ``title`` from the file's exported ``meta``
  - Each named story export (``export const Default: Story = ...``)
  - The Storybook URL story-id (``kebab(title) + "--" + kebab(name)``)
  - Best-effort component file path (from the meta's component
    import) so the fix dispatcher can target the right file

Phase 1 is the foundation: clean, testable, doesn't disrupt the
existing per-route loop. Phases 2-7 build capture, evaluation,
fix dispatch, score aggregation, and ProUXDesigner integration
on top of this catalog. Tracked in roadmap item 2.

This module is regex-based, not AST. ``*.stories.tsx`` is a tightly-
constrained format by convention (the skeleton ships one canonical
shape, the Engineer prompt requires it). When the regex misses, we
record a discovery warning and skip the file rather than crash —
Phase 1's job is to surface what's there, not to be a TypeScript
parser.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field


# Storybook's main.ts glob (per the React skeleton). Order matters:
# more specific extensions first so .tsx wins over .ts for ambiguous
# files.
_STORY_FILE_GLOBS: tuple = (
    "**/*.stories.tsx",
    "**/*.stories.ts",
    "**/*.stories.jsx",
    "**/*.stories.js",
    "**/*.stories.mjs",
)

# Default search roots inside a frontend workspace. We look under
# ``src/`` because that's where the skeleton's main.ts globs from.
_DEFAULT_SEARCH_ROOTS: tuple = ("src",)


# ── Public types ─────────────────────────────────────────────────


class StoryEntry(BaseModel):
    """One named story export from a ``*.stories.tsx`` file.

    Each story is a distinct screenshot target with a stable URL
    id. The Storybook dev server serves stories at
    ``http://<host>:<port>/iframe.html?id=<story_id>&viewMode=story``.
    """
    story_id: str = Field(
        description=(
            "Storybook URL id — ``kebab(title) + '--' + kebab(name)``. "
            "Stable across runs (key for cache invalidation)."
        ),
    )
    name: str = Field(
        description="The named export from the stories file (``Default``, ``Stacked``, ``WithError``).",
    )
    title: str = Field(
        description=(
            "The ``meta.title`` from the stories file "
            "(``\"Common/Toast\"``, ``\"Components/Button\"``)."
        ),
    )
    component_name: Optional[str] = Field(
        default=None,
        description=(
            "The ``meta.component`` identifier (``ToastContainer``, "
            "``Button``). Best-effort; ``None`` if we can't extract it."
        ),
    )
    component_file: Optional[Path] = Field(
        default=None,
        description=(
            "Path to the file the component is imported from, "
            "resolved relative to the stories file. Best-effort — the "
            "fix dispatcher uses this to know which file to edit."
        ),
    )
    stories_file: Path = Field(
        description="Path to the ``.stories.tsx`` file itself.",
    )


class StoryCatalog(BaseModel):
    """All stories discovered in a frontend workspace."""
    frontend_root: Path
    stories: List[StoryEntry] = Field(default_factory=list)
    discovery_warnings: List[str] = Field(
        default_factory=list,
        description=(
            "Stories files we found but couldn't parse fully. One "
            "line per warning; doesn't fail the walk."
        ),
    )

    @property
    def story_count(self) -> int:
        return len(self.stories)

    @property
    def unique_titles(self) -> List[str]:
        seen: set = set()
        out: List[str] = []
        for s in self.stories:
            if s.title not in seen:
                seen.add(s.title)
                out.append(s.title)
        return out


# ── Storybook URL id generation ──────────────────────────────────


def _kebab(value: str) -> str:
    """Approximate Storybook's ``paramCase`` slug logic:

    - Replace ``/`` with ``-`` (title path becomes flat)
    - Insert ``-`` before each uppercase letter (camelCase split)
    - Replace any non-alphanumeric run with ``-``
    - Lowercase
    - Collapse repeated ``-`` and trim edges
    """
    s = value.replace("/", "-")
    # Insert dash before each interior uppercase letter (camelCase split).
    s = re.sub(r"(?<=[a-z0-9])([A-Z])", r"-\1", s)
    s = re.sub(r"([A-Z])(?=[A-Z][a-z])", r"\1-", s)
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    s = re.sub(r"-{2,}", "-", s)
    return s


def storybook_id(title: str, story_name: str) -> str:
    """Build the Storybook URL id from a meta.title + story name.

    Examples:
      >>> storybook_id("Common/Toast", "Default")
      'common-toast--default'
      >>> storybook_id("Components/Button", "WithError")
      'components-button--with-error'
      >>> storybook_id("UI/Layout/Header", "Default")
      'ui-layout-header--default'
    """
    return f"{_kebab(title)}--{_kebab(story_name)}"


# ── Parser ───────────────────────────────────────────────────────


# Pull out the ``title`` value from any object literal. We accept
# single- and double-quoted strings, optional whitespace, optional
# trailing comma.
_TITLE_RE = re.compile(
    r"""\btitle\s*:\s*["'](?P<title>[^"']+)["']""",
    re.MULTILINE,
)

# ``component: SomeIdent,`` inside the meta object. Identifier only —
# no member expressions (``foo.Bar``) — Storybook discourages those.
_COMPONENT_RE = re.compile(
    r"""\bcomponent\s*:\s*(?P<comp>[A-Za-z_][A-Za-z0-9_]*)\s*[,}\n]""",
    re.MULTILINE,
)

# Named story exports. Examples that should match:
#   export const Default: Story = { ... }
#   export const WithError: StoryObj<typeof X> = { ... }
#   export const Primary = { args: { ... } }
# Excludes ``export default ...``.
_STORY_EXPORT_RE = re.compile(
    r"""^\s*export\s+const\s+(?P<name>[A-Z][A-Za-z0-9_]*)\s*"""
    r"""(?::\s*Story\w*(?:<[^>]*>)?)?\s*=\s*[\{(]""",
    re.MULTILINE,
)

# Import of the component. Captures the local binding name (default
# import or named) and the source path.
#   import ToastContainer, { showToast } from "./Toast";
#   import { Button } from "./Button";
#   import Button from "./Button";
_IMPORT_RE = re.compile(
    r"""^\s*import\s+"""
    r"""(?:"""
    r"""(?P<default>[A-Za-z_][A-Za-z0-9_]*)\s*(?:,\s*\{[^}]*\})?"""
    r"""|\{\s*(?P<named>[^}]+)\}"""
    r""")"""
    r"""\s+from\s+["'](?P<src>[^"']+)["']""",
    re.MULTILINE,
)


def _resolve_component_file(
    stories_file: Path,
    relative_src: str,
) -> Optional[Path]:
    """Resolve a stories-file's component import to an absolute path.

    Tries: ``<src>``, ``<src>.tsx``, ``<src>.ts``, ``<src>.jsx``,
    ``<src>.js``, ``<src>/index.tsx``, ``<src>/index.ts``,
    ``<src>/index.jsx``, ``<src>/index.js``. Returns the first
    existing match or ``None``.
    """
    if not relative_src.startswith("."):
        # Bare specifier (node_modules import) — no local file.
        return None
    base = (stories_file.parent / relative_src).resolve()
    candidates = [
        base,
        base.with_suffix(".tsx"),
        base.with_suffix(".ts"),
        base.with_suffix(".jsx"),
        base.with_suffix(".js"),
        base / "index.tsx",
        base / "index.ts",
        base / "index.jsx",
        base / "index.js",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def parse_stories_file(stories_file: Path) -> "tuple[List[StoryEntry], List[str]]":
    """Parse one ``*.stories.tsx`` file. Returns ``(entries, warnings)``.

    Empty entries + a warning string if the file is missing a
    ``title`` or has no named story exports — caller decides what
    to do with the warning.
    """
    warnings: List[str] = []
    try:
        text = stories_file.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return [], [f"{stories_file}: read failed ({e})"]

    title_match = _TITLE_RE.search(text)
    if title_match is None:
        return [], [f"{stories_file}: missing ``title`` in meta — skipping"]
    title = title_match.group("title").strip()

    story_names = [m.group("name") for m in _STORY_EXPORT_RE.finditer(text)]
    if not story_names:
        return [], [
            f"{stories_file}: title={title!r} found but no named story "
            f"exports — skipping"
        ]

    component_name: Optional[str] = None
    comp_match = _COMPONENT_RE.search(text)
    if comp_match is not None:
        component_name = comp_match.group("comp")

    component_file: Optional[Path] = None
    if component_name is not None:
        for m in _IMPORT_RE.finditer(text):
            default = m.group("default")
            named = m.group("named") or ""
            named_idents = [
                p.strip().split(" as ")[0].strip()
                for p in named.split(",") if p.strip()
            ]
            if default == component_name or component_name in named_idents:
                component_file = _resolve_component_file(
                    stories_file, m.group("src"),
                )
                break

    entries: List[StoryEntry] = []
    for name in story_names:
        entries.append(StoryEntry(
            story_id=storybook_id(title, name),
            name=name,
            title=title,
            component_name=component_name,
            component_file=component_file,
            stories_file=stories_file,
        ))
    return entries, warnings


# ── Directory walker ─────────────────────────────────────────────


def discover_stories(
    frontend_root: Path,
    search_roots: "Optional[tuple]" = None,
) -> StoryCatalog:
    """Walk ``frontend_root`` for stories files and return a catalog.

    ``search_roots`` is an iterable of subdirectory names (default
    ``("src",)``) to constrain the walk. Glob patterns are applied
    relative to each search root.

    Files are scanned in deterministic alphabetical order so the
    catalog is stable across runs (key for the view cache).
    """
    frontend_root = Path(frontend_root).resolve()
    if not frontend_root.is_dir():
        return StoryCatalog(
            frontend_root=frontend_root,
            discovery_warnings=[f"{frontend_root}: not a directory"],
        )

    search_roots = search_roots or _DEFAULT_SEARCH_ROOTS
    seen_files: set = set()
    candidate_files: List[Path] = []
    for sub in search_roots:
        sub_path = frontend_root / sub
        if not sub_path.is_dir():
            continue
        for pattern in _STORY_FILE_GLOBS:
            for path in sorted(sub_path.glob(pattern)):
                if path in seen_files:
                    continue
                # Skip node_modules + dist + build artifacts.
                if any(
                    seg in {"node_modules", "dist", "build", ".storybook"}
                    for seg in path.parts
                ):
                    continue
                seen_files.add(path)
                candidate_files.append(path)

    all_entries: List[StoryEntry] = []
    all_warnings: List[str] = []
    for f in candidate_files:
        entries, warnings = parse_stories_file(f)
        all_entries.extend(entries)
        all_warnings.extend(warnings)

    return StoryCatalog(
        frontend_root=frontend_root,
        stories=all_entries,
        discovery_warnings=all_warnings,
    )
