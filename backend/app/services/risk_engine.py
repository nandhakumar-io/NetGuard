"""AI Configuration Analyzer.

Rule-based risk detector for v1 (prototype). Each matched rule contributes
weighted points to an overall 0-100 risk score. This module is intentionally
isolated so it can later be swapped for an ML/LLM-backed scorer without
touching callers.
"""
import re

from app.core.config import settings
from app.schemas.change_request import RiskAnalysisResult

# (regex pattern, weight, human-readable finding)
RISK_RULES: list[tuple[str, int, str]] = [
    (r"\bno\s+router\s+bgp\b", 35, "BGP process removal detected"),
    (r"\bno\s+neighbor\s+\S+", 30, "BGP neighbor removal detected"),
    (r"\bno\s+router\s+ospf\b", 30, "OSPF process removal detected"),
    (r"\bshutdown\b", 20, "Interface shutdown detected"),
    (r"\bip\s+route\s+0\.0\.0\.0\s+0\.0\.0\.0\b", 15, "Default route change detected"),
    (r"\bno\s+ip\s+route\b", 15, "Static route removal detected"),
    (r"\baccess-list\s+\d+\s+deny\s+any\b", 15, "Broad ACL deny rule detected"),
    (r"\bno\s+vlan\s+\d+\b", 10, "VLAN removal detected"),
    (r"\bip\s+address\s+(\d{1,3}\.){3}\d{1,3}\s+255\.255\.255\.255\b", 10, "Suspicious /32 subnet mask"),
]


def analyze(proposed_config: str, current_config: str | None = None) -> RiskAnalysisResult:
    findings: list[str] = []
    score = 0

    text = proposed_config.lower()
    for pattern, weight, message in RISK_RULES:
        if re.search(pattern, text):
            findings.append(message)
            score += weight

    # Duplicate IP heuristic: same IP appears in both current and proposed on different lines
    if current_config:
        current_ips = set(re.findall(r"ip address (\d{1,3}(?:\.\d{1,3}){3})", current_config.lower()))
        proposed_ips = re.findall(r"ip address (\d{1,3}(?:\.\d{1,3}){3})", text)
        if len(proposed_ips) != len(set(proposed_ips)):
            findings.append("Duplicate IP address within proposed configuration")
            score += 20
        if current_ips and set(proposed_ips) & current_ips and current_config != proposed_config:
            # informational only, not scored, kept simple for prototype
            pass

    score = min(score, 100)

    if score <= settings.RISK_LOW_MAX:
        classification, recommendation = "Low Risk", "Safe to Deploy"
    elif score <= settings.RISK_MEDIUM_MAX:
        classification, recommendation = "Medium Risk", "Review Recommended Before Deploy"
    else:
        classification, recommendation = "Critical Risk", "Deployment Not Recommended"

    if not findings:
        findings.append("No significant risk patterns detected")

    return RiskAnalysisResult(
        risk_score=score,
        classification=classification,
        recommendation=recommendation,
        findings=findings,
    )
