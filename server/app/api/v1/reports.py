from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.services.report_service import generate_report
from app.services.export_service import generate_excel_report, generate_csv_report

router = APIRouter(prefix="/reports", tags=["Reports"])

REPORT_NAMES = {
    'executive_summary': 'Executive-Summary',
    'device_health': 'Device-Health',
    'service_health': 'Service-Health',
    'alert_analysis': 'Alert-Analysis',
    'full_report': 'Full-Report',
}


class ReportRequest(BaseModel):
    report_type: str = "executive_summary"
    period: str = "last_24h"
    from_time: Optional[datetime] = None
    to_time: Optional[datetime] = None
    device_ids: Optional[list[str]] = None
    group_ids: Optional[list[str]] = None
    locations: Optional[list[str]] = None
    device_types: Optional[list[str]] = None
    format: str = "pdf"  # pdf | excel | csv


@router.post("/generate")
async def generate_report_endpoint(
    data: ReportRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    name = REPORT_NAMES.get(data.report_type, 'Report')
    ts = datetime.utcnow().strftime('%Y%m%d-%H%M%S')

    try:
        common_args = dict(
            db=db,
            report_type=data.report_type,
            period=data.period,
            from_time=data.from_time,
            to_time=data.to_time,
            device_ids=data.device_ids,
            group_ids=data.group_ids,
            locations=data.locations,
            device_types=data.device_types,
        )

        if data.format == "excel":
            content = await generate_excel_report(**common_args)
            filename = f"ZenPlus-{name}-{ts}.xlsx"
            return Response(
                content=content,
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    "Content-Length": str(len(content)),
                },
            )

        elif data.format == "csv":
            content = await generate_csv_report(**common_args)
            filename = f"ZenPlus-{name}-{ts}.csv"
            return Response(
                content=content,
                media_type="text/csv",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    "Content-Length": str(len(content)),
                },
            )

        else:
            # Default: PDF
            pdf_bytes = await generate_report(**common_args)
            filename = f"ZenPlus-{name}-{ts}.pdf"
            return Response(
                content=bytes(pdf_bytes),
                media_type="application/pdf",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    "Content-Length": str(len(pdf_bytes)),
                },
            )

    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Report generation failed: {str(e)}")


@router.get("/types")
async def list_report_types(
    current_user: User = Depends(get_current_user),
):
    return {
        "report_types": [
            {
                "id": "executive_summary",
                "name": "Executive Summary",
                "description": "High-level overview with KPIs, health scores, and charts for management",
                "icon": "BarChart3",
            },
            {
                "id": "device_health",
                "name": "Device Health Report",
                "description": "Detailed per-device uptime, RTT analysis, and status history",
                "icon": "Monitor",
            },
            {
                "id": "service_health",
                "name": "Service Health Report",
                "description": "HTTP, TCP, and TLS service check analysis with response times",
                "icon": "ShieldCheck",
            },
            {
                "id": "alert_analysis",
                "name": "Alert Analysis",
                "description": "Alert trends, severity breakdown, MTTR, and top alerting devices",
                "icon": "Bell",
            },
            {
                "id": "full_report",
                "name": "Full Comprehensive Report",
                "description": "Complete report combining all sections — devices, services, and alerts",
                "icon": "FileText",
            },
        ],
        "periods": [
            {"id": "last_24h", "label": "Last 24 Hours"},
            {"id": "last_7d", "label": "Last 7 Days"},
            {"id": "last_30d", "label": "Last 30 Days"},
            {"id": "custom", "label": "Custom Range"},
        ],
    }
