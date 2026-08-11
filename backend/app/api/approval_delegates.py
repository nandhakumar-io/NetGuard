import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.approval_chain import ApprovalStageType
from app.models.user import User
from app.schemas.approval_delegate import ApprovalDelegateCreate, ApprovalDelegateRead
from app.services import approval_delegate_service

router = APIRouter(prefix="/approval-delegates", tags=["approval-delegates"])


def _to_read(db: Session, d) -> ApprovalDelegateRead:
    delegator = db.get(User, d.delegator_id)
    delegate = db.get(User, d.delegate_id)
    return ApprovalDelegateRead(
        id=d.id,
        delegator_id=d.delegator_id,
        delegator_name=delegator.full_name if delegator else None,
        delegate_id=d.delegate_id,
        delegate_name=delegate.full_name if delegate else None,
        stage_type=d.stage_type.value,
        starts_at=d.starts_at,
        ends_at=d.ends_at,
        active=d.active,
        reason=d.reason,
        created_at=d.created_at,
        revoked_at=d.revoked_at,
    )


@router.get("/delegated-to-me", response_model=list[ApprovalDelegateRead])
def list_delegated_to_me(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Delegations where *you* are the delegate -- i.e. authority
    someone else has handed to you. Used to decide whether to show
    approve/reject controls on a stage you wouldn't otherwise be
    eligible for by your own role.
    """
    delegates = (
        db.query(approval_delegate_service.ApprovalDelegate)
        .filter(approval_delegate_service.ApprovalDelegate.delegate_id == current_user.id)
        .order_by(approval_delegate_service.ApprovalDelegate.created_at.desc())
        .all()
    )
    return [_to_read(db, d) for d in delegates]


@router.get("", response_model=list[ApprovalDelegateRead])
def list_my_delegates(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Every delegation *you* have set up (active or revoked/expired --
    the UI can filter to `active` client-side). Only ever your own; there
    is no cross-user listing endpoint, since setting up someone else's
    backup approver is exactly what this feature must not allow.
    """
    delegates = approval_delegate_service.list_delegates_for_delegator(db, current_user.id)
    return [_to_read(db, d) for d in delegates]


@router.post("", response_model=ApprovalDelegateRead, status_code=201)
def create_delegate(
    payload: ApprovalDelegateCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
):
    """Sets up (or replaces) your backup approver for one stage type --
    e.g. a Manager naming another Manager to cover Manager Sign-off while
    they're on PTO. Only you may create a delegation for your own
    authority; the delegate must themselves hold an eligible role for
    that stage type.
    """
    try:
        stage_type = ApprovalStageType(payload.stage_type)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Unknown stage type '{payload.stage_type}'")

    delegate_user = db.get(User, payload.delegate_user_id)
    if not delegate_user:
        raise HTTPException(status_code=404, detail="Delegate user not found")

    from app.models.approval_chain import STAGE_ELIGIBLE_ROLES

    delegate_role = delegate_user.role.value if hasattr(delegate_user.role, "value") else delegate_user.role
    if delegate_role not in STAGE_ELIGIBLE_ROLES[stage_type]:
        raise HTTPException(
            status_code=422,
            detail=(
                f"'{delegate_user.full_name}' has role '{delegate_role}', which is not eligible for "
                f"'{stage_type.value}' -- choose someone who already holds an eligible role."
            ),
        )

    try:
        delegate = approval_delegate_service.create_delegate(
            db, current_user, delegate_user, stage_type, payload.starts_at, payload.ends_at, payload.reason
        )
    except approval_delegate_service.ApprovalDelegateError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return _to_read(db, delegate)


@router.delete("/{delegate_id}", response_model=ApprovalDelegateRead)
def revoke_delegate(
    delegate_id: uuid.UUID, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
):
    """Revokes one of your delegations early (back from PTO ahead of
    schedule, chose the wrong person, etc). Only the delegator who
    created it may revoke it.
    """
    try:
        delegate = approval_delegate_service.revoke_delegate(db, current_user, delegate_id)
    except approval_delegate_service.ApprovalDelegateError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return _to_read(db, delegate)


@router.get("/eligible-users/{stage_type}", response_model=list[dict])
def list_eligible_users(stage_type: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Users holding a role eligible for `stage_type` -- populates the
    "who do you want to delegate to" picker, excluding yourself.
    """
    try:
        st = ApprovalStageType(stage_type)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Unknown stage type '{stage_type}'")

    from app.models.approval_chain import STAGE_ELIGIBLE_ROLES

    eligible_roles = STAGE_ELIGIBLE_ROLES[st]
    users = db.query(User).filter(User.role.in_(eligible_roles), User.id != current_user.id).all()
    return [{"id": str(u.id), "full_name": u.full_name, "role": u.role.value if hasattr(u.role, "value") else u.role} for u in users]
