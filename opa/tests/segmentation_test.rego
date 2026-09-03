package netguard.segmentation_test

import rego.v1

import data.netguard.segmentation

mock_network_policies := {"vlans": {
	"guest": ["50"],
	"management": ["10"],
	"infrastructure": ["20"],
	"approved_registry": ["1", "10", "20", "50"],
}}

test_guest_to_management_acl_flagged if {
	inp := {"configuration": {"proposed": "access-list 101 permit ip vlan50 any vlan10 any"}}
	some v in segmentation.violation with input as inp
		with data.network_policies as mock_network_policies
	v.policy == "segmentation.guest_not_to_management"
}

test_unregistered_vlan_flagged if {
	inp := {"configuration": {"proposed": "vlan 99\n name shadow-it\n"}}
	some v in segmentation.violation with input as inp
		with data.network_policies as mock_network_policies
	v.policy == "segmentation.unauthorized_vlan"
}
