from app.services.oui_lookup import lookup_oui, normalize_mac


def test_normalize_mac():
    assert normalize_mac("00:1A:2B:3C:4D:5E") == "001A2B3C4D5E"
    assert normalize_mac("00-1a-2b-3c-4d-5e") == "001A2B3C4D5E"
    assert normalize_mac("001a.2b3c.4d5e") == "001A2B3C4D5E"
    assert normalize_mac("invalid") is None
    assert normalize_mac("") is None


def test_lookup_oui():
    assert lookup_oui("00:00:0C:11:22:33") == "Cisco"
    assert lookup_oui("00:05:85:AA:BB:CC") == "Juniper" or lookup_oui("00:05:85:AA:BB:CC") == "Cisco"
    assert lookup_oui("00:1C:73:00:11:22") == "Arista"
    assert lookup_oui("00:05:5E:12:34:56") == "Cisco"
    assert lookup_oui("00:50:56:12:34:56") == "VMware" or lookup_oui("00:50:56:12:34:56") == "Fortinet"
    assert lookup_oui("99:99:99:99:99:99") is None
    assert lookup_oui(None) is None
