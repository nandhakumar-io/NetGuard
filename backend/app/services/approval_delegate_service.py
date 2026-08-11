"""Create, revoke, and resolve Approval Delegates.

See app.models.approval_delegate for the "why" -- this module is the
"how": creating a delegation (delegator-only, one active delegate per
stage type at a time), and resolving whether a given user is currently
allowed to act for someone else on a given stage type, which
approval_chain_service.act_on_current_stage consults alongside the
normal STAGE_ELIGIBLE_ROLES check.
"""
import datetime
import uuid

from sqlalchemy.orm import Session

from app.models.approval_chain import ApprovalStageType
from app.models.approval_delegate import ApprovalDelegate
from app.models.user import User


class ApprovalDelegateError(Exception):
    """Raised for invalid delegate setup (self-delegation, ineligible
    delegate role, delegator not eligible for the stage type in the
    first place, etc). API layer maps this to a 422/409."""


def _is_active(delegate: ApprovalDelegate, at: datetime.datetime) -> bool:
    if not delegate.active:
        return False
    if delegate.starts_at and at < delegate.starts_at:
        return False
    if delegate.ends_at and at > delegate.ends_at:
        return False
    return True


def create_delegate(
    db: Session,
    delegator: User,
    delegate_user: User,
    stage_type: ApprovalStageType,
    starts_at: datetime.datetime | None,
    ends_at: datetime.datetime | None,
    reason: str | None,
) -> ApprovalDelegate:
    from app.models.approval_chain import STAGE_ELIGIBLE_ROLES

    if delegator.id == delegate_user.id:
        raise ApprovalDelegateError("You cannot delegate approval authority to yourself.")

    delegator_role = delegator.role.value if hasattr(delegator.role, "value") else delegator.role
    eligible_roles = STAGE_ELIGIBLE_ROLES[stage_type]
    if delegator_role not in eligible_roles:
        raise ApprovalDelegateError(
            f"Your role ('{delegator_role}') is not itself eligible for "
            f"'{stage_type.value}', so there is no authority for you to delegate."
        )

    if ends_at and starts_at and ends_at <= starts_at:
        raise ApprovalDelegateError("'ends_at' must be after 'starts_at'.")

    # One active delegate per (delegator, stage_type) at a time -- avoids
    # an ambiguous "who's actually covering for me" state. Setting up a
    # new one automatically supersedes (deactivates) any prior active
    # delegation for the same delegator/stage_type, rather than erroring,
    # so an admin correcting a mistaken delegate doesn't have to
    # remember to revoke the old one first.
    existing = (
        db.query(ApprovalDelegate)
        .filter(
            ApprovalDelegate.delegator_id == delegator.id,
            ApprovalDelegate.stage_type == stage_type,
            ApprovalDelegate.active == True,  # noqa: E712
        )
        .all()
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    for old in existing:
        old.active = False
        old.revoked_at = now

    delegate = ApprovalDelegate(
        delegator_id=delegator.id,
        delegate_id=delegate_user.id,
        stage_type=stage_type,
        starts_at=starts_at,
        ends_at=ends_at,
        reason=reason,
        active=True,
    )
    db.add(delegate)
    db.commit()
    db.refresh(delegate)
    return delegate


def revoke_delegate(db: Session, delegator: User, delegate_id: uuid.UUID) -> ApprovalDelegate:
    delegate = db.get(ApprovalDelegate, delegate_id)
    if delegate is None or delegate.delegator_id != delegator.id:
        raise ApprovalDelegateError("Delegation not found, or you are not its delegator.")
    delegate.active = False
    delegate.revoked_at = datetime.datetime.now(datetime.timezone.utc)
    db.commit()
    db.refresh(delegate)
    return delegate


def list_delegates_for_delegator(db: Session, delegator_id: uuid.UUID) -> list[ApprovalDelegate]:
    return (
        db.query(ApprovalDelegate)
        .filter(ApprovalDelegate.delegator_id == delegator_id)
        .order_by(ApprovalDelegate.created_at.desc())
        .all()
    )


def resolve_delegated_authority(db: Session, actor: User, stage_type: ApprovalStageType) -> User | None:
    """If `actor` is currently an active delegate for someone on
    `stage_type`, returns the delegator whose authority they're acting
    under (the *first* one found, if for some reason there were ever
    more than one -- shouldn't happen given the one-active-at-a-time
    rule in create_delegate, but resolution shouldn't hard-fail on data
    that predates a rule change). Returns None if `actor` isn't
    currently anyone's active delegate for this stage type.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    candidates = (
        db.query(ApprovalDelegate)
        .filter(ApprovalDelegate.delegate_id == actor.id, ApprovalDelegate.stage_type == stage_type)
        .all()
    )
    for d in candidates:
        if _is_active(d, now):
            return db.get(User, d.delegator_id)
    return None
