"""Room-to-room ETA estimation and path reconstruction for time-feasibility
filtering and staff transit simulation.

Uses a proper Dijkstra (min-heap, return-on-first-pop) rather than a FIFO
queue relaxation — a FIFO queue can dequeue a node before its true shortest
distance has been finalized on a weighted graph, silently returning a
larger-than-actual ETA. Dijkstra with a min-heap guarantees the first pop
of any node is its final shortest distance (all edge weights are non-negative).
"""

from __future__ import annotations

import heapq
import datetime as _dt
from datetime import datetime
from typing import Dict, List, Tuple

from app.routing.models import Staff

# Demo facility graph: undirected edges with travel time in minutes.
# Rooms are typical nursing-home identifiers; NS = nurse station.
#
# IMPORTANT: this range must stay in sync with every room picker that can
# submit an event (currently the iOS EventFormView dropdown, 101-112).
# A room that a client can submit but that isn't a node here degrades
# gracefully *server-side* (shortest_path returns a conservative fallback
# below) but previously broke the dashboard's client-side animation, since
# the client has no equivalent fallback for a room missing from its own
# coordinate map — see dashboard.html's ROOM_COORDS/getRoomCoords. Rooms
# 111-112 were added here (and mirrored in dashboard.html) specifically to
# close that gap; if the dropdown range ever changes again, update both.
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
    ("110", "111", 0.4),
    ("111", "112", 0.4),
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
    """Dijkstra. Returns (ordered rooms including both endpoints,
    cumulative minutes-from-origin at each node).

    hop_times[0] is always 0.0. hop_times[-1] is the total ETA in minutes.
    """
    if from_room == to_room:
        return [from_room], [0.0]

    graph = _get_graph()
    if from_room not in graph or to_room not in graph:
        return [from_room, to_room], [0.0, 5.0]  # conservative fallback

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
        return [from_room, to_room], [0.0, 999.0]

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
    """Shortest total travel time in minutes."""
    _, cumulative = shortest_path(from_room, to_room)
    return cumulative[-1]


def resolve_room(staff: Staff, now: datetime, time_scale: float = 1.0) -> str:
    """Last room this staff has actually reached, accounting for in-progress
    transit. Conservatively returns the room *behind* the staff if they're
    partway down a corridor — any new ETA computed from here is never an
    underestimate of real remaining distance.

    `time_scale` translates real elapsed seconds into sim-minutes at the
    demo's accelerated pace. Passed in by the caller (routing decisions use
    scaled time so the sim's internal clock is consistent).
    """
    transit = staff.transit
    if transit is None:
        return staff.current_position.room

    # Defense-in-depth: normalize here too, independent of whatever the
    # caller already did. now/departure passing an aware datetime raises
    # TypeError on subtraction otherwise.
    if now.tzinfo is not None:
        now = now.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    departure = transit.departure_time
    if departure.tzinfo is not None:
        departure = departure.astimezone(_dt.timezone.utc).replace(tzinfo=None)

    elapsed_real_seconds = (now - departure).total_seconds()
    elapsed_sim_minutes = (elapsed_real_seconds / 60.0) * time_scale
    hop_times = transit.hop_times

    if elapsed_sim_minutes >= hop_times[-1]:
        # Already arrived — for outbound this is the event room, for return
        # it's the home room (both already stored as current_position).
        return staff.current_position.room

    last_idx = 0
    for i, t in enumerate(hop_times):
        if elapsed_sim_minutes >= t:
            last_idx = i
        else:
            break
    return transit.path[last_idx]
