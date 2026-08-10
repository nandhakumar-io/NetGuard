
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.device import Device
from app.models.snapshot import ConfigSnapshot
from app.schemas.config_intent import (
    IntentKindInfo,
    RenderIntentAllVendorsResponse,
    RenderIntentRequest,
    RenderIntentResponse,
)
from app.services import (
    config_intent_service,
    credential_service,
    deployment_engine,
    snapshot_service,
)
from app.services.config_intent_service import (
    ConfigIntent,
    IntentKind,
    UnsupportedIntentError,
    Vendor,
)
from app.services.pipeline_service import DEVICE_TYPE_MAP

router = APIRouter(prefix="/config-intents", tags=["config-intents"])

_PARAM_SPECS: dict[IntentKind, tuple[list[str], list[str]]] = {
    IntentKind.ADD_VLAN: (["vlan_id", "name"], []),
    IntentKind.ADD_ACL_RULE: (
        ["acl_name", "action", "protocol", "source", "destination"], ["sequence"],
    ),
    IntentKind.SET_INTERFACE_DESCRIPTION: (["interface", "description"], []),
    IntentKind.SET_INTERFACE_ADMIN_STATE: (["interface", "enabled"], []),
    IntentKind.ADD_STATIC_ROUTE: (["prefix", "mask", "next_hop"], []),
    IntentKind.SET_NTP_SERVER: (["server_ip"], []),
}

_DESCRIPTIONS: dict[IntentKind, str] = {
    IntentKind.ADD_VLAN: "Create a VLAN with a given ID and name.",
    IntentKind.ADD_ACL_RULE: "Append a permit/deny rule to a named access list.",
    IntentKind.SET_INTERFACE_DESCRIPTION: "Set an interface's description.",
    IntentKind.SET_INTERFACE_ADMIN_STATE: "Bring an interface up or shut it down.",
    IntentKind.ADD_STATIC_ROUTE: "Add a static route.",
    IntentKind.SET_NTP_SERVER: "Add/set an NTP server.",
}


@router.get("/kinds", response_model=list[IntentKindInfo])
def list_intent_kinds(_=Depends(get_current_user)):
    """Every supported intent kind and the parameters it needs -- used to
    drive a dynamic "build a change once, deploy to any vendor" form in
    the UI instead of hardcoding one per platform.
    """
    return [
        IntentKindInfo(
            kind=kind.value,
            description=_DESCRIPTIONS[kind],
            required_params=required,
            optional_params=optional,
        )
        for kind, (required, optional) in _PARAM_SPECS.items()
    ]


def _resolve_vendor(db: Session, payload: RenderIntentRequest) -> tuple[Vendor, Device | None]:
    if payload.vendor:
        try:
            return Vendor(payload.vendor), None
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Unknown vendor '{payload.vendor}'")

    if payload.device_id is None:
        raise HTTPException(status_code=422, detail="Either 'vendor' or 'device_id' is required")

    device = db.get(Device, payload.device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    model_vendor = device.vendor.value if hasattr(device.vendor, "value") else device.vendor
    vendor = config_intent_service.DEVICE_MODEL_VENDOR_DEFAULT.get(model_vendor)
    if vendor is None:
        raise HTTPException(
            status_code=422,
            detail=f"Device '{device.hostname}' has no default intent vendor mapping for '{model_vendor}'.",
        )
    return vendor, device


def _current_config_for_device(device: Device, db: Session) -> tuple[str | None, str]:
    try:
        ssh_password = credential_service.get_ssh_password(device)
        netmiko_type = DEVICE_TYPE_MAP.get(
            device.vendor.value if hasattr(device.vendor, "value") else device.vendor, "cisco_ios"
        )
        current_config, _proto = deployment_engine.read_running_config(
            netmiko_type, device.ip_address, device.ssh_username or "admin", ssh_password
        )
        return current_config, "live"
    except credential_service.CredentialNotFoundError:
        pass
    except Exception:
        pass

    latest = (
        db.query(ConfigSnapshot)
        .filter(ConfigSnapshot.device_id == device.id)
        .order_by(ConfigSnapshot.created_at.desc())
        .first()
    )
    if latest is not None:
        return snapshot_service.decrypt_config(latest.running_config_encrypted), "last_snapshot"
    return None, "unavailable"


@router.post("/render", response_model=RenderIntentResponse)
def render_intent(
    payload: RenderIntentRequest, db: Session = Depends(get_db), current_user=Depends(get_current_user)
):
    """Renders one config intent into a real CLI snippet for the target
    vendor (given explicitly, or inferred from `device_id`'s inventory
    record). When `device_id` is given, also returns `proposed_config` --
    the device's current config with the snippet appended -- ready to
    hand straight to `POST /change-requests` as `proposed_config`, so an
    intent flows through the exact same risk-scoring / validation /
    approval pipeline as a hand-written change.
    """
    try:
        kind = IntentKind(payload.kind)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Unknown intent kind '{payload.kind}'")

    vendor, device = _resolve_vendor(db, payload)
    intent = ConfigIntent(kind=kind, params=payload.params)

    try:
        snippet = config_intent_service.render_intent(intent, vendor)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except UnsupportedIntentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    proposed_config = None
    current_source = None
    if device is not None:
        current_config, current_source = _current_config_for_device(device, db)
        proposed_config = config_intent_service.build_proposed_config(current_config, intent, vendor)

    return RenderIntentResponse(
        vendor=vendor.value,
        rendered_snippet=snippet,
        proposed_config=proposed_config,
        device_id=device.id if device else None,
        current_source=current_source,
    )


@router.post("/render-all-vendors", response_model=RenderIntentAllVendorsResponse)
def render_intent_all_vendors(payload: RenderIntentRequest, current_user=Depends(get_current_user)):
    """Renders one intent for every supported vendor at once -- the
    "here's what this looks like on IOS vs NX-OS vs Junos vs EOS"
    preview, useful when building a change request that will target a
    mixed-vendor group of devices.
    """
    try:
        kind = IntentKind(payload.kind)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Unknown intent kind '{payload.kind}'")

    intent = ConfigIntent(kind=kind, params=payload.params)
    return RenderIntentAllVendorsResponse(
        kind=kind.value, by_vendor=config_intent_service.render_intent_for_all_vendors(intent)
    )
