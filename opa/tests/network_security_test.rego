package netguard.network_security_test

import rego.v1

import data.netguard.network_security

base_input := {
	"configuration": {"proposed": "interface GigabitEthernet0/1\n description uplink\n"},
	"device": {"role": "access"},
}

test_safe_config_no_violations if {
	count(network_security.violation) == 0 with input as base_input
}

test_telnet_denied if {
	inp := object.union(base_input, {"configuration": {"proposed": "line vty 0 4\n transport input telnet\n"}})
	some v in network_security.violation with input as inp
	v.policy == "network_security.no_telnet"
	v.severity == "critical"
}

test_weak_credential_denied if {
	inp := object.union(base_input, {"configuration": {"proposed": "username admin password cisco\n"}})
	some v in network_security.violation with input as inp
	v.policy == "network_security.no_weak_credentials"
}
