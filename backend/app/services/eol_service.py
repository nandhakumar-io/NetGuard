"""Firmware / hardware End-of-Life (EOL) and End-of-Support (EOS) tracking.

app.models.device.Device.os_version (and .model / .platform) is already
captured by every discovery/backup/facts-gathering path in this app, but
nothing does anything with it beyond display. Security/audit teams
specifically want to know "how many devices are running software past
its vendor-published support date" -- this module answers that from a
static lookup table, no external service dependency required.

This is deliberately NOT a live feed from each vendor's EOL API/page --
those require per-vendor scraping or a paid data source (e.g. Cisco's EoX
API needs a support contract + OAuth app registration), which is out of
scope for a first pass. The table below is a a small, manually-curated
set of well-known EOL/EOS milestones for the platforms this app already
targets (Cisco IOS/IOS-XE, Juniper Junos, Arista EOS); an operator can
extend EOL_DATABASE with entries specific to their fleet without touching
any other code, and the matching logic is written to degrade gracefully
(a device whose model/version isn't in the table is reported as
"unknown", never as false-negative "supported").
"""
import dataclasses
import datetime
import re


# Each entry matches a (vendor, model-prefix) or (vendor, os-version-prefix)
# pair against a device. `eos_date` = End of Support (vendor stops selling
# support contracts / issuing new fixes); `eol_date` = End of Life (vendor
# stops shipping the platform at all -- informational, EOS is what
# actually matters operationally). Dates are illustrative of real,
# published vendor milestones for widely-deployed platforms; an operator
# should verify/update against their vendor's current EoX bulletins before
# relying on this for a compliance audit.
@dataclasses.dataclass(frozen=True)
class EolEntry:
    vendor: str
    match_field: str  # "model" or "os_version"
    match_prefix: str  # case-insensitive startswith match
    platform_label: str
    eos_date: datetime.date
    eol_date: datetime.date | None
    note: str = ""
    # The version an operator should be planning to move this platform
    # to -- independent of whether it's EOS/EOL yet. A device can be
    # fully supported today and still be "behind" (e.g. three feature
    # releases back), which is exactly the case "already past support"
    # doesn't capture: by the time something shows up as EOS, there's no
    # more runway to plan the migration. None means no house-recommended
    # target has been curated for this platform yet.
    recommended_target_version: str | None = None


EOL_DATABASE: list[EolEntry] = [
    # --- Cisco ---
    EolEntry("cisco", "model", "2960", "Catalyst 2960 series", datetime.date(2023, 10, 31), datetime.date(2024, 10, 31),
             "Replaced by Catalyst 9200 series.", recommended_target_version="17.9"),
    EolEntry("cisco", "model", "3750", "Catalyst 3750 series", datetime.date(2019, 1, 31), datetime.date(2024, 1, 31),
             recommended_target_version="17.9"),
    EolEntry("cisco", "model", "3850", "Catalyst 3850 series", datetime.date(2024, 10, 31), None,
             "Replaced by Catalyst 9300 series.", recommended_target_version="17.9"),
    EolEntry("cisco", "model", "ISR4", "ISR 4000 series", datetime.date(2027, 10, 31), None,
             recommended_target_version="17.9"),
    EolEntry("cisco", "os_version", "12.", "IOS 12.x", datetime.date(2016, 1, 1), datetime.date(2018, 1, 1),
             "IOS 12.x has been end-of-support fleet-wide for years; upgrade to IOS-XE.",
             recommended_target_version="17.9"),
    EolEntry("cisco", "os_version", "15.0", "IOS 15.0", datetime.date(2019, 7, 31), datetime.date(2020, 7, 31),
             recommended_target_version="17.9"),
    # --- Juniper ---
    EolEntry("juniper", "model", "EX2200", "EX2200 series", datetime.date(2021, 10, 27), datetime.date(2022, 10, 27),
             "Replaced by EX2300 series.", recommended_target_version="21.4"),
    EolEntry("juniper", "model", "EX4200", "EX4200 series", datetime.date(2020, 5, 31), datetime.date(2021, 5, 31),
             recommended_target_version="21.4"),
    EolEntry("juniper", "model", "SRX100", "SRX100 series", datetime.date(2018, 10, 30), datetime.date(2019, 10, 30),
             recommended_target_version="21.4"),
    EolEntry("juniper", "os_version", "12.1", "Junos 12.1", datetime.date(2017, 3, 24), datetime.date(2018, 3, 24),
             recommended_target_version="21.4"),
    # --- Arista ---
    EolEntry("arista", "model", "7050", "7050 series", datetime.date(2025, 12, 31), None,
             recommended_target_version="4.31"),
]


@dataclasses.dataclass
class EolStatus:
    is_eol: bool
    is_eos: bool
    matched: bool
    platform_label: str | None = None
    eos_date: datetime.date | None = None
    eol_date: datetime.date | None = None
    note: str | None = None
    days_since_eos: int | None = None
    # "What should this device be running" and "is it there yet" --
    # deliberately independent of is_eos/is_eol above. A device can be
    # matched, fully supported, and still behind its recommended target;
    # that's the case an "already unsupported" view can't surface until
    # it's too late to plan around.
    recommended_target_version: str | None = None
    needs_upgrade: bool = False


def _normalize(value: str | None) -> str:
    return re.sub(r"\s+", "", (value or "")).lower()


def _version_key(value: str | None) -> tuple[int, ...]:
    """Best-effort numeric ordering for version strings like "17.9",
    "15.2(4)S1", "21.4R3". Pulls out the numeric groups in order and
    compares them as a tuple, which handles the common
    "<major>.<minor>[.<patch>]" shape across all three vendors here
    without needing a vendor-specific parser. Returns () when nothing
    numeric could be extracted, which callers treat as "unorderable."
    """
    return tuple(int(n) for n in re.findall(r"\d+", value or ""))


def _is_behind_target(current: str | None, target: str | None) -> bool:
    """True if `current` should be considered behind `target`. Falls back
    to a plain string-inequality check when either side doesn't parse
    into a comparable numeric version -- still useful (flags "not on the
    recommended build") even though it can't distinguish ahead/behind in
    that case.
    """
    if not target:
        return False
    if not current:
        return True
    cur_key, target_key = _version_key(current), _version_key(target)
    if cur_key and target_key:
        return cur_key < target_key
    return _normalize(current) != _normalize(target)


def check_device_eol(vendor: str, model: str | None, os_version: str | None, today: datetime.date | None = None) -> EolStatus:
    """Matches a device against EOL_DATABASE by model first (more
    specific -- a model uniquely identifies hardware regardless of the
    software version currently loaded on it), falling back to os_version
    prefix match. Returns matched=False (not a false "supported") for
    anything not in the table, since a static table can never be
    complete and silently reporting "ok" for an unknown platform would
    be actively misleading for an audit.
    """
    today = today or datetime.date.today()
    vendor_norm = (vendor or "").lower()
    model_norm = _normalize(model)
    version_norm = _normalize(os_version)

    candidates = [e for e in EOL_DATABASE if e.vendor == vendor_norm]

    match: EolEntry | None = None
    if model_norm:
        match = next((e for e in candidates if e.match_field == "model" and _normalize(e.match_prefix) in model_norm), None)
    if match is None and version_norm:
        match = next((e for e in candidates if e.match_field == "os_version" and version_norm.startswith(_normalize(e.match_prefix))), None)

    if match is None:
        return EolStatus(is_eol=False, is_eos=False, matched=False)

    is_eos = today >= match.eos_date
    is_eol = bool(match.eol_date and today >= match.eol_date)
    needs_upgrade = _is_behind_target(os_version, match.recommended_target_version)
    return EolStatus(
        is_eol=is_eol,
        is_eos=is_eos,
        matched=True,
        platform_label=match.platform_label,
        eos_date=match.eos_date,
        eol_date=match.eol_date,
        note=match.note or None,
        days_since_eos=(today - match.eos_date).days if is_eos else None,
        recommended_target_version=match.recommended_target_version,
        needs_upgrade=needs_upgrade,
    )
