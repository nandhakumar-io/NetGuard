package netguard.change_management

import rego.v1

review contains v if {
	input.change.priority == "emergency"
	not input.change.maintenance_window
	v := {
		"policy": "change_management.emergency_outside_window",
		"severity": "medium",
		"message": "Emergency-priority change is being made outside a declared maintenance window.",
		"details": {},
	}
}

review contains v if {
	object.get(input.blast_radius, "devices", 0) >= data.network_policies.thresholds.high_blast_radius_devices
	v := {
		"policy": "change_management.high_blast_radius",
		"severity": "medium",
		"message": sprintf(
			"Change affects %d devices, at or above the high-blast-radius review threshold.",
			[object.get(input.blast_radius, "devices", 0)],
		),
		"details": {"devices": object.get(input.blast_radius, "devices", 0)},
	}
}

review contains v if {
	input.device.role == "core"
	v := {
		"policy": "change_management.core_device_stricter_review",
		"severity": "low",
		"message": "Change targets a core device role; stricter review policy applies.",
		"details": {},
	}
}
