"""Tests for the Storybook story discovery layer (Phase 1)."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from bizniz.ux_designer.storybook_discovery import (
    StoryCatalog,
    StoryEntry,
    _kebab,
    discover_stories,
    parse_stories_file,
    storybook_id,
)


# ── _kebab + storybook_id ────────────────────────────────────────


class TestKebab:
    @pytest.mark.parametrize("inp,expected", [
        ("Common/Toast", "common-toast"),
        ("Components/Button", "components-button"),
        ("UI/Layout/Header", "ui-layout-header"),
        ("WithError", "with-error"),
        ("Default", "default"),
        ("HTTPClient", "http-client"),  # consecutive uppercase
        ("APIRoute", "api-route"),
        ("My Component", "my-component"),
        ("foo--bar", "foo-bar"),  # collapse doubles
    ])
    def test_kebab_cases(self, inp, expected):
        assert _kebab(inp) == expected


class TestStorybookId:
    def test_simple(self):
        assert storybook_id("Common/Toast", "Default") == "common-toast--default"

    def test_camel_case_story_name(self):
        assert (
            storybook_id("Components/Button", "WithError")
            == "components-button--with-error"
        )

    def test_deep_title_path(self):
        assert (
            storybook_id("UI/Layout/Header", "Default")
            == "ui-layout-header--default"
        )


# ── parse_stories_file ───────────────────────────────────────────


def _write(tmp_path: Path, name: str, content: str) -> Path:
    """Write a .stories.tsx file under tmp_path and return the path."""
    f = tmp_path / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(textwrap.dedent(content), encoding="utf-8")
    return f


class TestParseStoriesFile:
    def test_single_story(self, tmp_path):
        f = _write(tmp_path, "Button.stories.tsx", """
            import Button from "./Button";

            const meta = {
              title: "Components/Button",
              component: Button,
            };
            export default meta;

            export const Default = { args: { label: "Click" } };
        """)
        # Sibling component file must exist for resolution.
        (tmp_path / "Button.tsx").write_text("export default function Button(){}", "utf-8")

        entries, warnings = parse_stories_file(f)
        assert warnings == []
        assert len(entries) == 1
        e = entries[0]
        assert e.title == "Components/Button"
        assert e.name == "Default"
        assert e.story_id == "components-button--default"
        assert e.component_name == "Button"
        assert e.component_file is not None
        assert e.component_file.name == "Button.tsx"

    def test_multiple_stories_in_one_file(self, tmp_path):
        # Matches the canonical Toast skeleton shape.
        f = _write(tmp_path, "Toast.stories.tsx", """
            import type { Meta, StoryObj } from "@storybook/react";
            import ToastContainer, { showToast } from "./Toast";

            const meta: Meta<typeof ToastContainer> = {
              title: "Common/Toast",
              component: ToastContainer,
            };
            export default meta;
            type Story = StoryObj<typeof ToastContainer>;

            export const Default: Story = { render: () => null };
            export const Stacked: Story = { render: () => null };
        """)
        (tmp_path / "Toast.tsx").write_text("export default function Toast(){}", "utf-8")

        entries, warnings = parse_stories_file(f)
        assert warnings == []
        assert [e.name for e in entries] == ["Default", "Stacked"]
        assert all(e.title == "Common/Toast" for e in entries)
        assert [e.story_id for e in entries] == [
            "common-toast--default", "common-toast--stacked",
        ]
        # Component resolves from ``import ToastContainer ... from "./Toast"``.
        assert entries[0].component_name == "ToastContainer"
        assert entries[0].component_file is not None
        assert entries[0].component_file.name == "Toast.tsx"

    def test_named_import_component_resolution(self, tmp_path):
        f = _write(tmp_path, "Modal.stories.tsx", """
            import { Modal } from "./Modal";

            const meta = {
              title: "UI/Modal",
              component: Modal,
            };
            export default meta;

            export const Default = {};
            export const WithError = {};
        """)
        (tmp_path / "Modal.tsx").write_text("export const Modal = () => null;", "utf-8")

        entries, warnings = parse_stories_file(f)
        assert warnings == []
        assert len(entries) == 2
        assert entries[0].component_name == "Modal"
        # Named import resolved.
        assert entries[0].component_file is not None
        assert entries[0].component_file.name == "Modal.tsx"

    def test_index_resolution(self, tmp_path):
        # Component imported from a directory — resolves to index.tsx.
        f = _write(tmp_path, "components/Card/Card.stories.tsx", """
            import { Card } from "./";
            const meta = { title: "UI/Card", component: Card };
            export default meta;
            export const Default = {};
        """)
        (tmp_path / "components/Card/index.tsx").write_text(
            "export const Card = () => null;", "utf-8",
        )

        entries, warnings = parse_stories_file(f)
        assert warnings == []
        assert entries[0].component_file is not None
        assert entries[0].component_file.name == "index.tsx"

    def test_missing_title_yields_warning(self, tmp_path):
        f = _write(tmp_path, "Bad.stories.tsx", """
            import X from "./X";
            const meta = { component: X };
            export default meta;
            export const Default = {};
        """)
        entries, warnings = parse_stories_file(f)
        assert entries == []
        assert any("missing ``title``" in w for w in warnings)

    def test_no_story_exports_yields_warning(self, tmp_path):
        # title set but no named exports.
        f = _write(tmp_path, "Empty.stories.tsx", """
            const meta = { title: "UI/Empty" };
            export default meta;
        """)
        entries, warnings = parse_stories_file(f)
        assert entries == []
        assert any("no named story exports" in w for w in warnings)

    def test_export_default_not_treated_as_story(self, tmp_path):
        # ``export default meta`` shouldn't show up as a story.
        f = _write(tmp_path, "X.stories.tsx", """
            const meta = { title: "UI/X" };
            export default meta;
            export const Default = {};
        """)
        entries, warnings = parse_stories_file(f)
        assert [e.name for e in entries] == ["Default"]

    def test_lowercase_export_const_skipped(self, tmp_path):
        # Stories must start with an uppercase letter — lowercase
        # exports are usually helpers, not stories.
        f = _write(tmp_path, "X.stories.tsx", """
            const meta = { title: "UI/X" };
            export default meta;
            export const helper = () => 1;
            export const Default = {};
        """)
        entries, _ = parse_stories_file(f)
        assert [e.name for e in entries] == ["Default"]

    def test_missing_component_field_still_emits_stories(self, tmp_path):
        # ``component`` is best-effort; ``title`` alone is enough.
        f = _write(tmp_path, "X.stories.tsx", """
            const meta = { title: "UI/X" };
            export default meta;
            export const Default = {};
        """)
        entries, warnings = parse_stories_file(f)
        assert len(entries) == 1
        assert entries[0].component_name is None
        assert entries[0].component_file is None

    def test_unresolvable_component_import_is_silent(self, tmp_path):
        # ``component: X`` referenced but no matching import — leave
        # component_file=None; don't warn (best-effort).
        f = _write(tmp_path, "X.stories.tsx", """
            const meta = { title: "UI/X", component: NotImported };
            export default meta;
            export const Default = {};
        """)
        entries, warnings = parse_stories_file(f)
        assert len(entries) == 1
        assert entries[0].component_name == "NotImported"
        assert entries[0].component_file is None
        assert warnings == []


# ── discover_stories ─────────────────────────────────────────────


class TestDiscoverStories:
    def test_walks_src_tree(self, tmp_path):
        (tmp_path / "src/components/common").mkdir(parents=True)
        (tmp_path / "src/components/common/Toast.tsx").write_text(
            "export default function T(){};", "utf-8",
        )
        _write(tmp_path, "src/components/common/Toast.stories.tsx", """
            import Toast from "./Toast";
            const meta = { title: "Common/Toast", component: Toast };
            export default meta;
            export const Default = {};
            export const Stacked = {};
        """)
        catalog = discover_stories(tmp_path)
        assert catalog.story_count == 2
        assert catalog.unique_titles == ["Common/Toast"]
        assert catalog.discovery_warnings == []

    def test_multiple_files_alphabetical_order(self, tmp_path):
        # File order matters for cache stability.
        for fname, title in [
            ("Alpha.stories.tsx", "UI/Alpha"),
            ("Beta.stories.tsx", "UI/Beta"),
            ("Gamma.stories.tsx", "UI/Gamma"),
        ]:
            _write(tmp_path, f"src/components/{fname}", f"""
                const meta = {{ title: "{title}" }};
                export default meta;
                export const Default = {{}};
            """)
        catalog = discover_stories(tmp_path)
        assert [s.title for s in catalog.stories] == [
            "UI/Alpha", "UI/Beta", "UI/Gamma",
        ]

    def test_skips_node_modules_dist_build(self, tmp_path):
        # Hostile dirs that shouldn't be walked.
        for hostile in ("node_modules", "dist", "build"):
            _write(tmp_path, f"src/{hostile}/X.stories.tsx", """
                const meta = { title: "Should/NotFind" };
                export default meta;
                export const Default = {};
            """)
        # And one legitimate story.
        _write(tmp_path, "src/components/Good.stories.tsx", """
            const meta = { title: "UI/Good" };
            export default meta;
            export const Default = {};
        """)
        catalog = discover_stories(tmp_path)
        assert [s.title for s in catalog.stories] == ["UI/Good"]

    def test_warnings_for_partial_files_dont_block_others(self, tmp_path):
        # One bad file (missing title) + one good file → catalog has
        # the good file's stories AND a warning for the bad file.
        _write(tmp_path, "src/Bad.stories.tsx", """
            const meta = { component: X };
            export default meta;
            export const Default = {};
        """)
        _write(tmp_path, "src/Good.stories.tsx", """
            const meta = { title: "UI/Good" };
            export default meta;
            export const Default = {};
        """)
        catalog = discover_stories(tmp_path)
        assert [s.title for s in catalog.stories] == ["UI/Good"]
        assert len(catalog.discovery_warnings) == 1
        assert "Bad.stories.tsx" in catalog.discovery_warnings[0]

    def test_non_directory_root_returns_empty_catalog_with_warning(self, tmp_path):
        nope = tmp_path / "does-not-exist"
        catalog = discover_stories(nope)
        assert catalog.story_count == 0
        assert any("not a directory" in w for w in catalog.discovery_warnings)

    def test_empty_src_dir_returns_empty_catalog(self, tmp_path):
        (tmp_path / "src").mkdir()
        catalog = discover_stories(tmp_path)
        assert catalog.story_count == 0
        assert catalog.discovery_warnings == []

    def test_no_src_dir_returns_empty_catalog(self, tmp_path):
        catalog = discover_stories(tmp_path)
        assert catalog.story_count == 0
        # Not warnable — frontend without src/ is legal.
        assert catalog.discovery_warnings == []

    def test_custom_search_roots(self, tmp_path):
        (tmp_path / "app").mkdir()
        _write(tmp_path, "app/X.stories.tsx", """
            const meta = { title: "App/X" };
            export default meta;
            export const Default = {};
        """)
        catalog = discover_stories(tmp_path, search_roots=("app",))
        assert catalog.story_count == 1
        assert catalog.stories[0].title == "App/X"
