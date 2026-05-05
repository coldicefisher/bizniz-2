"""
Topological sorting of engineering issues into dependency layers.

Issues are sorted using Kahn's algorithm into wavefronts:
  Layer 0: issues with no dependencies (foundation)
  Layer 1: issues depending only on layer 0
  Layer N: issues depending only on layers 0..N-1

Issues within the same layer have no inter-dependencies and can be
batched into a single orchestrator call.
"""

from collections import defaultdict, deque
from typing import List, Dict

from bizniz.engineer.types import EngineeringIssue, DependencyLayer


class CyclicDependencyError(Exception):
    """Raised when issues form a dependency cycle."""
    pass


def resolve_dependencies(issues: List[EngineeringIssue]) -> List[EngineeringIssue]:
    """
    Resolve depends_on_titles to depends_on_issues (db_ids).
    Mutates issues in place and returns them.
    """
    title_to_id: Dict[str, int] = {}
    for issue in issues:
        if issue.db_id is not None:
            title_to_id[issue.title] = issue.db_id

    for issue in issues:
        resolved = []
        for title in issue.depends_on_titles:
            if title in title_to_id:
                resolved.append(title_to_id[title])
        issue.depends_on_issues = resolved

    return issues


def sort_into_layers(issues: List[EngineeringIssue]) -> List[DependencyLayer]:
    """
    Topological sort issues into dependency layers using Kahn's algorithm.

    Returns a list of DependencyLayer, each containing issues that can be
    processed together (no inter-dependencies within a layer).

    Raises CyclicDependencyError if a cycle is detected.
    """
    if not issues:
        return []

    # Build adjacency using db_ids
    id_to_issue = {issue.db_id: issue for issue in issues}
    in_degree = {issue.db_id: 0 for issue in issues}
    dependents = defaultdict(list)  # id -> list of ids that depend on it

    for issue in issues:
        for dep_id in issue.depends_on_issues:
            if dep_id in id_to_issue:
                in_degree[issue.db_id] += 1
                dependents[dep_id].append(issue.db_id)

    # Kahn's algorithm, collecting by wavefront (layer)
    layers = []
    queue = deque([iid for iid, deg in in_degree.items() if deg == 0])
    processed = 0

    while queue:
        layer_ids = list(queue)
        queue.clear()
        layer_issues = [id_to_issue[iid] for iid in layer_ids]
        layers.append(DependencyLayer(
            layer_index=len(layers),
            issues=layer_issues,
        ))
        processed += len(layer_ids)

        for iid in layer_ids:
            for dep_id in dependents[iid]:
                in_degree[dep_id] -= 1
                if in_degree[dep_id] == 0:
                    queue.append(dep_id)

    if processed < len(issues):
        raise CyclicDependencyError(
            f"Dependency cycle detected: {len(issues) - processed} issues unreachable"
        )

    return layers
