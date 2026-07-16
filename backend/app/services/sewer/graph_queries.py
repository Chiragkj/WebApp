"""Graph traversal queries for the normalized sewer network.

The sewer graph is directed:

    upstream_manhole_id -> downstream_manhole_id

Only pipes with both accepted manhole endpoints participate in directed
upstream/downstream traversal.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def get_upstream_network(
    db: AsyncSession,
    dataset_id: uuid.UUID,
    manhole_id: uuid.UUID,
    max_depth: int = 100,
) -> dict[str, Any]:
    """Return all accepted pipes and manholes upstream of a manhole."""

    _validate_max_depth(max_depth)

    result = await db.execute(
        text(
            """
            WITH RECURSIVE upstream_graph AS (
                SELECT
                    p.id AS pipe_id,
                    p.asset_id AS pipe_asset_id,
                    p.upstream_manhole_id,
                    p.downstream_manhole_id,
                    p.length_m,
                    p.topology_confidence,
                    1 AS depth,
                    ARRAY[
                        p.downstream_manhole_id,
                        p.upstream_manhole_id
                    ]::uuid[] AS visited_nodes
                FROM sewer_pipes p
                WHERE p.dataset_id = :dataset_id
                  AND p.downstream_manhole_id = :manhole_id
                  AND p.upstream_manhole_id IS NOT NULL
                  AND p.downstream_manhole_id IS NOT NULL

                UNION ALL

                SELECT
                    parent.id AS pipe_id,
                    parent.asset_id AS pipe_asset_id,
                    parent.upstream_manhole_id,
                    parent.downstream_manhole_id,
                    parent.length_m,
                    parent.topology_confidence,
                    graph.depth + 1,
                    graph.visited_nodes || parent.upstream_manhole_id
                FROM upstream_graph graph
                JOIN sewer_pipes parent
                  ON parent.dataset_id = :dataset_id
                 AND parent.downstream_manhole_id =
                     graph.upstream_manhole_id
                WHERE graph.depth < :max_depth
                  AND parent.upstream_manhole_id IS NOT NULL
                  AND parent.downstream_manhole_id IS NOT NULL
                  AND NOT (
                        parent.upstream_manhole_id =
                        ANY(graph.visited_nodes)
                  )
            )
            SELECT
                graph.pipe_id,
                graph.pipe_asset_id,
                graph.upstream_manhole_id,
                upstream.asset_id AS upstream_manhole_asset_id,
                graph.downstream_manhole_id,
                downstream.asset_id AS downstream_manhole_asset_id,
                graph.length_m,
                graph.topology_confidence,
                graph.depth
            FROM upstream_graph graph
            JOIN sewer_manholes upstream
              ON upstream.id = graph.upstream_manhole_id
            JOIN sewer_manholes downstream
              ON downstream.id = graph.downstream_manhole_id
            ORDER BY graph.depth, graph.pipe_asset_id
            """
        ),
        {
            "dataset_id": dataset_id,
            "manhole_id": manhole_id,
            "max_depth": max_depth,
        },
    )

    rows = result.mappings().all()

    return _build_traversal_response(
        dataset_id=dataset_id,
        start_manhole_id=manhole_id,
        direction="upstream",
        rows=rows,
        max_depth=max_depth,
    )


async def get_downstream_network(
    db: AsyncSession,
    dataset_id: uuid.UUID,
    manhole_id: uuid.UUID,
    max_depth: int = 100,
) -> dict[str, Any]:
    """Return all accepted pipes and manholes downstream of a manhole."""

    _validate_max_depth(max_depth)

    result = await db.execute(
        text(
            """
            WITH RECURSIVE downstream_graph AS (
                SELECT
                    p.id AS pipe_id,
                    p.asset_id AS pipe_asset_id,
                    p.upstream_manhole_id,
                    p.downstream_manhole_id,
                    p.length_m,
                    p.topology_confidence,
                    1 AS depth,
                    ARRAY[
                        p.upstream_manhole_id,
                        p.downstream_manhole_id
                    ]::uuid[] AS visited_nodes
                FROM sewer_pipes p
                WHERE p.dataset_id = :dataset_id
                  AND p.upstream_manhole_id = :manhole_id
                  AND p.upstream_manhole_id IS NOT NULL
                  AND p.downstream_manhole_id IS NOT NULL

                UNION ALL

                SELECT
                    child.id AS pipe_id,
                    child.asset_id AS pipe_asset_id,
                    child.upstream_manhole_id,
                    child.downstream_manhole_id,
                    child.length_m,
                    child.topology_confidence,
                    graph.depth + 1,
                    graph.visited_nodes || child.downstream_manhole_id
                FROM downstream_graph graph
                JOIN sewer_pipes child
                  ON child.dataset_id = :dataset_id
                 AND child.upstream_manhole_id =
                     graph.downstream_manhole_id
                WHERE graph.depth < :max_depth
                  AND child.upstream_manhole_id IS NOT NULL
                  AND child.downstream_manhole_id IS NOT NULL
                  AND NOT (
                        child.downstream_manhole_id =
                        ANY(graph.visited_nodes)
                  )
            )
            SELECT
                graph.pipe_id,
                graph.pipe_asset_id,
                graph.upstream_manhole_id,
                upstream.asset_id AS upstream_manhole_asset_id,
                graph.downstream_manhole_id,
                downstream.asset_id AS downstream_manhole_asset_id,
                graph.length_m,
                graph.topology_confidence,
                graph.depth
            FROM downstream_graph graph
            JOIN sewer_manholes upstream
              ON upstream.id = graph.upstream_manhole_id
            JOIN sewer_manholes downstream
              ON downstream.id = graph.downstream_manhole_id
            ORDER BY graph.depth, graph.pipe_asset_id
            """
        ),
        {
            "dataset_id": dataset_id,
            "manhole_id": manhole_id,
            "max_depth": max_depth,
        },
    )

    rows = result.mappings().all()

    return _build_traversal_response(
        dataset_id=dataset_id,
        start_manhole_id=manhole_id,
        direction="downstream",
        rows=rows,
        max_depth=max_depth,
    )


async def get_connected_component(
    db: AsyncSession,
    dataset_id: uuid.UUID,
    manhole_id: uuid.UUID,
    max_depth: int = 200,
) -> dict[str, Any]:
    """Return the undirected accepted component containing a manhole."""

    _validate_max_depth(max_depth)

    result = await db.execute(
        text(
            """
            WITH RECURSIVE edges AS (
                SELECT
                    id AS pipe_id,
                    asset_id AS pipe_asset_id,
                    upstream_manhole_id AS node_a,
                    downstream_manhole_id AS node_b,
                    length_m,
                    topology_confidence
                FROM sewer_pipes
                WHERE dataset_id = :dataset_id
                  AND upstream_manhole_id IS NOT NULL
                  AND downstream_manhole_id IS NOT NULL

                UNION ALL

                SELECT
                    id AS pipe_id,
                    asset_id AS pipe_asset_id,
                    downstream_manhole_id AS node_a,
                    upstream_manhole_id AS node_b,
                    length_m,
                    topology_confidence
                FROM sewer_pipes
                WHERE dataset_id = :dataset_id
                  AND upstream_manhole_id IS NOT NULL
                  AND downstream_manhole_id IS NOT NULL
            ),

            component AS (
                SELECT
                    CAST(:manhole_id AS uuid) AS node_id,
                    0 AS depth,
                    ARRAY[CAST(:manhole_id AS uuid)]::uuid[] AS visited_nodes

                UNION ALL

                SELECT
                    edge.node_b,
                    component.depth + 1,
                    component.visited_nodes || edge.node_b
                FROM component
                JOIN edges edge
                  ON edge.node_a = component.node_id
                WHERE component.depth < :max_depth
                  AND NOT (
                        edge.node_b = ANY(component.visited_nodes)
                  )
            ),

            distinct_nodes AS (
                SELECT
                    node_id,
                    min(depth) AS depth
                FROM component
                GROUP BY node_id
            )

            SELECT
                node.node_id AS manhole_id,
                manhole.asset_id AS manhole_asset_id,
                node.depth,
                manhole.incoming_pipe_count,
                manhole.outgoing_pipe_count,
                manhole.topology_status,
                ST_AsGeoJSON(manhole.geom)::json AS geometry
            FROM distinct_nodes node
            JOIN sewer_manholes manhole
              ON manhole.id = node.node_id
            ORDER BY node.depth, manhole.asset_id
            """
        ),
        {
            "dataset_id": dataset_id,
            "manhole_id": manhole_id,
            "max_depth": max_depth,
        },
    )

    node_rows = result.mappings().all()

    node_ids = [row["manhole_id"] for row in node_rows]

    pipe_rows: list[dict[str, Any]] = []

    if node_ids:
        pipe_result = await db.execute(
            text(
                """
                SELECT
                    p.id AS pipe_id,
                    p.asset_id AS pipe_asset_id,
                    p.upstream_manhole_id,
                    upstream.asset_id AS upstream_manhole_asset_id,
                    p.downstream_manhole_id,
                    downstream.asset_id AS downstream_manhole_asset_id,
                    p.length_m,
                    p.topology_status,
                    p.topology_confidence
                FROM sewer_pipes p
                JOIN sewer_manholes upstream
                  ON upstream.id = p.upstream_manhole_id
                JOIN sewer_manholes downstream
                  ON downstream.id = p.downstream_manhole_id
                WHERE p.dataset_id = :dataset_id
                    AND p.upstream_manhole_id =
                        ANY(CAST(:node_ids AS uuid[]))
                    AND p.downstream_manhole_id =
                        ANY(CAST(:node_ids AS uuid[]))
                ORDER BY p.asset_id
                """
            ),
            {
                "dataset_id": dataset_id,
                "node_ids": node_ids,
            },
        )

        pipe_rows = [
            _serialize_pipe_row(row)
            for row in pipe_result.mappings().all()
        ]

    return {
        "dataset_id": str(dataset_id),
        "start_manhole_id": str(manhole_id),
        "manhole_count": len(node_rows),
        "pipe_count": len(pipe_rows),
        "manholes": [
            {
                "manhole_id": str(row["manhole_id"]),
                "asset_id": row["manhole_asset_id"],
                "depth": row["depth"],
                "incoming_pipe_count": row["incoming_pipe_count"],
                "outgoing_pipe_count": row["outgoing_pipe_count"],
                "topology_status": row["topology_status"],
                "geometry": row["geometry"],
            }
            for row in node_rows
        ],
        "pipes": pipe_rows,
    }


async def get_direct_connections(
    db: AsyncSession,
    dataset_id: uuid.UUID,
    manhole_id: uuid.UUID,
) -> dict[str, Any]:
    """Return directly connected incoming and outgoing pipes."""

    manhole_result = await db.execute(
        text(
            """
            SELECT
                id,
                asset_id,
                incoming_pipe_count,
                outgoing_pipe_count,
                topology_status,
                confidence_score,
                ST_AsGeoJSON(geom)::json AS geometry
            FROM sewer_manholes
            WHERE dataset_id = :dataset_id
              AND id = :manhole_id
            """
        ),
        {
            "dataset_id": dataset_id,
            "manhole_id": manhole_id,
        },
    )

    manhole = manhole_result.mappings().one_or_none()

    if manhole is None:
        return {
            "dataset_id": str(dataset_id),
            "manhole_id": str(manhole_id),
            "found": False,
            "incoming_pipes": [],
            "outgoing_pipes": [],
        }

    incoming_result = await db.execute(
        text(
            """
            SELECT
                p.id AS pipe_id,
                p.asset_id AS pipe_asset_id,
                p.upstream_manhole_id,
                upstream.asset_id AS upstream_manhole_asset_id,
                p.downstream_manhole_id,
                downstream.asset_id AS downstream_manhole_asset_id,
                p.length_m,
                parent.topology_status,
                p.topology_confidence
            FROM sewer_pipes p
            LEFT JOIN sewer_manholes upstream
              ON upstream.id = p.upstream_manhole_id
            LEFT JOIN sewer_manholes downstream
              ON downstream.id = p.downstream_manhole_id
            WHERE p.dataset_id = :dataset_id
              AND p.downstream_manhole_id = :manhole_id
            ORDER BY p.asset_id
            """
        ),
        {
            "dataset_id": dataset_id,
            "manhole_id": manhole_id,
        },
    )

    outgoing_result = await db.execute(
        text(
            """
            SELECT
                p.id AS pipe_id,
                p.asset_id AS pipe_asset_id,
                p.upstream_manhole_id,
                upstream.asset_id AS upstream_manhole_asset_id,
                p.downstream_manhole_id,
                downstream.asset_id AS downstream_manhole_asset_id,
                p.length_m,
                p.topology_status,
                p.topology_confidence
            FROM sewer_pipes p
            LEFT JOIN sewer_manholes upstream
              ON upstream.id = p.upstream_manhole_id
            LEFT JOIN sewer_manholes downstream
              ON downstream.id = p.downstream_manhole_id
            WHERE p.dataset_id = :dataset_id
              AND p.upstream_manhole_id = :manhole_id
            ORDER BY p.asset_id
            """
        ),
        {
            "dataset_id": dataset_id,
            "manhole_id": manhole_id,
        },
    )

    return {
        "dataset_id": str(dataset_id),
        "manhole_id": str(manhole["id"]),
        "manhole_asset_id": manhole["asset_id"],
        "found": True,
        "incoming_pipe_count": manhole["incoming_pipe_count"],
        "outgoing_pipe_count": manhole["outgoing_pipe_count"],
        "topology_status": manhole["topology_status"],
        "confidence_score": manhole["confidence_score"],
        "geometry": manhole["geometry"],
        "incoming_pipes": [
            _serialize_pipe_row(row)
            for row in incoming_result.mappings().all()
        ],
        "outgoing_pipes": [
            _serialize_pipe_row(row)
            for row in outgoing_result.mappings().all()
        ],
    }


async def get_graph_summary(
    db: AsyncSession,
    dataset_id: uuid.UUID,
) -> dict[str, Any]:
    """Return a graph-oriented network summary."""

    result = await db.execute(
        text(
            """
            SELECT
                (
                    SELECT count(*)
                    FROM sewer_manholes
                    WHERE dataset_id = :dataset_id
                ) AS total_manholes,

                (
                    SELECT count(*)
                    FROM sewer_pipes
                    WHERE dataset_id = :dataset_id
                ) AS total_pipes,

                (
                    SELECT count(*)
                    FROM sewer_pipes
                    WHERE dataset_id = :dataset_id
                      AND upstream_manhole_id IS NOT NULL
                      AND downstream_manhole_id IS NOT NULL
                ) AS fully_connected_pipes,

                (
                    SELECT count(*)
                    FROM sewer_pipes
                    WHERE dataset_id = :dataset_id
                      AND (
                            upstream_manhole_id IS NULL
                            OR downstream_manhole_id IS NULL
                      )
                ) AS incomplete_pipes,

                (
                    SELECT count(*)
                    FROM sewer_manholes
                    WHERE dataset_id = :dataset_id
                      AND incoming_pipe_count = 0
                      AND outgoing_pipe_count = 0
                ) AS isolated_manholes,

                (
                    SELECT count(*)
                    FROM sewer_manholes
                    WHERE dataset_id = :dataset_id
                      AND incoming_pipe_count > 0
                      AND outgoing_pipe_count = 0
                ) AS terminal_manholes,

                (
                    SELECT count(*)
                    FROM sewer_manholes
                    WHERE dataset_id = :dataset_id
                      AND incoming_pipe_count = 0
                      AND outgoing_pipe_count > 0
                ) AS source_manholes
            """
        ),
        {"dataset_id": dataset_id},
    )

    row = result.mappings().one()

    total_pipes = int(row["total_pipes"] or 0)
    fully_connected = int(row["fully_connected_pipes"] or 0)

    connection_percentage = (
        round((fully_connected / total_pipes) * 100, 2)
        if total_pipes
        else 0.0
    )

    return {
        "dataset_id": str(dataset_id),
        "total_manholes": int(row["total_manholes"] or 0),
        "total_pipes": total_pipes,
        "fully_connected_pipes": fully_connected,
        "incomplete_pipes": int(row["incomplete_pipes"] or 0),
        "isolated_manholes": int(row["isolated_manholes"] or 0),
        "source_manholes": int(row["source_manholes"] or 0),
        "terminal_manholes": int(row["terminal_manholes"] or 0),
        "connection_percentage": connection_percentage,
    }


def _build_traversal_response(
    dataset_id: uuid.UUID,
    start_manhole_id: uuid.UUID,
    direction: str,
    rows: list[Any],
    max_depth: int,
) -> dict[str, Any]:
    """Create a stable JSON response for graph traversal queries."""

    pipes = [_serialize_pipe_row(row) for row in rows]

    manholes: dict[str, dict[str, Any]] = {}

    for row in rows:
        upstream_value = row["upstream_manhole_id"]
        downstream_value = row["downstream_manhole_id"]

        if upstream_value is not None:
            upstream_id = str(upstream_value)

            manholes[upstream_id] = {
                "manhole_id": upstream_id,
                "asset_id": row.get(
                    "upstream_manhole_asset_id"
                ),
            }

        if downstream_value is not None:
            downstream_id = str(downstream_value)

            manholes[downstream_id] = {
                "manhole_id": downstream_id,
                "asset_id": row.get(
                    "downstream_manhole_asset_id"
                ),
            }

    return {
        "dataset_id": str(dataset_id),
        "start_manhole_id": str(start_manhole_id),
        "direction": direction,
        "max_depth_requested": max_depth,
        "maximum_depth_reached": maximum_depth_reached,
        "manhole_count": len(manholes),
        "pipe_count": len(pipes),
        "total_pipe_length_m": round(total_length_m, 3),
        "manholes": list(manholes.values()),
        "pipes": pipes,
    }


def _serialize_pipe_row(row: Any) -> dict[str, Any]:
    """Serialize a pipe mapping returned by SQLAlchemy."""

    return {
        "pipe_id": str(row["pipe_id"]),
        "asset_id": row["pipe_asset_id"],
        "upstream_manhole_id": (
            str(row["upstream_manhole_id"])
            if row["upstream_manhole_id"]
            else None
        ),
        "upstream_manhole_asset_id": row.get(
            "upstream_manhole_asset_id"
        ),
        "downstream_manhole_id": (
            str(row["downstream_manhole_id"])
            if row["downstream_manhole_id"]
            else None
        ),
        "downstream_manhole_asset_id": row.get(
            "downstream_manhole_asset_id"
        ),
        "length_m": row.get("length_m"),
        "topology_status": row.get("topology_status"),
        "topology_confidence": row.get(
            "topology_confidence"
        ),
        "depth": row.get("depth"),
    }


def _validate_max_depth(max_depth: int) -> None:
    """Protect recursive SQL from unreasonable traversal depth."""

    if max_depth < 1:
        raise ValueError("max_depth must be at least 1.")

    if max_depth > 500:
        raise ValueError("max_depth cannot exceed 500.")