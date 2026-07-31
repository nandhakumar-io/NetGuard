import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.services import compliance_report

router = APIRouter(prefix="/reports", tags=["reports"])

_MEDIA_TYPES = {
    "csv": "text/csv",
    "pdf": "application/pdf",
}


@router.get("/compliance")
def get_compliance_report(
    format: str = Query("pdf", pattern="^(pdf|csv)$"),
    days: int = Query(30, ge=1, le=365, description="Reporting window in days"),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Fleet-wide compliance report (drift compliance scores, open drift
    counts, and AI Configuration Analyzer risk stats per device over the
    given window), rendered as a downloadable PDF or CSV.

    Available to any authenticated user (read-only, no device/config
    content is included) -- consistent with the other dashboard/summary
    endpoints in this API.
    """
    report = compliance_report.build_report(db, window_days=days)

    if format == "csv":
        body = compliance_report.render_csv(report)
    else:
        try:
            body = compliance_report.render_pdf(report)
        except ImportError:
            raise HTTPException(
                status_code=503,
                detail="PDF generation is unavailable: the 'reportlab' package is not installed on this server. "
                "Use format=csv instead, or install reportlab.",
            )

    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")
    filename = f"netguard-compliance-report-{timestamp}.{format}"
    return Response(
        content=body,
        media_type=_MEDIA_TYPES[format],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )