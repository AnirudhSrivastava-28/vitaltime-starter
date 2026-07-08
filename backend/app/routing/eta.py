"""Room-to-room ETA estimation and path reconstruction for time-feasibility
filtering and staff transit simulation.

Uses a proper Dijkstra (min-heap, return-on-first-pop) rather than a FIFO
queue relaxation — a FIFO queue can dequeue a node before its true shortest
distance has been finalized on a weighted graph, silently returning a
larger-than-actual ETA. Dijkstra with a min-heap guarantees the first pop
of any node is its final shortest distance (non-negative weights, which
all facility edges are).
"""

from __future__ import annotations

import heapq
from typing import Dict, List, Tuple

from app.routing.models import Staff

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


def shortest_path(from_room: str, to_room: str) -> Tuple[List[str], List[float]]:
    """Dijkstra's algorithm. Returns (ordered room path incl. both endpoints,
    cumulative minutes-from-origin at each node in that path).

    hop_times[0] is always 0.0 (time at origin). hop_times[-1] is the total
    ETA, equivalent to what eta_minutes() used to return alone.
    """
    if from_room == to_room:
        return [from_room], [0.0]

    graph = _get_graph()
    if from_room not in graph or to_room not in graph:
        # Unknown room — conservative fallback so events are not silently
        # dropped. Matches the old function's fallback ETA.
        return [from_room, to_room], [0.0, 5.0]

    dist: Dict[str, float] = {from_room: 0.0}
    prev: Dict[str, str] = {}
    visited: set[str] = set()
    heap: list[tuple[float, str]] = [(0.0, from_room)]

    while heap:
        d, node = heapq.heappop(heap)
        if node in visited:
            continue
        visited.add(node)
        if node == to_room:
            break
        for neighbor, weight in graph[node]:
            nd = d + weight
            if neighbor not in dist or nd < dist[neighbor]:
                dist[neighbor] = nd
                prev[neighbor] = node
                heapq.heappush(heap, (nd, neighbor))

    if to_room not in dist:
        return [from_room, to_room], [0.0, 999.0]  # unreachable

    path = [to_room]
    while path[-1] != from_room:
        path.append(prev[path[-1]])
    path.reverse()

    cumulative = [0.0]
    for a, b in zip(path, path[1:]):
        edge_weight = next(w for n, w in graph[a] if n == b)
        cumulative.append(cumulative[-1] + edge_weight)

    return path, cumulative


def eta_minutes(from_room: str, to_room: str) -> float:
    """Shortest total travel time in minutes. Kept for callers that only
    need the scalar; internally just the last cumulative hop time."""
    _, cumulative = shortest_path(from_room, to_room)
    return cumulative[-1]


def resolve_room(staff: Staff, now) -> str:
    """The last room this staff member has actually reached, even if a
    transit toward a new destination is currently in progress.

    Deliberately conservative: if they're partway down a corridor segment,
    this returns the room *behind* them, not credit for partial progress
    into the next hop. Any new ETA computed from this point is therefore
    never an underestimate of real remaining distance.
    """
    transit = staff.transit
    if transit is None:
        return staff.current_position.room

    departure = transit.departure_time
    elapsed_minutes = (now - departure).total_seconds() / 60.0
    hop_times = transit.hop_times

    if elapsed_minutes >= hop_times[-1]:
        return staff.current_position.room  # already arrived

    last_idx = 0
    for i, t in enumerate(hop_times):
        if elapsed_minutes >= t:
            last_idx = i
        else:
            break
    return transit.path[last_idx]
