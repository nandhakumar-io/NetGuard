"""OUI (Organizationally Unique Identifier) vendor lookup service.

Normalizes MAC addresses into standard 6-hex-digit prefixes and matches them
against known network equipment and virtualization vendor OUIs.
"""
from __future__ import annotations

import re

# Common network equipment and infrastructure hardware/virtual OUIs
KNOWN_OUIS: dict[str, str] = {
    # Cisco Systems
    "00000C": "Cisco",
    "000142": "Cisco",
    "000143": "Cisco",
    "000163": "Cisco",
    "000164": "Cisco",
    "000196": "Cisco",
    "000197": "Cisco",
    "00024B": "Cisco",
    "00036B": "Cisco",
    "00044D": "Cisco",
    "00049A": "Cisco",
    "000531": "Cisco",
    "000532": "Cisco",
    "00055E": "Cisco",
    "000573": "Cisco",
    "000585": "Cisco",
    "00059A": "Cisco",
    "000628": "Cisco",
    "000652": "Cisco",
    "000653": "Cisco",
    "00070E": "Cisco",
    "000711": "Cisco",
    "00074F": "Cisco",
    "000750": "Cisco",
    "00077D": "Cisco",
    "000785": "Cisco",
    "0007B4": "Cisco",
    "0007EC": "Cisco",
    "000820": "Cisco",
    "000821": "Cisco",
    "00087C": "Cisco",
    "000883": "Cisco",
    "0008A3": "Cisco",
    "0008C7": "Cisco",
    "0008E2": "Cisco",
    "0008E3": "Cisco",
    "000911": "Cisco",
    "000912": "Cisco",
    "000943": "Cisco",
    "000944": "Cisco",
    "00097B": "Cisco",
    "00097C": "Cisco",
    "0009B6": "Cisco",
    "0009B7": "Cisco",
    "0009E8": "Cisco",
    "0009E9": "Cisco",

    # Juniper Networks
    "000B60": "Juniper",
    "000C86": "Juniper",
    "001011": "Juniper",
    "001DB5": "Juniper",
    "002159": "Juniper",
    "0024DC": "Juniper",
    "002688": "Juniper",
    "003004": "Juniper",
    "009069": "Juniper",
    "2C6B7D": "Juniper",
    "40B4CD": "Juniper",
    "44F477": "Juniper",
    "50C58D": "Juniper",
    "54E032": "Juniper",
    "64644B": "Juniper",
    "78FE3D": "Juniper",
    "84C1C1": "Juniper",
    "88E0F3": "Juniper",
    "AC4BCA": "Juniper",
    "CC885D": "Juniper",
    "D007CA": "Juniper",
    "E45D37": "Juniper",
    "F4B52F": "Juniper",
    "F8C001": "Juniper",

    # Arista Networks
    "001C73": "Arista",
    "005079": "Arista",
    "28993A": "Arista",
    "444C00": "Arista",
    "444C6E": "Arista",
    "706D15": "Arista",
    "7483EF": "Arista",
    "948E89": "Arista",
    "A0369F": "Arista",
    "B4A95A": "Arista",
    "D4E880": "Arista",

    # MikroTik
    "000C42": "MikroTik",
    "04D6AA": "MikroTik",
    "085531": "MikroTik",
    "18FD74": "MikroTik",
    "2C59E5": "MikroTik",
    "488F5A": "MikroTik",
    "4C5E0C": "MikroTik",
    "64D154": "MikroTik",
    "6C3B6B": "MikroTik",
    "744D28": "MikroTik",
    "789A18": "MikroTik",
    "B869F4": "MikroTik",
    "C4AD34": "MikroTik",
    "D40129": "MikroTik",
    "E81132": "MikroTik",

    # Fortinet
    "00090F": "Fortinet",
    "704C88": "Fortinet",
    "906CAC": "Fortinet",
    "A0E0AF": "Fortinet",

    # Virtualization / Hypervisors
    "000569": "VMware",
    "000C29": "VMware",
    "005056": "VMware",
    "00155D": "Hyper-V",
    "080027": "VirtualBox",
    "525400": "QEMU/KVM",

    # General Hardware Vendors
    "000325": "Aruba",
    "000B86": "Aruba",
    "001A1E": "Aruba",
    "000F61": "HP",
    "00110A": "HP",
}


def normalize_mac(mac_address: str) -> str | None:
    """Normalizes any MAC address string into 12 uppercase hex digits,
    or returns None if invalid."""
    if not mac_address:
        return None
    cleaned = re.sub(r"[^0-9A-Fa-f]", "", mac_address).upper()
    if len(cleaned) == 12:
        return cleaned
    return None


def lookup_oui(mac_address: str | None) -> str | None:
    """Looks up the vendor name for a given MAC address string."""
    if not mac_address:
        return None
    normalized = normalize_mac(mac_address)
    if not normalized:
        return None
    oui_prefix = normalized[:6]
    return KNOWN_OUIS.get(oui_prefix)
