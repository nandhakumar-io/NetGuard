"""Seeds a small set of common, category-matched AlertRunbook rows on
first startup -- so a fresh install isn't staring at an empty "Alert
Runbooks" page with none of the well-known, safe-to-automate playbooks
(clear ARP cache, clear a full MAC table, ...) already wired up, the
same way NetGuard's config-compliance module ships pre-seeded
CIS/vendor-hardening PolicyRules instead of an empty rule set.

Only the two genuinely device-agnostic, non-disruptive remediations
below (clear ARP cache, clear a dynamic MAC table) are seeded with
remediation_enabled=True -- both are safe to push blind to any device
regardless of its interface names or running config, unlike e.g. an
interface bounce, which needs a specific interface name and would be
actively wrong if seeded generically. Everything else is seeded
doc-only (no remediation_command) so it still shows up as a runbook
link on matching alerts without risking a device write nobody
reviewed.

Called once from app.main.lifespan on every startup. Idempotent: skips
any category(+source) pair that already has a row, whether that row
came from a previous run of this seed or was hand-created/edited by an
admin -- so an admin's edits or deletions are never overwritten, and
this is safe to run on every restart.
"""
import logging

from sqlalchemy.orm import Session

from app.models.alert_runbook import AlertRunbook, RemediationActionType

logger = logging.getLogger(__name__)

_DEFAULT_RUNBOOKS = [
    {
        "category": "ARP Table Stale",
        "source": None,
        "title": "Stale/incorrect ARP entries — clear the ARP cache",
        "url": "https://wiki.internal/runbooks/clear-arp-cache",
        "notes": (
            "Covers stale ARP entries, IP conflicts, and \"can ping the "
            "device but not the hosts behind it\" symptoms. Clearing the "
            "cache is non-disruptive -- it just forces re-resolution on "
            "next traffic."
        ),
        "remediation_enabled": True,
        "remediation_action_type": RemediationActionType.RESTART_SERVICE,
        "remediation_label": "Clear ARP cache",
        "remediation_command": "clear arp-cache",
        "remediation_required_role": None,
    },
    {
        "category": "MAC Table Full",
        "source": None,
        "title": "MAC address table full/overflowing — clear learned entries",
        "url": "https://wiki.internal/runbooks/clear-mac-table",
        "notes": (
            "A full CAM table causes unknown-unicast flooding across the "
            "whole VLAN. Clearing dynamic entries is non-disruptive -- "
            "they're immediately relearned from live traffic."
        ),
        "remediation_enabled": True,
        "remediation_action_type": RemediationActionType.RESTART_SERVICE,
        "remediation_label": "Clear MAC address table",
        "remediation_command": "clear mac address-table dynamic",
        "remediation_required_role": None,
    },
    {
        "category": "Interface Down",
        "source": None,
        "title": "Interface Down — triage steps",
        "url": "https://wiki.internal/runbooks/interface-down",
        "notes": (
            "Doc-only by design: bouncing an interface needs the specific "
            "interface name, which isn't safe to guess generically. Add a "
            "second, more specific runbook mapping (e.g. scoped to a "
            "source) with its own remediation_command if you want a "
            "one-click bounce for a particular device/interface pattern."
        ),
        "remediation_enabled": False,
        "remediation_action_type": None,
        "remediation_label": None,
        "remediation_command": None,
        "remediation_required_role": None,
    },
    {
        "category": "High CPU",
        "source": None,
        "title": "Sustained high CPU — triage steps",
        "url": "https://wiki.internal/runbooks/high-cpu",
        "notes": "Check top processes before restarting anything -- a restart without knowing the cause just hides a recurring problem.",
        "remediation_enabled": False,
        "remediation_action_type": None,
        "remediation_label": None,
        "remediation_command": None,
        "remediation_required_role": None,
    },
    {
        "category": "Device Unreachable",
        "source": None,
        "title": "Device Unreachable — triage steps",
        "url": "https://wiki.internal/runbooks/device-unreachable",
        "notes": None,
        "remediation_enabled": False,
        "remediation_action_type": None,
        "remediation_label": None,
        "remediation_command": None,
        "remediation_required_role": None,
    },
    {
        "category": "Configuration Drift Detected",
        "source": None,
        "title": "Configuration drift — review and reconcile",
        "url": "https://wiki.internal/runbooks/config-drift",
        "notes": "Use the device's \"What Changed\" panel to review the diff before deciding whether to reconcile to golden or accept the drift as an intentional change.",
        "remediation_enabled": False,
        "remediation_action_type": None,
        "remediation_label": None,
        "remediation_command": None,
        "remediation_required_role": None,
    },
]


def seed_default_runbooks(db: Session) -> int:
    """Inserts any of the above rows whose category(+source) isn't
    already present. Returns the number of rows actually inserted.
    Never raises -- a seeding failure should never block app startup,
    same policy as the rest of app.main.lifespan's best-effort setup
    steps.
    """
    inserted = 0
    try:
        existing = {
            (r.category.lower(), r.source) for r in db.query(AlertRunbook.category, AlertRunbook.source).all()
        }
        for defaults in _DEFAULT_RUNBOOKS:
            key = (defaults["category"].lower(), defaults["source"])
            if key in existing:
                continue
            db.add(AlertRunbook(created_by="system", **defaults))
            existing.add(key)
            inserted += 1
        if inserted:
            db.commit()
            logger.info("Seeded %d default alert runbook(s)", inserted)
    except Exception:
        logger.exception("Failed to seed default alert runbooks (non-fatal)")
        db.rollback()
    return inserted
