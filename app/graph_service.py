"""Dependency-graph validation, replacement and fault classification.

Classification and path generation are fully **iterative** (Kahn topological
order + combinatorial unranking), so thousand-service dependency chains no
longer blow the Python recursion limit and highly branched graphs no longer
require enumerating every path up front.  When the full result does not fit in
one page, paths are served through opaque, state-pinned cursors instead.
"""
from __future__ import annotations

import sqlite3
from collections import deque
from typing import Optional

from .cursors import decode_cursor, encode_cursor
from .database import get_state_version, read_tx, write_tx
from .errors import GraphError, InvalidCursor, NoGraph, StaleCursor
from .schemas import GraphPayload, GraphResponse, ServiceIn

# A page without explicit ``limit`` holds at most this many paths.  Legacy
# (parameter-less) responses stay unpaginated exactly while the total number
# of all-down paths is at most this value, so small graphs keep their original
# response shape.
DEFAULT_PAGE_LIMIT = 100
MAX_PAGE_LIMIT = 1000

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_graph(payload: GraphPayload) -> tuple[list[str], dict[str, list[str]]]:
    """Validate a full-graph payload.

    Returns ``(nodes, upstream)`` where ``upstream[s]`` is the sorted list of
    services ``s`` depends on.  Raises :class:`GraphError` on any structural
    problem (duplicate names, unknown references or cycles); the caller keeps
    the previously committed graph untouched in that case.
    """
    nodes: list[str] = []
    seen: set[str] = set()
    duplicates: list[str] = []
    for svc in payload.services:
        if svc.name in seen:
            duplicates.append(svc.name)
        seen.add(svc.name)
        nodes.append(svc.name)
    if duplicates:
        raise GraphError(f"duplicate service names: {sorted(set(duplicates))}")

    node_set = set(nodes)
    upstream: dict[str, list[str]] = {}
    unknown: set[tuple[str, str]] = set()
    dup_edges: set[tuple[str, str]] = set()

    for svc in payload.services:
        deps: list[str] = []
        edge_seen: set[str] = set()
        for dep in svc.dependencies:
            if dep == svc.name:
                raise GraphError(f"service {svc.name!r} cannot depend on itself")
            if dep not in node_set:
                unknown.add((svc.name, dep))
            if dep in edge_seen:
                dup_edges.add((svc.name, dep))
            edge_seen.add(dep)
            deps.append(dep)
        upstream[svc.name] = deps

    if unknown:
        detail = ", ".join(f"{a}->{b}" for a, b in sorted(unknown))
        raise GraphError(f"unknown service references: {detail}")
    if dup_edges:
        detail = ", ".join(f"{a}->{b}" for a, b in sorted(dup_edges))
        raise GraphError(f"duplicate dependency edges: {detail}")

    _ensure_acyclic(nodes, upstream)
    return nodes, upstream


def _ensure_acyclic(nodes: list[str], upstream: dict[str, list[str]]) -> None:
    """Kahn's algorithm; anything left over when the queue drains is on a cycle."""
    indegree = {n: 0 for n in nodes}
    dependents: dict[str, list[str]] = {n: [] for n in nodes}
    for downstream, deps in upstream.items():
        for up in deps:
            indegree[downstream] += 1
            dependents[up].append(downstream)

    queue = deque(sorted(n for n, d in indegree.items() if d == 0))
    visited = 0
    while queue:
        node = queue.popleft()
        visited += 1
        for child in sorted(dependents[node]):
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)

    if visited != len(nodes):
        cyclic = sorted(n for n, d in indegree.items() if d > 0)
        raise GraphError(f"dependency cycle detected involving: {cyclic}")


# ---------------------------------------------------------------------------
# Replacement
# ---------------------------------------------------------------------------


def replace_graph(payload: GraphPayload) -> dict:
    """Atomically replace the whole graph.

    Service-state rows (up/down + last seq) are keyed by service name and live
    outside the graph tables, so states of same-named services survive a
    replacement.  The swap of nodes/edges/meta happens in one write
    transaction and is therefore all-or-nothing.
    """
    nodes, upstream = validate_graph(payload)

    with write_tx() as conn:
        row = conn.execute("SELECT revision FROM graph_meta WHERE id = 1").fetchone()
        new_revision = (row["revision"] + 1) if row else 1

        conn.execute("DELETE FROM edges")
        conn.execute("DELETE FROM nodes")
        conn.executemany(
            "INSERT INTO nodes (service, revision) VALUES (?, ?)",
            [(n, new_revision) for n in nodes],
        )
        edge_rows = [
            (new_revision, up, downstream)
            for downstream, deps in upstream.items()
            for up in deps
        ]
        conn.executemany(
            "INSERT INTO edges (revision, upstream, downstream) VALUES (?, ?, ?)",
            edge_rows,
        )
        conn.execute(
            "INSERT INTO graph_meta (id, revision, version) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET revision = excluded.revision, "
            "version = excluded.version, "
            "committed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
            (new_revision, payload.version),
        )
        preserved = [
            r["service"]
            for r in conn.execute(
                "SELECT service FROM service_state WHERE service IN (%s) ORDER BY service"
                % ",".join("?" * len(nodes)),
                nodes,
            )
        ]
    return {
        "accepted": True,
        "revision": new_revision,
        "version": payload.version,
        "service_count": len(nodes),
        "edge_count": len(edge_rows),
        "preserved_state": preserved,
    }


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _load_graph(conn: sqlite3.Connection) -> tuple[int, str | None, dict[str, list[str]]]:
    meta = conn.execute("SELECT revision, version FROM graph_meta WHERE id = 1").fetchone()
    if meta is None:
        raise NoGraph("no dependency graph has been committed yet")
    upstream: dict[str, list[str]] = {
        r["service"]: [] for r in conn.execute("SELECT service FROM nodes")
    }
    for r in conn.execute(
        "SELECT edges.upstream AS upstream, edges.downstream AS downstream "
        "FROM edges ORDER BY edges.upstream, edges.downstream"
    ):
        upstream[r["downstream"]].append(r["upstream"])
    return meta["revision"], meta["version"], upstream


def get_graph() -> GraphResponse:
    with read_tx() as conn:
        revision, version, upstream = _load_graph(conn)
        meta = conn.execute(
            "SELECT committed_at FROM graph_meta WHERE id = 1"
        ).fetchone()
        services = [
            ServiceIn(name=name, dependencies=sorted(deps))
            for name, deps in sorted(upstream.items())
        ]
    return GraphResponse(
        revision=revision,
        version=version,
        committed_at=meta["committed_at"],
        services=services,
    )


# ---------------------------------------------------------------------------
# Iterative fault classification
# ---------------------------------------------------------------------------


def _topological_order(
    nodes: list[str], upstream: dict[str, list[str]]
) -> list[str]:
    """Kahn order over the whole DAG (upstreams before dependents)."""
    indegree = {n: len(upstream.get(n, ())) for n in nodes}
    dependents: dict[str, list[str]] = {n: [] for n in nodes}
    for downstream, deps in upstream.items():
        for up in deps:
            dependents[up].append(downstream)
    queue = deque(sorted(n for n, d in indegree.items() if d == 0))
    order: list[str] = []
    while queue:
        node = queue.popleft()
        order.append(node)
        for child in sorted(dependents[node]):
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    return order


def _classify(
    upstream: dict[str, list[str]],
    down_here: set[str],
    topo: list[str],
) -> tuple[set[str], dict[str, set[str]], dict[str, int]]:
    """Iterative single-pass classification on one committed snapshot.

    Returns ``(roots, reachable_roots, path_count)`` where

    * ``roots`` are faulty nodes without any faulty upstream;
    * ``reachable_roots[v]`` is the set of roots reachable from ``v`` through
      faulty nodes only (empty when every path is cut by a healthy/unreported
      node);
    * ``path_count[v]`` is the number of all-faulty paths from ``v`` to any
      root (``1`` for roots themselves, ``0`` exactly when no root is
      reachable).  Counts may be huge on branched graphs; they stay Python
      integers and are never expanded into actual path lists here.
    """
    roots: set[str] = set()
    reachable: dict[str, set[str]] = {}
    path_count: dict[str, int] = {}

    for node in topo:
        if node not in down_here:
            continue
        down_upstreams = [u for u in upstream.get(node, ()) if u in down_here]
        if not down_upstreams:
            # Faulty node with no faulty upstream: a root cause.
            roots.add(node)
            reachable[node] = {node}
            path_count[node] = 1
            continue
        merged: set[str] = set()
        total = 0
        for up in down_upstreams:
            merged |= reachable[up]
            total += path_count[up]
        reachable[node] = merged
        path_count[node] = total
    return roots, reachable, path_count


def _unrank_path(
    start: str,
    rank: int,
    upstream: dict[str, list[str]],
    down_here: set[str],
    roots: set[str],
    path_count: dict[str, int],
) -> list[str]:
    """Return the ``rank``-th (0-based) lexicographic all-faulty path.

    Combinatorial unranking: each faulty upstream owns a consecutive block of
    ``path_count[upstream]`` paths, so the target path is reconstructed by
    block subtraction while walking upstream.  The walk is iterative and
    strictly moves upstream, so its depth is bounded by the graph diameter
    rather than the Python recursion limit.
    """
    path = [start]
    current = start
    while current not in roots:
        for up in sorted(u for u in upstream.get(current, ()) if u in down_here):
            block = path_count[up]
            if rank < block:
                path.append(up)
                current = up
                break
            rank -= block
        else:  # pragma: no cover - invariant: rank < path_count[start]
            raise InvalidCursor("path rank is out of range for the current state")
    return path


def _build_classification(
    conn: sqlite3.Connection,
) -> dict:
    """Load one snapshot and compute every classification field except paths."""
    revision, version, upstream = _load_graph(conn)
    state_version = get_state_version(conn)
    down: set[str] = {
        r["service"]
        for r in conn.execute("SELECT service FROM service_state WHERE status = 'down'")
    }
    seq_rows = {
        r["service"]: r["last_seq"]
        for r in conn.execute("SELECT service, last_seq FROM service_state")
    }

    all_nodes = set(upstream)
    down_here = {s for s in down if s in all_nodes}
    topo = _topological_order(sorted(all_nodes), upstream)
    roots, reachable, path_count = _classify(upstream, down_here, topo)

    actionable: list[dict] = []
    suppressed_services: list[str] = []
    root_causes: dict[str, list[str]] = {}
    for svc in sorted(down_here):
        if svc in roots or not reachable[svc]:
            # Roots are always actionable; a fault whose every upstream path
            # is cut by a healthy/unreported node stays actionable as well.
            actionable.append({"service": svc, "last_seq": seq_rows[svc]})
        else:
            suppressed_services.append(svc)
            root_causes[svc] = sorted(reachable[svc])

    unreported = sorted(all_nodes - set(seq_rows))
    healthy = sorted(s for s in seq_rows if s in all_nodes and s not in down_here)

    return {
        "revision": revision,
        "version": version,
        "state_version": state_version,
        "upstream": upstream,
        "down_here": down_here,
        "roots": roots,
        "path_count": path_count,
        "seq_rows": seq_rows,
        "all_nodes": all_nodes,
        "actionable": actionable,
        "suppressed_services": suppressed_services,
        "root_causes": root_causes,
        "unreported": unreported,
        "healthy": healthy,
    }


def _base_response(ctx: dict) -> dict:
    return {
        "graph_revision": ctx["revision"],
        "graph_version": ctx["version"],
        "services": sorted(ctx["all_nodes"]),
        "actionable_faults": ctx["actionable"],
        "down_services": sorted(ctx["down_here"]),
        "unreported_services": ctx["unreported"],
        "healthy_services": ctx["healthy"],
    }


def _suppressed_entries(ctx: dict, page_paths: dict[str, list[list[str]]]) -> list[dict]:
    return [
        {
            "service": svc,
            "last_seq": ctx["seq_rows"][svc],
            "root_causes": ctx["root_causes"][svc],
            "paths": page_paths.get(svc, []),
        }
        for svc in ctx["suppressed_services"]
    ]


def query_alerts(
    limit: Optional[int] = None, cursor: Optional[str] = None
) -> dict:
    """Compute the alert classification, paging the (potentially huge) paths.

    Legacy mode (no ``limit`` and no ``cursor``): when every all-faulty path
    fits in :data:`DEFAULT_PAGE_LIMIT`, the response is byte-identical to the
    original non-paginated ``/alerts`` payload.  Otherwise the first page is
    returned together with an explicit pagination block instead of silently
    truncating.

    Paged mode: the cursor pins ``(graph_revision, state_version, offset,
    limit)``.  A graph replacement or an *applied* report changes one of the
    versions and invalidates every outstanding cursor; duplicate retries and
    rejected reports change nothing, so their cursors remain valid.  The page
    size travels inside the cursor, so follow-up requests that only pass
    ``cursor`` keep paging with the original ``limit``.
    """
    with read_tx() as conn:
        ctx = _build_classification(conn)
        revision = ctx["revision"]
        state_version = ctx["state_version"]

        if cursor is not None:
            payload = decode_cursor(conn, cursor)
            if payload["rev"] != revision or payload["sv"] != state_version:
                raise StaleCursor(
                    "pagination cursor refers to an older graph revision or "
                    "fault state; restart paging from GET /alerts without a cursor"
                )
            offset = payload["off"]
            # The cursor remembers its walk's page size; an explicit query
            # parameter may only override it with another valid size.
            page_size = payload["limit"] if limit is None else limit
        else:
            offset = 0
            page_size = DEFAULT_PAGE_LIMIT if limit is None else limit

        suppressed_services = ctx["suppressed_services"]
        path_count = ctx["path_count"]

        # Stable global ordering: services by name, paths lexicographically
        # within each service -- the same order the old sorted() output used.
        starts: dict[str, int] = {}
        running = 0
        total_paths = 0
        for svc in suppressed_services:
            starts[svc] = running
            total = path_count[svc]
            running += total
            total_paths = running

        if offset > total_paths:
            raise InvalidCursor(
                "cursor offset lies past the end of the current path list"
            )

        legacy_mode = limit is None and cursor is None

        if legacy_mode and total_paths <= DEFAULT_PAGE_LIMIT:
            # Small graph: enumerate every path (bounded by the page size) and
            # return the original, unpaginated response shape.
            page_paths = {
                svc: [
                    _unrank_path(
                        svc,
                        rank,
                        ctx["upstream"],
                        ctx["down_here"],
                        ctx["roots"],
                        path_count,
                    )
                    for rank in range(path_count[svc])
                ]
                for svc in suppressed_services
            }
            response = _base_response(ctx)
            response["suppressed_faults"] = _suppressed_entries(ctx, page_paths)
            return response

        # Paged response.  Only the ranks covered by this page are unranked;
        # no other page's paths are ever enumerated.
        remaining = page_size
        page_paths: dict[str, list[list[str]]] = {}
        for svc in suppressed_services:
            start = starts[svc]
            total = path_count[svc]
            if remaining <= 0 or offset >= start + total:
                continue
            local_start = max(0, offset - start)
            take = min(total - local_start, remaining)
            paths = [
                _unrank_path(
                    svc,
                    local_start + i,
                    ctx["upstream"],
                    ctx["down_here"],
                    ctx["roots"],
                    path_count,
                )
                for i in range(take)
            ]
            page_paths[svc] = paths
            remaining -= take

        returned = page_size - remaining
        next_offset = offset + returned
        complete = next_offset >= total_paths

        response = _base_response(ctx)
        response["suppressed_faults"] = _suppressed_entries(ctx, page_paths)
        if complete:
            next_cursor: Optional[str] = None
        else:
            next_cursor = encode_cursor(
                conn, revision, state_version, next_offset, page_size
            )
        response["pagination"] = {
            "limit": page_size,
            "offset": offset,
            "next_offset": next_offset,
            "returned_paths": returned,
            "total_paths": total_paths,
            "path_counts": {svc: path_count[svc] for svc in suppressed_services},
            "complete": complete,
            "truncated": not complete,
            "next_cursor": next_cursor,
        }
        if not complete:
            response["pagination"]["hint"] = (
                "result is paginated: repeat GET /alerts with cursor=next_cursor "
                "to read the remaining paths; the paths shown here are not a complete set"
            )
        return response


def classify_alerts() -> dict:
    """Backward-compatible wrapper: legacy, parameter-less alert classification."""
    return query_alerts()
