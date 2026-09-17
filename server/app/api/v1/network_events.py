from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.core.security import get_current_user
from app.core.scoping import visible_tags, jsonb_tags_visible
from app.models.user import User

router = APIRouter(prefix='/network-events', tags=['Network Events'])


@router.get('')
async def list_events(hours: int = Query(24, ge=1, le=720), search: str = Query('', max_length=256),
                      severity: int = Query(7, ge=0, le=7), limit: int = Query(200, ge=1, le=1000),
                      db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    params = dict(hours=hours, search=search, severity=severity, limit=limit)
    where = ["e.received_at >= now() - make_interval(hours => :hours)",
             "e.severity <= :severity", "position(lower(:search) in lower(e.message)) > 0"]
    scope = await visible_tags(db, user)
    if scope is not None:
        where.append(jsonb_tags_visible('d.tags'))
        params['vis_tags'] = scope
    rows = (await db.execute(text('''SELECT e.*, d.hostname AS device_hostname
        FROM network_events e LEFT JOIN devices d ON d.id = e.device_id
        WHERE ''' + ' AND '.join(where) + ' ORDER BY e.received_at DESC LIMIT :limit'), params)).mappings().all()
    return {'data': [dict(r) for r in rows]}
