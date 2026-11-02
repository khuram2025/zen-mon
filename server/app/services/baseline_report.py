"""Scoped, current software-baseline coverage without loading every rule/server pair."""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.scoping import jsonb_tags_visible
from app.services.list_ordering import natural_sql, patterns


SCOPE_SQL = f"""b.enabled AND s.status IS DISTINCT FROM 'disabled'
    AND (b.os_type IS NULL OR b.os_type = s.os_type)
    AND (b.site_id IS NULL OR b.site_id = s.site_id)
    AND COALESCE(s.tags, '[]'::jsonb) @> b.match_tags
    AND (:scope_all OR {jsonb_tags_visible('s.tags')})"""

VALID_RESULTS_SQL = f"""SELECT res.* FROM server_baseline_results res
    JOIN software_baselines b ON b.id = res.baseline_id
    JOIN software_baseline_rules r ON r.id = res.rule_id AND r.baseline_id = b.id
    JOIN servers s ON s.id = res.server_id
    WHERE {SCOPE_SQL} AND res.evaluated_at >= b.updated_at"""


def scope_params(scope: list[str] | None) -> dict:
    return {"scope_all": scope is None, "vis_tags": scope or []}


def coverage_cte() -> str:
    return f"""WITH policy AS (SELECT * FROM software_baselines WHERE id = :bid),
    rules AS (SELECT r.* FROM software_baseline_rules r WHERE r.baseline_id = :bid),
    scoped AS MATERIALIZED (
        SELECT s.id AS server_id, s.display_name AS server_name, s.hostname,
               s.primary_ip, s.os_type, COALESCE(s.status, 'unknown') AS server_status, s.last_seen, s.inventory_updated_at
        FROM servers s CROSS JOIN policy b WHERE {SCOPE_SQL}
    ), current_results AS MATERIALIZED (
        SELECT res.* FROM server_baseline_results res
        JOIN rules r ON r.id = res.rule_id
        JOIN scoped s ON s.server_id = res.server_id
        CROSS JOIN policy b
        WHERE res.baseline_id = :bid AND res.evaluated_at >= b.updated_at
    ), coverage AS (
        SELECT s.*, COUNT(res.rule_id)::int AS checked,
               COUNT(res.rule_id) FILTER (WHERE res.status = 'compliant')::int AS compliant,
               COUNT(res.rule_id) FILTER (WHERE res.status != 'compliant')::int AS violations,
               COUNT(res.rule_id) FILTER (WHERE res.status != 'compliant' AND res.severity = 'critical')::int AS critical,
               COUNT(res.rule_id) FILTER (WHERE res.status = 'missing')::int AS missing,
               COUNT(res.rule_id) FILTER (WHERE res.status = 'outdated')::int AS outdated,
               COUNT(res.rule_id) FILTER (WHERE res.status = 'prohibited')::int AS prohibited,
               MIN(res.evaluated_at) AS evaluated_at,
               MIN(res.first_failed_at) FILTER (WHERE res.status != 'compliant') AS first_failed_at
        FROM scoped s LEFT JOIN current_results res ON res.server_id = s.server_id
        GROUP BY s.server_id, s.server_name, s.hostname, s.primary_ip, s.os_type, s.server_status, s.last_seen, s.inventory_updated_at
    ), classified AS MATERIALIZED (
        SELECT *, GREATEST(:rule_count - checked, 0)::int AS not_evaluated,
               CASE WHEN violations > 0 THEN 'violated'
                    WHEN :rule_count > 0 AND checked = :rule_count THEN 'compliant'
                    ELSE 'not_evaluated' END AS compliance_status
        FROM coverage
    )"""


def _decoded(value: Any, default: Any) -> Any:
    if value is None:
        return default
    return json.loads(value) if isinstance(value, str) else value


async def coverage_report(db: AsyncSession, baseline: dict, scope: list[str] | None,
                          *, q: str = '', status: str = 'all', sort: str = 'status',
                          order: str = 'asc', page: int = 1, page_size: int = 50) -> dict:
    params = {**scope_params(scope), 'bid': baseline['id'], 'rule_count': len(baseline['rules']),
              'limit': page_size, 'offset': (page - 1) * page_size}
    where = []
    if q.strip():
        params['search'] = patterns(q.strip())['list_contains']
        where.append("(server_name ILIKE :search ESCAPE '\\' OR hostname ILIKE :search ESCAPE '\\' OR host(primary_ip) ILIKE :search ESCAPE '\\')")
    if status != 'all':
        params['status'] = status
        where.append('compliance_status = :status')
    predicate = ' AND '.join(where) or 'TRUE'
    columns = {
        'status': "CASE compliance_status WHEN 'violated' THEN 0 WHEN 'not_evaluated' THEN 1 ELSE 2 END",
        'server_name': natural_sql('server_name'), 'primary_ip': 'primary_ip',
        'violations': 'violations', 'checked': 'checked', 'evaluated_at': 'evaluated_at',
    }
    direction = 'DESC' if order == 'desc' else 'ASC'
    ordering = f"{columns[sort]} {direction} NULLS LAST, {natural_sql('server_name')}, server_id"
    row = (await db.execute(text(coverage_cte() + f""",
        filtered AS (SELECT * FROM classified WHERE {predicate}),
        page_rows AS (SELECT * FROM filtered ORDER BY {ordering} LIMIT :limit OFFSET :offset)
        SELECT
          (SELECT jsonb_build_object(
            'servers_in_scope', COUNT(*),
            'compliant', COUNT(*) FILTER (WHERE compliance_status = 'compliant'),
            'violated', COUNT(*) FILTER (WHERE compliance_status = 'violated'),
            'not_evaluated', COUNT(*) FILTER (WHERE compliance_status = 'not_evaluated'),
            'evaluated', COUNT(*) FILTER (WHERE checked > 0),
            'violations', COALESCE(SUM(violations), 0),
            'last_evaluated', MAX(evaluated_at)) FROM classified) AS summary,
          (SELECT COUNT(*) FROM filtered) AS total,
          (SELECT jsonb_agg(to_jsonb(p) || jsonb_build_object('primary_ip', host(p.primary_ip))) FROM page_rows p) AS items,
          (SELECT jsonb_agg(to_jsonb(r)) FROM (
              SELECT rules.id AS rule_id,
                COUNT(res.server_id) FILTER (WHERE res.status = 'compliant')::int AS compliant,
                COUNT(res.server_id) FILTER (WHERE res.status != 'compliant')::int AS violated,
                ((SELECT COUNT(*) FROM scoped) - COUNT(res.server_id))::int AS not_evaluated
              FROM rules LEFT JOIN current_results res ON res.rule_id = rules.id
              GROUP BY rules.id, rules.package_match ORDER BY rules.package_match, rules.id
          ) r) AS rule_summary
    """), params)).mappings().one()
    summary = _decoded(row['summary'], {})
    baseline.update(servers_evaluated=summary['evaluated'], servers_compliant=summary['compliant'],
                    violations=summary['violations'])
    return {'baseline': baseline, 'summary': summary, 'items': _decoded(row['items'], []),
            'rule_summary': _decoded(row['rule_summary'], []), 'total': row['total'],
            'page': page, 'page_size': page_size}


async def server_checks(db: AsyncSession, baseline_id: UUID, server_id: UUID,
                        scope: list[str] | None) -> dict:
    params = {**scope_params(scope), 'bid': baseline_id, 'sid': server_id}
    server = (await db.execute(text(f"""SELECT s.id FROM servers s
        JOIN software_baselines b ON b.id = :bid WHERE s.id = :sid AND {SCOPE_SQL}"""), params)).first()
    if server is None:
        raise HTTPException(404, 'Server not in this baseline or not visible')
    rows = (await db.execute(text("""SELECT r.id AS rule_id, r.baseline_id, r.rule_type,
        r.package_match, r.match_type, r.min_version, r.severity, r.notes,
        COALESCE(res.status, 'not_evaluated') AS status, res.found_package, res.found_version,
        res.expected, res.first_failed_at, res.evaluated_at
        FROM software_baseline_rules r
        JOIN software_baselines b ON b.id = r.baseline_id
        LEFT JOIN server_baseline_results res ON res.rule_id = r.id AND res.server_id = :sid
            AND res.baseline_id = b.id AND res.evaluated_at >= b.updated_at
        WHERE r.baseline_id = :bid
        ORDER BY CASE COALESCE(res.status, 'not_evaluated') WHEN 'compliant' THEN 2
            WHEN 'not_evaluated' THEN 1 ELSE 0 END,
            CASE r.severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
            r.package_match, r.id"""), params)).mappings().all()
    return {'items': [dict(r) for r in rows]}
