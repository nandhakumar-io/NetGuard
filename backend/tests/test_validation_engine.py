from app.services import validation_engine as ve


def test_rejects_empty_config():
    result = ve.validate_syntax("")
    assert result.passed is False
    assert "empty" in result.errors[0].lower()


def test_accepts_simple_valid_cisco_config():
    config = "interface Gi0/1\n ip address 10.2.2.1 255.255.255.0\n"
    result = ve.validate_syntax(config)
    assert result.passed is True
    assert result.errors == []


def test_rejects_unsupported_command():
    result = ve.validate_syntax("frobnicate the-widget\n")
    assert result.passed is False
    assert "unrecognized or unsupported command" in result.errors[0].lower()


def test_rejects_malformed_interface_declaration():
    result = ve.validate_syntax("interface\n")
    assert result.passed is False
    assert any("malformed interface" in e.lower() for e in result.errors)


def test_placeholder_token_rejected():
    result = ve.validate_syntax("interface Gi0/1\n TODO finish this\n")
    assert result.passed is False
    assert any("placeholder" in e.lower() for e in result.errors)


# --- VLAN cross-checks -----------------------------------------------------
def test_rejects_reference_to_undefined_vlan():
    config = "interface Gi0/2\n switchport access vlan 99\n"
    result = ve.validate_syntax(config, current_config="vlan 10\n name DATA\n")
    assert result.passed is False
    assert any("vlan 99" in e.lower() for e in result.errors)


def test_accepts_vlan_defined_in_same_change():
    config = "vlan 99\ninterface Gi0/2\n switchport access vlan 99\n"
    result = ve.validate_syntax(config)
    assert result.passed is True


def test_accepts_vlan_defined_in_current_config():
    config = "interface Gi0/2\n switchport access vlan 10\n"
    result = ve.validate_syntax(config, current_config="vlan 10\n name DATA\n")
    assert result.passed is True


def test_trunk_vlan_range_cross_check():
    config = "interface Gi0/1\n switchport trunk allowed vlan 10-12\n"
    result = ve.validate_syntax(config, current_config="vlan 10\nvlan 11\n")
    assert result.passed is False
    assert any("vlan 12" in e.lower() for e in result.errors)


# --- ACL cross-checks -------------------------------------------------------
def test_rejects_broken_acl_reference():
    config = "interface Gi0/3\n ip access-group BLOCK_TELNET in\n"
    result = ve.validate_syntax(config, current_config="")
    assert result.passed is False
    assert any("acl 'block_telnet'" in e.lower() for e in result.errors)


def test_accepts_acl_defined_in_same_change():
    config = "ip access-list extended BLOCK_TELNET\ninterface Gi0/3\n ip access-group BLOCK_TELNET in\n"
    result = ve.validate_syntax(config)
    assert result.passed is True


def test_accepts_acl_already_on_device():
    config = "interface Gi0/3\n ip access-group 101 in\n"
    result = ve.validate_syntax(config, current_config="access-list 101 deny ip any any\n")
    assert result.passed is True


# --- Gateway cross-checks ----------------------------------------------------
def test_rejects_gateway_outside_known_subnets():
    current = "interface Gi0/1\n ip address 10.1.1.1 255.255.255.0\n"
    result = ve.validate_syntax("ip default-gateway 192.168.99.1\n", current_config=current)
    assert result.passed is False
    assert any("gateway conflict" in e.lower() for e in result.errors)


def test_accepts_gateway_within_known_subnet():
    current = "interface Gi0/1\n ip address 10.1.1.1 255.255.255.0\n"
    result = ve.validate_syntax("ip default-gateway 10.1.1.254\n", current_config=current)
    assert result.passed is True


def test_gateway_check_skipped_gracefully_without_inventory():
    result = ve.validate_syntax("ip default-gateway 10.1.1.254\n", current_config=None)
    assert result.passed is True
    assert any("no current device configuration" in w.lower() for w in result.warnings)


# --- Vendor dispatch ---------------------------------------------------------
def test_juniper_accepts_set_style_commands():
    config = "set interfaces ge-0/0/0 unit 0 family inet address 10.1.1.1/24\n"
    result = ve.validate_syntax(config, vendor="juniper")
    assert result.passed is True


def test_juniper_rejects_non_set_style_commands():
    result = ve.validate_syntax("bogus statement here\n", vendor="juniper")
    assert result.passed is False


def test_linux_skips_ios_allowlist_and_cross_checks():
    result = ve.validate_syntax("systemctl restart networking\n", vendor="linux")
    assert result.passed is True