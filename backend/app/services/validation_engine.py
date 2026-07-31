"""Automated Validation Engine.

Performs lightweight syntax and structural checks on a proposed configuration
before it is allowed to proceed to deployment. Real syntax validation would
typically use vendor-specific parsers (e.g. Cisco IOS grammar); this
prototype uses pragmatic heuristics.
"""
from dataclasses import dataclass, field


@dataclass
class ValidationResult:
    passed: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


REQUIRED_INTERFACE_PATTERN = "interface"
KNOWN_INVALID_TOKENS = ["TODO", "FIXME", "<placeholder>"]


def validate_syntax(config_text: str) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []

    if not config_text or not config_text.strip():
        errors.append("Proposed configuration is empty")
        return ValidationResult(passed=False, errors=errors)

    for token in KNOWN_INVALID_TOKENS:
        if token.lower() in config_text.lower():
            errors.append(f"Placeholder/unsupported token found: '{token}'")

    lines = [line.strip() for line in config_text.splitlines() if line.strip()]
    for line in lines:
        if line.lower().startswith("interface") and len(line.split()) < 2:
            errors.append(f"Malformed interface declaration: '{line}'")
        if line.lower().startswith("ip address") and len(line.split()) < 3:
            errors.append(f"Missing parameter in IP address line: '{line}'")

    if REQUIRED_INTERFACE_PATTERN not in config_text.lower():
        warnings.append("No interface block found in proposed configuration")

    return ValidationResult(passed=len(errors) == 0, errors=errors, warnings=warnings)
