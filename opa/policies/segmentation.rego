package netguard.segmentation

import rego.v1

proposed := lower(input.configuration.proposed)

# Guest VLAN / management VLAN identifiers come from the shared policy
# data document rather than being hardcoded, so an operator can retune
# them per-deployment without a Rego change.
guest_vlans := data.network_policies.vlans.guest
management_vlans := data.network_policies.vlans.management
infrastructure_vlans := data.network_policies.vlans.infrastructure

violation contains v if {
	some g in guest_vlans
	some m in management_vlans
	regex.match(sprintf(`permit ip [^\n]*%s[^\n]*%s`, [g, m]), proposed)
	v := {
		"policy": "segmentation.guest_not_to_management",
		"severity": "critical",
		"message": sprintf("Proposed ACL appears to permit guest VLAN %s to reach management VLAN %s.", [g, m]),
		"details": {"guest_vlan": g, "management_vlan": m},
	}
}

violation contains v if {
	some g in guest_vlans
	some i in infrastructure_vlans
	regex.match(sprintf(`permit ip [^\n]*%s[^\n]*%s`, [g, i]), proposed)
	v := {
		"policy": "segmentation.guest_not_to_infrastructure",
		"severity": "critical",
		"message": sprintf("Proposed ACL appears to permit guest VLAN %s to reach infrastructure VLAN %s.", [g, i]),
		"details": {"guest_vlan": g, "infrastructure_vlan": i},
	}
}

# New VLAN introduced that isn't in the approved VLAN registry.
introduced_vlan_ids := {id |
	some line in split(input.configuration.proposed, "\n")
	matches := regex.find_all_string_submatch_n(`^vlan (\d+)`, trim_space(line), -1)
	count(matches) > 0
	id := matches[0][1]
}

known_vlan_ids := data.network_policies.vlans.approved_registry

violation contains v if {
	some id in introduced_vlan_ids
	not id in known_vlan_ids
	v := {
		"policy": "segmentation.unauthorized_vlan",
		"severity": "medium",
		"message": sprintf("VLAN %s is not in the approved VLAN registry and requires explicit policy approval.", [id]),
		"details": {"vlan_id": id},
	}
}
