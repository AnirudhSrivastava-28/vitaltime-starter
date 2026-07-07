"""Room-to-room ETA estimation for time-feasibility filtering."""

from __future__ import annotations

from collections import deque
from typing import Dict, Tuple

# Demo facility graph: undirected edges with travel time in minutes.
# Rooms are typical nursing-home identifiers; NS = nurse station.
_FACILITY_EDGES: tuple[tuple[str, str, float], ...] = (
    ("NS", "101", 1.0),
    ("NS", "105", 1.2),
    ("NS", "108", 1.5),
    ("101", "102", 0.4),
    ("102", "103", 0.4),
    ("103", "104", 0.4),
    ("104", "105", 0.5),
    ("105", "106", 0.4),
    ("106", "107", 0.4),
    ("107", "108", 0.4),
    ("108", "109", 0.5),
    ("109", "110", 0.4),
    ("102", "106", 0.8),
    ("103", "107", 0.8),
    ("104", "108", 0.9),
)

_graph: Dict[str, list[tuple[str, float]]] | None = None


def _build_graph() -> Dict[str, list[tuple[str, float]]]:
    graph: Dict[str, list[tuple[str, float]]] = {}
    for a, b, weight in _FACILITY_EDGES:
        graph.setdefault(a, []).append((b, weight))
        graph.setdefault(b, []).append((a, weight))
    return graph


def _get_graph() -> Dict[str, list[tuple[str, float]]]:
    global _graph
    if _graph is None:
        _graph = _build_graph()
    return _graph


def eta_minutes(from_room: str, to_room: str) -> float:
    """Shortest-path travel time in minutes between two rooms."""
    if from_room == to_room:
        return 0.0

    graph = _get_graph()
    if from_room not in graph or to_room not in graph:
        # Unknown room — conservative fallback so events are not silently dropped
        return 5.0

    dist: Dict[str, float] = {from_room: 0.0}
    queue: deque[str] = deque([from_room])

    while queue:
        current = queue.popleft()
        current_dist = dist[current]
        if current == to_room:
            return current_dist

        for neighbor, weight in graph[current]:
            new_dist = current_dist + weight
            if neighbor not in dist or new_dist < dist[neighbor]:
                dist[neighbor] = new_dist
                queue.append(neighbor)

    return 999.0  # unreachable
