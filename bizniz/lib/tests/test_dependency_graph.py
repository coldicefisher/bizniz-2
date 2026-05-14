"""Tests for generic dependency_graph.topological_layers."""
from dataclasses import dataclass, field
from typing import List

import pytest

from bizniz.lib.dependency_graph import (
    CyclicDependencyError, topological_layers,
)


@dataclass
class _Item:
    """Generic item with id + depends_on for testing."""
    id: str
    depends_on: List[str] = field(default_factory=list)


class TestTopologicalLayers:
    def test_empty(self):
        assert topological_layers([]) == []

    def test_single(self):
        layers = topological_layers([_Item(id="a")])
        assert layers == [[_Item(id="a")]]

    def test_no_dependencies_all_layer_zero(self):
        items = [_Item(id="a"), _Item(id="b"), _Item(id="c")]
        layers = topological_layers(items)
        assert len(layers) == 1
        assert {i.id for i in layers[0]} == {"a", "b", "c"}

    def test_linear_chain(self):
        items = [
            _Item(id="c", depends_on=["b"]),
            _Item(id="b", depends_on=["a"]),
            _Item(id="a"),
        ]
        layers = topological_layers(items)
        assert [i.id for i in layers[0]] == ["a"]
        assert [i.id for i in layers[1]] == ["b"]
        assert [i.id for i in layers[2]] == ["c"]

    def test_diamond(self):
        # a → {b, c} → d
        items = [
            _Item(id="a"),
            _Item(id="b", depends_on=["a"]),
            _Item(id="c", depends_on=["a"]),
            _Item(id="d", depends_on=["b", "c"]),
        ]
        layers = topological_layers(items)
        assert {i.id for i in layers[0]} == {"a"}
        assert {i.id for i in layers[1]} == {"b", "c"}
        assert {i.id for i in layers[2]} == {"d"}

    def test_external_dep_silently_ignored(self):
        # Dep on an id NOT in the input set — treated as no-op.
        items = [_Item(id="a", depends_on=["external"])]
        layers = topological_layers(items)
        assert layers == [[items[0]]]

    def test_cycle_detected(self):
        items = [
            _Item(id="a", depends_on=["b"]),
            _Item(id="b", depends_on=["a"]),
        ]
        with pytest.raises(CyclicDependencyError, match="cycle"):
            topological_layers(items)

    def test_cycle_self_loop(self):
        items = [_Item(id="a", depends_on=["a"])]
        with pytest.raises(CyclicDependencyError):
            topological_layers(items)

    def test_works_with_name_field_fallback(self):
        # Object exposes 'name' instead of 'id' — should still sort.
        @dataclass
        class _Svc:
            name: str
            depends_on: List[str] = field(default_factory=list)

        items = [
            _Svc(name="frontend", depends_on=["backend"]),
            _Svc(name="backend", depends_on=["db"]),
            _Svc(name="db"),
        ]
        layers = topological_layers(items)
        assert layers[0][0].name == "db"
        assert layers[1][0].name == "backend"
        assert layers[2][0].name == "frontend"

    def test_works_with_depends_on_names_alias(self):
        # Some types use depends_on_names instead of depends_on.
        @dataclass
        class _M:
            id: str
            depends_on_names: List[str] = field(default_factory=list)

        items = [_M(id="b", depends_on_names=["a"]), _M(id="a")]
        layers = topological_layers(items)
        assert layers[0][0].id == "a"
        assert layers[1][0].id == "b"
