package netguard.access_control

import rego.v1

proposed := lower(input.configuration.proposed)
current := lower(object.get(input.configuration, "current", ""))

management_subnets := data.network_policies.protected_subnets.management

violation contains v if {
	regex.match(`permit ip any any`, proposed)
	some subnet in management_subnets
	contains(proposed, subnet)
	v := {
		"policy": "access_control.no_unrestricted_management_access",
		"severity": "critical",
		"message": "Proposed ACL permits unrestricted (any any) access in a context referencing a protected management network.",
		"details": {},
	}
}

# A deny rule that protected a management subnet in the current config but
# is absent from the proposed config -- requires review even if nothing
# else looks obviously wrong, since removing a deny is easy to miss in a
# large diff.
review contains v if {
	current != ""
	some subnet in management_subnets
	deny_line := sprintf("deny ip any %s", [subnet])
	contains(current, deny_line)
	not contains(proposed, deny_line)
	v := {
		"policy": "access_control.deny_rule_removed",
		"severity": "medium",
		"message": sprintf("A deny rule protecting management subnet %s present in the current configuration is missing from the proposed configuration.", [subnet]),
		"details": {"subnet": subnet},
	}
}
