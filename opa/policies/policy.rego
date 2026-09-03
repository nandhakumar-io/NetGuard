package netguard

import rego.v1

# This is the document `opa_service.py` requests (OPA_POLICY_PATH =
# /v1/data/netguard). It aggregates every sub-package's `violation` /
# `review` / `warning` sets into the single shape opa_service._parse_decision
# expects: {violations, warnings, matched_policies, deny, review}.

import data.netguard.access_control
import data.netguard.change_management
import data.netguard.network_security
import data.netguard.routing
import data.netguard.segmentation

all_violations := network_security.violation | segmentation.violation | access_control.violation | routing.violation

all_reviews := change_management.review | access_control.review | routing.review

violations contains v if {
	some v in all_violations
}

violations contains v if {
	some v in all_reviews
}

matched_policies contains p if {
	some v in (all_violations | all_reviews)
	p := v.policy
}

deny if {
	some v in all_violations
	v.severity in {"critical", "high"}
}

review if {
	some v in all_reviews
}

review if {
	some v in all_violations
	v.severity in {"medium", "low"}
}

policy_version := data.network_policies.policy_version
