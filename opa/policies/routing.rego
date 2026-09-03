package netguard.routing

import rego.v1

proposed := lower(input.configuration.proposed)
current := lower(object.get(input.configuration, "current", ""))

authorized_default_route_sources := data.network_policies.routing.authorized_default_route_next_hops

violation contains v if {
	matches := regex.find_all_string_submatch_n(`ip route 0\.0\.0\.0 0\.0\.0\.0 (\S+)`, proposed, -1)
	some m in matches
	next_hop := m[1]
	not next_hop in authorized_default_route_sources
	v := {
		"policy": "routing.unauthorized_default_route",
		"severity": "high",
		"message": sprintf("Proposed default route via %s is not in the authorized default-route next-hop list.", [next_hop]),
		"details": {"next_hop": next_hop},
	}
}

critical_networks := data.network_policies.routing.critical_networks

review contains v if {
	some net in critical_networks
	route_line := sprintf("ip route %s", [net])
	contains(current, route_line)
	not contains(proposed, route_line)
	v := {
		"policy": "routing.critical_route_removed",
		"severity": "high",
		"message": sprintf("A route to critical network %s present in the current configuration is missing from the proposed configuration.", [net]),
		"details": {"network": net},
	}
}

review contains v if {
	input.device.role == "core"
	regex.match(`router (bgp|ospf|eigrp)`, proposed)
	v := {
		"policy": "routing.core_routing_change_elevated_approval",
		"severity": "medium",
		"message": "Routing protocol configuration change on a core device requires elevated approval.",
		"details": {},
	}
}
