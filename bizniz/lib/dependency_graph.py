"""Generic topological-sort into dependency layers (Kahn's algorithm).

Type-agnostic: works on any iterable of items where each item exposes
an ``id`` (str/int) and a ``depends_on`` (List of ids referencing other
items in the same set).

Used by v2.5 at three levels:
  - Services: ``service.depends_on`` → which services build first
  - Issues per service: ``issue.depends_on`` → which issues code first
  - Tests vs code: enforced separately by the symbol_validator step
    inside Coder (cannot write tests until imports resolve)

Returns wavefronts: items in the same layer have no inter-dependencies
and can be processed in parallel (or any order). Items in layer N
strictly depend only on layers 0..N-1.
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Iterable, List, Optional, Protocol, TypeVar


class _HasIdAndDeps(Protocol):
    """Anything with an ``id`` (or ``name``/``db_id`` fallback) and a
    ``depends_on`` list."""
    id: Any
    depends_on: List[Any]


T = TypeVar("T")


class CyclicDependencyError(Exception):
    """Items form a dependency cycle — no valid topo order exists.

    ``cyclic_ids`` carries the ids of all items participating in the
    cycle (or merely blocked behind it) so callers that want to repair
    rather than halt can drop edges among those members.
    """

    def __init__(self, message: str, cyclic_ids: Optional[List[Any]] = None):
        super().__init__(message)
        self.cyclic_ids: List[Any] = list(cyclic_ids or [])


def _item_id(item: Any) -> Any:
    """Pick the most meaningful identity field. Tries id, name, db_id."""
    for attr in ("id", "name", "db_id"):
        v = getattr(item, attr, None)
        if v is not None:
            return v
    raise ValueError(
        f"item {item!r} has no id/name/db_id — can't topo-sort it"
    )


def _item_deps(item: Any) -> List[Any]:
    """Pick the depends_on list. Tries depends_on, depends_on_names,
    depends_on_titles, depends_on_issues."""
    for attr in ("depends_on", "depends_on_names", "depends_on_titles",
                 "depends_on_issues"):
        v = getattr(item, attr, None)
        if v is not None:
            return list(v)
    return []


def topological_layers(items: Iterable[T]) -> List[List[T]]:
    """Return a list of layers (each a list of items). Items within a
    layer have no inter-dependencies; items in layer N depend only on
    layers 0..N-1.

    Raises ``CyclicDependencyError`` if the input has a dependency
    cycle.
    """
    items_list = list(items)
    if not items_list:
        return []

    by_id = {_item_id(it): it for it in items_list}
    in_degree = {_item_id(it): 0 for it in items_list}
    dependents: dict = defaultdict(list)

    for it in items_list:
        my_id = _item_id(it)
        for dep in _item_deps(it):
            # Only count deps that are within this set; external/unknown
            # deps are silently ignored (caller can pre-filter if needed).
            if dep in by_id:
                in_degree[my_id] += 1
                dependents[dep].append(my_id)

    layers: List[List[T]] = []
    queue = deque([iid for iid, deg in in_degree.items() if deg == 0])
    processed = 0

    while queue:
        layer_ids = list(queue)
        queue.clear()
        layers.append([by_id[iid] for iid in layer_ids])
        processed += len(layer_ids)
        for iid in layer_ids:
            for dep_id in dependents[iid]:
                in_degree[dep_id] -= 1
                if in_degree[dep_id] == 0:
                    queue.append(dep_id)

    if processed < len(items_list):
        cyclic = [
            _item_id(it) for it in items_list
            if in_degree[_item_id(it)] > 0
        ]
        raise CyclicDependencyError(
            f"dependency cycle involving: {cyclic}",
            cyclic_ids=cyclic,
        )
    return layers
