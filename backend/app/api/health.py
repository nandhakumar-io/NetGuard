import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.schemas.health import ComponentStatus, PageHealth, SystemHealthReport
from app.services import health_service

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/detailed", response_model=SystemHealthReport)
async def detailed_health(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Full system health: database, Redis/Celery, and every optional
    external integration (Ollama, NetBox, GNS3, SMTP), plus a per-page
    rollup (see /health/pages). Backs the System Health page. This is
    deliberately separate from the unauthenticated GET /health liveness
    probe in app.main -- that one answers "is the process up" for load
    balancers in ~0ms; this one actually dials out to every dependency and
    can take a couple seconds.
    """
    components = await health_service.run_all_checks(db)
    pages = [PageHealth(**p) for p in health_service.build_page_health(components)]

    if any(c.status == ComponentStatus.DOWN and c.critical for c in components):
        overall = ComponentStatus.DOWN
    elif any(c.status in (ComponentStatus.DOWN, ComponentStatus.DEGRADED) for c in components):
        overall = ComponentStatus.DEGRADED
    else:
        overall = ComponentStatus.UP

    return SystemHealthReport(
        status=overall,
        checked_at=datetime.datetime.now(datetime.timezone.utc),
        components=components,
        pages=pages,
    )


@router.get("/pages", response_model=list[PageHealth])
async def page_health(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Just the per-page rollup, for a lighter-weight poll (e.g. a nav
    badge) than /health/detailed's full component list."""
    components = await health_service.run_all_checks(db)
    return [PageHealth(**p) for p in health_service.build_page_health(components)]