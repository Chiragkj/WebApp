"""Validation rules for the normalized sewer network."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def validate_sewer_network(
    db: AsyncSession,
    dataset_id: uuid.UUID,
) -> dict[str, Any]:
    """Run deterministic sewer-network validation.

    The validator detects:

    - unresolved upstream endpoints
    - unresolved downstream endpoints
    - self-loop pipes
    - duplicate manhole connections
    - unused manholes
    - dead-end manholes
    - disconnected network components
    """

    await _delete_previous_validation_issues(db, dataset_id)

    missing_upstream = await _detect_missing_upstream_nodes(
        db,
        dataset_id,
    )

    missing_downstream = await _detect_missing_downstream_nodes(
        db,
        dataset_id,
    )

    self_loops = await _detect_self_loop_pipes(
        db,
        dataset_id,
    )

    duplicate_connections = await _detect_duplicate_connections(
        db,
        dataset_id,
    )

    unused_manholes = await _detect_unused_manholes(
        db,
        dataset_id,
    )

    dead_end_manholes = await _detect_dead_end_manholes(
        db,
        dataset_id,
    )

    connected_components = await _assign_connected_components(
        db,
        dataset_id,
    )

    await db.commit()

    total_issues = (
        missing_upstream
        + missing_downstream
        + self_loops
        + duplicate_connections
        + unused_manholes
        + dead_end_manholes
    )

    return {
        "dataset_id": str(dataset_id),
        "missing_upstream_nodes": missing_upstream,
        "missing_downstream_nodes": missing_downstream,
        "self_loop_pipes": self_loops,
        "duplicate_connections": duplicate_connections,
        "unused_manholes": unused_manholes,
        "dead_end_manholes": dead_end_manholes,
        "connected_components": connected_components,
        "validation_issues_created": total_issues,
    }


async def _delete_previous_validation_issues(
    db: AsyncSession,
    dataset_id: uuid.UUID,
) -> None:
    """Delete only issues produced by this validation module."""

    await db.execute(
        text(
            """
            DELETE FROM sewer_topology_issues
            WHERE dataset_id = :dataset_id
              AND issue_type IN (
                    'missing_upstream_manhole',
                    'missing_downstream_manhole',
                    'self_loop_pipe',
                    'duplicate_connection',
                    'unused_manhole',
                    'dead_end_manhole'
              )
            """
        ),
        {"dataset_id": dataset_id},
    )


async def _detect_missing_upstream_nodes(
    db: AsyncSession,
    dataset_id: uuid.UUID,
) -> int:
    """Create issues for pipes with no accepted upstream manhole."""

    result = await db.execute(
        text(
            """
            INSERT INTO sewer_topology_issues (
                id,
                dataset_id,
                pipe_id,
                issue_type,
                severity,
                status,
                title,
                description,
                confidence_score,
                details,
                geom,
                created_at,
                updated_at
            )
            SELECT
                gen_random_uuid(),
                p.dataset_id,
                p.id,
                'missing_upstream_manhole',
                'high',
                'open',
                'Pipe has no accepted upstream manhole',
                'The pipe start endpoint is not connected to an accepted manhole.',
                1.0,
                jsonb_build_object(
                    'pipe_asset_id',
                    p.asset_id,
                    'start_snap_distance_m',
                    p.start_snap_distance_m,
                    'topology_status',
                    p.topology_status
                ),
                ST_StartPoint(ST_LineMerge(p.geom)),
                now(),
                now()
            FROM sewer_pipes p
            WHERE p.dataset_id = :dataset_id
              AND p.upstream_manhole_id IS NULL
            RETURNING id
            """
        ),
        {"dataset_id": dataset_id},
    )

    return len(result.fetchall())


async def _detect_missing_downstream_nodes(
    db: AsyncSession,
    dataset_id: uuid.UUID,
) -> int:
    """Create issues for pipes with no accepted downstream manhole."""

    result = await db.execute(
        text(
            """
            INSERT INTO sewer_topology_issues (
                id,
                dataset_id,
                pipe_id,
                issue_type,
                severity,
                status,
                title,
                description,
                confidence_score,
                details,
                geom,
                created_at,
                updated_at
            )
            SELECT
                gen_random_uuid(),
                p.dataset_id,
                p.id,
                'missing_downstream_manhole',
                'high',
                'open',
                'Pipe has no accepted downstream manhole',
                'The pipe end endpoint is not connected to an accepted manhole.',
                1.0,
                jsonb_build_object(
                    'pipe_asset_id',
                    p.asset_id,
                    'end_snap_distance_m',
                    p.end_snap_distance_m,
                    'topology_status',
                    p.topology_status
                ),
                ST_EndPoint(ST_LineMerge(p.geom)),
                now(),
                now()
            FROM sewer_pipes p
            WHERE p.dataset_id = :dataset_id
              AND p.downstream_manhole_id IS NULL
            RETURNING id
            """
        ),
        {"dataset_id": dataset_id},
    )

    return len(result.fetchall())


async def _detect_self_loop_pipes(
    db: AsyncSession,
    dataset_id: uuid.UUID,
) -> int:
    """Detect pipes whose accepted start and end manholes are identical."""

    result = await db.execute(
        text(
            """
            INSERT INTO sewer_topology_issues (
                id,
                dataset_id,
                pipe_id,
                manhole_id,
                issue_type,
                severity,
                status,
                title,
                description,
                confidence_score,
                details,
                geom,
                created_at,
                updated_at
            )
            SELECT
                gen_random_uuid(),
                p.dataset_id,
                p.id,
                p.upstream_manhole_id,
                'self_loop_pipe',
                'high',
                'open',
                'Pipe connects a manhole to itself',
                'The accepted upstream and downstream manholes are identical.',
                1.0,
                jsonb_build_object(
                    'pipe_asset_id',
                    p.asset_id,
                    'manhole_id',
                    p.upstream_manhole_id
                ),
                p.geom,
                now(),
                now()
            FROM sewer_pipes p
            WHERE p.dataset_id = :dataset_id
              AND p.upstream_manhole_id IS NOT NULL
              AND p.downstream_manhole_id IS NOT NULL
              AND p.upstream_manhole_id = p.downstream_manhole_id
            RETURNING id
            """
        ),
        {"dataset_id": dataset_id},
    )

    return len(result.fetchall())


async def _detect_duplicate_connections(
    db: AsyncSession,
    dataset_id: uuid.UUID,
) -> int:
    """Detect multiple pipes connecting the same accepted manhole pair."""

    result = await db.execute(
        text(
            """
            WITH duplicate_groups AS (
                SELECT
                    upstream_manhole_id,
                    downstream_manhole_id,
                    count(*) AS pipe_count,
                    array_agg(id) AS pipe_ids,
                    array_agg(asset_id) AS pipe_asset_ids
                FROM sewer_pipes
                WHERE dataset_id = :dataset_id
                  AND upstream_manhole_id IS NOT NULL
                  AND downstream_manhole_id IS NOT NULL
                GROUP BY
                    upstream_manhole_id,
                    downstream_manhole_id
                HAVING count(*) > 1
            )
            INSERT INTO sewer_topology_issues (
                id,
                dataset_id,
                pipe_id,
                issue_type,
                severity,
                status,
                title,
                description,
                confidence_score,
                details,
                geom,
                created_at,
                updated_at
            )
            SELECT
                gen_random_uuid(),
                p.dataset_id,
                p.id,
                'duplicate_connection',
                'medium',
                'open',
                'Duplicate manhole connection detected',
                'Multiple sewer pipes connect the same upstream and downstream manholes.',
                0.95,
                jsonb_build_object(
                    'upstream_manhole_id',
                    duplicate_groups.upstream_manhole_id,
                    'downstream_manhole_id',
                    duplicate_groups.downstream_manhole_id,
                    'pipe_count',
                    duplicate_groups.pipe_count,
                    'pipe_ids',
                    duplicate_groups.pipe_ids,
                    'pipe_asset_ids',
                    duplicate_groups.pipe_asset_ids
                ),
                p.geom,
                now(),
                now()
            FROM duplicate_groups
            JOIN sewer_pipes p
              ON p.id = ANY(duplicate_groups.pipe_ids)
            RETURNING id
            """
        ),
        {"dataset_id": dataset_id},
    )

    return len(result.fetchall())


async def _detect_unused_manholes(
    db: AsyncSession,
    dataset_id: uuid.UUID,
) -> int:
    """Detect manholes that have no accepted incoming or outgoing pipes."""

    result = await db.execute(
        text(
            """
            INSERT INTO sewer_topology_issues (
                id,
                dataset_id,
                manhole_id,
                issue_type,
                severity,
                status,
                title,
                description,
                confidence_score,
                details,
                geom,
                created_at,
                updated_at
            )
            SELECT
                gen_random_uuid(),
                m.dataset_id,
                m.id,
                'unused_manhole',
                'medium',
                'open',
                'Manhole is not connected to the sewer graph',
                'No accepted upstream or downstream pipe references this manhole.',
                1.0,
                jsonb_build_object(
                    'manhole_asset_id',
                    m.asset_id,
                    'incoming_pipe_count',
                    m.incoming_pipe_count,
                    'outgoing_pipe_count',
                    m.outgoing_pipe_count
                ),
                m.geom,
                now(),
                now()
            FROM sewer_manholes m
            WHERE m.dataset_id = :dataset_id
              AND NOT EXISTS (
                    SELECT 1
                    FROM sewer_pipes p
                    WHERE p.dataset_id = m.dataset_id
                      AND (
                            p.upstream_manhole_id = m.id
                            OR p.downstream_manhole_id = m.id
                      )
              )
            RETURNING id
            """
        ),
        {"dataset_id": dataset_id},
    )

    return len(result.fetchall())


async def _detect_dead_end_manholes(
    db: AsyncSession,
    dataset_id: uuid.UUID,
) -> int:
    """Detect connected manholes without an accepted outgoing pipe."""

    result = await db.execute(
        text(
            """
            INSERT INTO sewer_topology_issues (
                id,
                dataset_id,
                manhole_id,
                issue_type,
                severity,
                status,
                title,
                description,
                confidence_score,
                details,
                geom,
                created_at,
                updated_at
            )
            SELECT
                gen_random_uuid(),
                m.dataset_id,
                m.id,
                'dead_end_manhole',
                'medium',
                'open',
                'Connected manhole has no downstream pipe',
                'The manhole receives at least one pipe but has no accepted outgoing pipe.',
                0.9,
                jsonb_build_object(
                    'manhole_asset_id',
                    m.asset_id,
                    'incoming_pipe_count',
                    m.incoming_pipe_count,
                    'outgoing_pipe_count',
                    m.outgoing_pipe_count
                ),
                m.geom,
                now(),
                now()
            FROM sewer_manholes m
            WHERE m.dataset_id = :dataset_id
              AND EXISTS (
                    SELECT 1
                    FROM sewer_pipes incoming
                    WHERE incoming.dataset_id = m.dataset_id
                      AND incoming.downstream_manhole_id = m.id
              )
              AND NOT EXISTS (
                    SELECT 1
                    FROM sewer_pipes outgoing
                    WHERE outgoing.dataset_id = m.dataset_id
                      AND outgoing.upstream_manhole_id = m.id
              )
            RETURNING id
            """
        ),
        {"dataset_id": dataset_id},
    )

    return len(result.fetchall())


async def _assign_connected_components(
    db: AsyncSession,
    dataset_id: uuid.UUID,
) -> int:
    """Calculate the number of accepted undirected sewer components.

    PostgreSQL recursive traversal is used so no additional graph library is
    required for this first implementation.
    """

    result = await db.execute(
        text(
            """
            WITH RECURSIVE accepted_edges AS (
                SELECT
                    upstream_manhole_id AS node_a,
                    downstream_manhole_id AS node_b
                FROM sewer_pipes
                WHERE dataset_id = :dataset_id
                  AND upstream_manhole_id IS NOT NULL
                  AND downstream_manhole_id IS NOT NULL

                UNION

                SELECT
                    downstream_manhole_id AS node_a,
                    upstream_manhole_id AS node_b
                FROM sewer_pipes
                WHERE dataset_id = :dataset_id
                  AND upstream_manhole_id IS NOT NULL
                  AND downstream_manhole_id IS NOT NULL
            ),

            nodes AS (
                SELECT id AS node_id
                FROM sewer_manholes
                WHERE dataset_id = :dataset_id
            ),

            reachability AS (
                SELECT
                    node_id AS origin,
                    node_id AS reached
                FROM nodes

                UNION

                SELECT
                    reachability.origin,
                    accepted_edges.node_b
                FROM reachability
                JOIN accepted_edges
                  ON accepted_edges.node_a = reachability.reached
            ),

            component_roots AS (
                SELECT
                    reached AS node_id,
                    min(origin::text)::uuid AS component_root
                FROM reachability
                GROUP BY reached
            )

            SELECT count(DISTINCT component_root)
            FROM component_roots
            """
        ),
        {"dataset_id": dataset_id},
    )

    value = result.scalar_one_or_none()

    return int(value or 0)