package netguard.network_security

import rego.v1

# Each rule contributes to `violations` (deny/review) via the aggregating
# rules in policy.rego. This file only defines the raw checks.

proposed := lower(input.configuration.proposed)

violation contains v if {
	contains(proposed, "transport input telnet")
	v := {
		"policy": "network_security.no_telnet",
		"severity": "critical",
		"message": "Telnet management access must not be enabled.",
		"details": {"matched": "transport input telnet"},
	}
}

violation contains v if {
	regex.match(`\bip http server\b`, proposed)
	not regex.match(`\bip http secure-server\b`, proposed)
	v := {
		"policy": "network_security.no_plaintext_http_mgmt",
		"severity": "high",
		"message": "HTTP management must not be enabled where HTTPS/SSH is required; use 'ip http secure-server'.",
		"details": {},
	}
}

violation contains v if {
	contains(proposed, "snmp-server community")
	regex.match(`snmp-server community \S+ (ro|rw)?\s*$`, proposed)
	not contains(proposed, "snmp-server group")
	v := {
		"policy": "network_security.snmp_v1v2c_flagged",
		"severity": "medium",
		"message": "SNMPv1/v2c community string detected; prefer SNMPv3.",
		"details": {},
	}
}

weak_credential_patterns := ["password cisco", "password admin", "password password", "secret cisco", "secret admin"]

violation contains v if {
	some pattern in weak_credential_patterns
	contains(proposed, pattern)
	v := {
		"policy": "network_security.no_weak_credentials",
		"severity": "critical",
		"message": sprintf("Default/weak credential pattern detected: '%s'.", [pattern]),
		"details": {"matched": pattern},
	}
}

violation contains v if {
	input.device.role == "core"
	contains(proposed, "no ip ssh")
	v := {
		"policy": "network_security.ssh_required_for_management",
		"severity": "high",
		"message": "SSH is being disabled on a core device; SSH must remain enabled for management.",
		"details": {},
	}
}
