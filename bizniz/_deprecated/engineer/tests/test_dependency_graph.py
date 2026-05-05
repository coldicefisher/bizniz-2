import pytest

from bizniz.engineer.dependency_graph import (
    resolve_dependencies,
    sort_into_layers,
    CyclicDependencyError,
)
from bizniz.engineer.types import EngineeringIssue


def _issue(db_id, title, depends_on_titles=None, depends_on_issues=None):
    return EngineeringIssue(
        db_id=db_id,
        title=title,
        description=f"Implement {title}",
        depends_on_titles=depends_on_titles or [],
        depends_on_issues=depends_on_issues or [],
    )


class TestResolveDependencies:
    def test_resolves_titles_to_ids(self):
        issues = [
            _issue(1, "Models"),
            _issue(2, "Services", depends_on_titles=["Models"]),
            _issue(3, "Routes", depends_on_titles=["Models", "Services"]),
        ]
        resolve_dependencies(issues)
        assert issues[0].depends_on_issues == []
        assert issues[1].depends_on_issues == [1]
        assert issues[2].depends_on_issues == [1, 2]

    def test_unknown_title_ignored(self):
        issues = [
            _issue(1, "Models"),
            _issue(2, "Services", depends_on_titles=["NonExistent"]),
        ]
        resolve_dependencies(issues)
        assert issues[1].depends_on_issues == []

    def test_empty_list(self):
        assert resolve_dependencies([]) == []


class TestSortIntoLayers:
    def test_single_issue_one_layer(self):
        issues = [_issue(1, "Models")]
        layers = sort_into_layers(issues)
        assert len(layers) == 1
        assert layers[0].layer_index == 0
        assert len(layers[0].issues) == 1

    def test_independent_issues_one_layer(self):
        issues = [_issue(1, "A"), _issue(2, "B"), _issue(3, "C")]
        layers = sort_into_layers(issues)
        assert len(layers) == 1
        assert len(layers[0].issues) == 3

    def test_chain_produces_sequential_layers(self):
        issues = [
            _issue(1, "A"),
            _issue(2, "B", depends_on_issues=[1]),
            _issue(3, "C", depends_on_issues=[2]),
        ]
        layers = sort_into_layers(issues)
        assert len(layers) == 3
        assert [l.issues[0].title for l in layers] == ["A", "B", "C"]

    def test_diamond_dependency(self):
        # A -> B, A -> C, B -> D, C -> D
        issues = [
            _issue(1, "A"),
            _issue(2, "B", depends_on_issues=[1]),
            _issue(3, "C", depends_on_issues=[1]),
            _issue(4, "D", depends_on_issues=[2, 3]),
        ]
        layers = sort_into_layers(issues)
        assert len(layers) == 3
        # Layer 0: A
        assert [i.title for i in layers[0].issues] == ["A"]
        # Layer 1: B, C (independent of each other)
        assert sorted(i.title for i in layers[1].issues) == ["B", "C"]
        # Layer 2: D
        assert [i.title for i in layers[2].issues] == ["D"]

    def test_cyclic_dependency_raises(self):
        issues = [
            _issue(1, "A", depends_on_issues=[2]),
            _issue(2, "B", depends_on_issues=[1]),
        ]
        with pytest.raises(CyclicDependencyError):
            sort_into_layers(issues)

    def test_empty_list(self):
        assert sort_into_layers([]) == []

    def test_layer_indices_sequential(self):
        issues = [
            _issue(1, "A"),
            _issue(2, "B", depends_on_issues=[1]),
        ]
        layers = sort_into_layers(issues)
        assert [l.layer_index for l in layers] == [0, 1]
