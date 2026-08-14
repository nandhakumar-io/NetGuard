"""Builds and advances a ChangeRequest's approval chain
(app.models.approval_chain), and is the source of truth for whether a
change request may actually be enqueued for deployment.

This sits *alongside* the existing single/dual Network Administrator
approval gate in app.api.change_requests.approve_change_request, not in
place of it: a change with no chain (the common, low/medium-risk case)
behaves exactly as before -- one or two Network Administrator approvals,
same as today. A change that gets a chain must clear every stage in
order; the *last* stage is always ADMIN_APPROVAL, which is what finally
flips the ChangeRequest to APPROVED and enqueues deployment -- so
"approve" always ends the same way, it just may now require sign-off
from other roles first.
"""
import dataclasses
import uuid

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.approval_chain import (
    STAGE_ELIGIBLE_ROLES,
    ApprovalStageStatus,
    ApprovalStageType,
    ChangeRequestApprovalStage,
)
from app.models.change_request import ChangeRequest
from app.models.user import User


class ApprovalChainError(Exception):
    """Raised when an action against a chain isn't allowed (wrong role,
    wrong stage, chain already resolved, etc). The API layer maps this
    to a 403/409 as appropriate."""


@dataclasses.dataclass
class ChainDecision:
    stages: list[ApprovalStageType]
    reason: str | None


def decide_chain(risk_classification: str | None, requires_dual_approval: bool, dual_approval_reason: str | None) -> ChainDecision:
    """Decides which stages a change request needs, beyond the baseline
    single Network Administrator approval every change already requires.

    - Critical Risk, or high blast-radius (both already computed by
      app.api.change_requests._dual_approval and passed in here): full
      chain -- Peer Review -> Manager Sign-off -> Admin Approval. This is
      the PCI-DSS/SOX "two-person integrity" case: a distinct technical
      reviewer AND a distinct business sign-off, not just the same role
      approving twice.
    - Everything else: no chain at all (empty list) -- the existing
      single-approval path in approve_change_request is unchanged.

    Feature-flagged via settings.APPROVAL_CHAIN_ENABLED so a deployment
    that isn't ready to onboard Manager-role users yet can disable this
    without code changes and fall back to plain dual-approval.
    """
    if not settings.APPROVAL_CHAIN_ENABLED:
        return ChainDecision(stages=[], reason=None)

    if not requires_dual_approval:
        return ChainDecision(stages=[], reason=None)

    return ChainDecision(
        stages=[ApprovalStageType.PEER_REVIEW, ApprovalStageType.MANAGER_SIGNOFF, ApprovalStageType.ADMIN_APPROVAL],
        reason=dual_approval_reason,
    )


def build_chain(db: Session, cr: ChangeRequest, decision: ChainDecision) -> list[ChangeRequestApprovalStage]:
    """Creates the stage rows for `cr`. Call once, at submission (and
    again at rescore, after clearing any prior chain -- see
    reset_chain), never after the chain has started being acted on."""
    stages = []
    for i, stage_type in enumerate(decision.stages, start=1):
        stage = ChangeRequestApprovalStage(
            change_request_id=cr.id,
            sequence=i,
            stage_type=stage_type,
            required_role=STAGE_ELIGIBLE_ROLES[stage_type][0]
            if len(STAGE_ELIGIBLE_ROLES[stage_type]) == 1
            else "/".join(STAGE_ELIGIBLE_ROLES[stage_type]),
            status=ApprovalStageStatus.PENDING,
        )
        db.add(stage)
        stages.append(stage)
    if stages:
        db.flush()
    return stages


def reset_chain(db: Session, cr: ChangeRequest) -> None:
    """Discards any existing (not-yet-resolved) stages for `cr` -- used
    when a change is rescored and its dual-approval requirement/reason
    changes, so a stale chain built for the old risk classification
    can't linger. Only ever called while the CR is still
    PENDING_APPROVAL, before anyone has acted on a stage."""
    db.query(ChangeRequestApprovalStage).filter(
        ChangeRequestApprovalStage.change_request_id == cr.id
    ).delete(synchronize_session=False)


def get_chain(db: Session, cr_id: uuid.UUID) -> list[ChangeRequestApprovalStage]:
    return (
        db.query(ChangeRequestApprovalStage)
        .filter(ChangeRequestApprovalStage.change_request_id == cr_id)
        .order_by(ChangeRequestApprovalStage.sequence)
        .all()
    )


def current_stage(db: Session, cr_id: uuid.UUID) -> ChangeRequestApprovalStage | None:
    """The next stage awaiting action, or None if there's no chain at
    all, or every stage is already resolved."""
    return (
        db.query(ChangeRequestApprovalStage)
        .filter(
            ChangeRequestApprovalStage.change_request_id == cr_id,
            ChangeRequestApprovalStage.status == ApprovalStageStatus.PENDING,
        )
        .order_by(ChangeRequestApprovalStage.sequence)
        .first()
    )


def is_chain_fully_approved(db: Session, cr_id: uuid.UUID) -> bool:
    stages = get_chain(db, cr_id)
    return bool(stages) and all(s.status == ApprovalStageStatus.APPROVED for s in stages)


def act_on_current_stage(
    db: Session, cr: ChangeRequest, actor: User, approve: bool, notes: str | None = None
) -> ChangeRequestApprovalStage:
    """Records `actor`'s decision on whatever stage is currently
    pending for `cr`. Enforces, in order:
      1. There is a pending stage at all (chain not exhausted/rejected).
      2. `actor`'s role is eligible for *that* stage type.
      3. Segregation of duties: the actor can't be the person who
         submitted the change request (an author can't peer-review or
         sign off on their own change), and can't be someone who already
         acted on an earlier stage of the same chain.
    Advancing past this stage (or rejecting the whole chain) is the
    caller's responsibility based on the returned stage's `.status`.
    """
    stage = current_stage(db, cr.id)
    if stage is None:
        raise ApprovalChainError("This change request has no pending approval stage.")

    eligible_roles = STAGE_ELIGIBLE_ROLES[stage.stage_type]
    actor_role = actor.role.value if hasattr(actor.role, "value") else actor.role

    acting_for: User | None = None
    if actor_role not in eligible_roles:
        # Not eligible under their own base role -- check whether they're
        # currently acting as someone else's approval delegate for this
        # exact stage type (app.services.approval_delegate_service). This
        # was previously never consulted here, which made delegation a
        # dead feature: a delegate would still be rejected with "requires
        # role X; you are Y" even with an active delegation in place.
        from app.services import approval_delegate_service

        acting_for = approval_delegate_service.resolve_delegated_authority(db, actor, stage.stage_type)
        if acting_for is None:
            raise ApprovalChainError(
                f"This stage ({stage.stage_type.value}) requires role "
                f"{'/'.join(eligible_roles)}; you are '{actor_role}'."
            )

    if actor.id == cr.submitted_by or (acting_for is not None and acting_for.id == cr.submitted_by):
        raise ApprovalChainError(
            "The person who submitted a change request cannot act on its own approval chain."
        )

    already_acted = (
        db.query(ChangeRequestApprovalStage)
        .filter(
            ChangeRequestApprovalStage.change_request_id == cr.id,
            ChangeRequestApprovalStage.acted_by.in_(
                [actor.id, acting_for.id] if acting_for is not None else [actor.id]
            ),
        )
        .first()
    )
    if already_acted is not None:
        raise ApprovalChainError(
            "You have already acted on an earlier stage of this change request's approval chain; "
            "a later stage must be completed by someone else (segregation of duties)."
        )

    stage.status = ApprovalStageStatus.APPROVED if approve else ApprovalStageStatus.REJECTED
    stage.acted_by = actor.id
    stage.notes = notes
    from datetime import datetime, timezone

    stage.acted_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(stage)
    return stage
