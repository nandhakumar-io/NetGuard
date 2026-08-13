"""Resolves which runbook (if any) applies to a given alert.

Lookup is category-first (case-insensitive), preferring a source-specific
mapping over a source-agnostic one:

  1. exact category + matching source
  2. exact category + source IS NULL (applies to any source)
"""
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.alert import Alert
from app.models.alert_runbook import AlertRunbook


def resolve_runbook(db: Session, category: str, source: str) -> AlertRunbook | None:
    if not category:
        return None
    matches = (
        db.query(AlertRunbook)
        .filter(func.lower(AlertRunbook.category) == category.lower())
        .filter((AlertRunbook.source == source) | (AlertRunbook.source.is_(None)))
        .all()
    )
    if not matches:
        return None
    # Prefer the source-specific mapping when both exist.
    for m in matches:
        if m.source == source:
            return m
    return matches[0]


def resolve_runbook_map(db: Session, alerts: list[Alert]) -> dict:
    """Batch version -- one query for a page of alerts instead of N.

    Returns {alert_id: AlertRunbook}. Only alerts with a resolved runbook
    appear in the dict.
    """
    categories = {a.category.lower() for a in alerts if a.category}
    if not categories:
        return {}
    rows = (
        db.query(AlertRunbook)
        .filter(func.lower(AlertRunbook.category).in_(categories))
        .all()
    )
    if not rows:
        return {}

    by_category: dict[str, list[AlertRunbook]] = {}
    for r in rows:
        by_category.setdefault(r.category.lower(), []).append(r)

    result = {}
    for alert in alerts:
        if not alert.category:
            continue
        candidates = by_category.get(alert.category.lower())
        if not candidates:
            continue
        chosen = next((c for c in candidates if c.source == alert.source), None)
        chosen = chosen or next((c for c in candidates if c.source is None), None)
        if chosen:
            result[alert.id] = chosen
    return result
