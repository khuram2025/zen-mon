"""Backup coverage must describe fresh, validated recovery data, not row counts."""
from datetime import datetime,timezone,timedelta
from ipaddress import ip_address


def backup_status(row,now=None):
    now=now or datetime.now(timezone.utc)
    try:
        ip=ip_address(row.ip)
        candidate=not(ip.is_loopback or ip.is_multicast or ip.is_unspecified)
    except (ValueError,TypeError):
        candidate=False
    candidate=candidate and row.device_type in ('router','switch','firewall','access_point','load_balancer')
    eligible=row.eligible_override if row.eligible_override is not None else candidate
    # Each required type needs a successful validated run with a surviving
    # artifact. Manual imports and old success flags cannot refresh coverage.
    types=getattr(row,'config_types',None) or ['running']
    verified=getattr(row,'verified_types',None) or {}
    times=[datetime.fromisoformat(verified[t]) for t in types if verified.get(t)]
    checked=min(times) if len(times)==len(types) else None
    fresh=bool(row.ncm_enabled and checked and checked <= now and now-checked <= timedelta(hours=row.freshness_hours or 24))
    state='excluded' if not eligible else 'unconfigured' if not row.platform else 'disabled' if not row.ncm_enabled else 'failed' if row.last_status=='failed' else 'fresh' if fresh else 'stale' if row.validated_at else 'unverified' if row.versions else 'pending'
    return {'eligible':bool(eligible),'fresh':fresh,'backup_state':state,'validated_at':row.validated_at.isoformat() if row.validated_at else None}
