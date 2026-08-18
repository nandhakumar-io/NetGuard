"""Config secret scanning -- run before any config text is committed to
a GitOps-tracked repo (see app.services.git_sync_service.push_template_
version) so SNMP community strings, pre-shared keys, plaintext RADIUS/
TACACS+ secrets, or bare passwords never land in git history. Git
history is effectively permanent (rewriting it after the fact means
force-pushing, coordinating every clone, and hoping nobody already
pulled) -- so this is deliberately a hard gate on the write path rather
than a lint warning, and there's no "commit anyway" override in
git_sync_service; the secret has to actually be removed from the
template body (parameterized as a Jinja variable and pulled from
credential_service at deploy time, the way SSH/SNMP credentials already
work) before the push will go through.

Patterns below are vendor-config-aware (Cisco/Juniper/Arista syntax for
the secrets that show up in golden configs specifically) rather than a
generic secret-scanner ruleset -- a config template's "secret-shaped"
content is narrower and more predictable than an arbitrary source file's
would be, which keeps false positives on ordinary config lines (ACLs,
descriptions, hostnames) low without needing entropy-based heuristics.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class SecretFinding:
    rule: str
    line_number: int
    excerpt: str  # the matched line, with the secret value itself redacted


# Each entry: (rule name, compiled pattern, group index of the secret
# value to redact in the excerpt -- 0 means "redact the whole match").
# Patterns match on keyword + explicit value; comments/docs mentioning
# these keywords without a value (e.g. "configure SNMP community below")
# don't trip them.
_RULES: list[tuple[str, re.Pattern, int]] = [
    (
        "snmp_community",
        re.compile(r"(?i)\bsnmp-server\s+community\s+(\S+)"),
        1,
    ),
    (
        "snmp_v3_auth_key",
        re.compile(r"(?i)\b(?:authentication-key|auth-password)\s+(\S+)"),
        1,
    ),
    (
        "snmp_v3_priv_key",
        re.compile(r"(?i)\b(?:privacy-key|priv-password)\s+(\S+)"),
        1,
    ),
    (
        "pre_shared_key",
        re.compile(
            r"(?i)\b(?:pre-shared-key|preshared-key|ipsec-attribute\s+pre-shared-key|"
            r"crypto\s+isakmp\s+key|set\s+security\s+ike\s+.*\s+pre-shared-key)\s+(\S+)"
        ),
        1,
    ),
    (
        "cisco_type7_password",
        # `password 7 <hex>` / `secret 7 <hex>` (weakly "encrypted" --
        # trivially reversible -- Cisco Type 7). Type 5/8/9 hashes are
        # NOT flagged (irreversible, safe to keep in a golden config).
        re.compile(r"(?i)\b(?:password|secret)\s+7\s+([0-9A-Fa-f]+)"),
        1,
    ),
    (
        "plaintext_password",
        # `password <value>` / `secret <value>` either with an explicit
        # Cisco Type 0 marker (genuinely plaintext) or no encryption-type
        # marker at all (defaults to plaintext). Types 5/8/9 are
        # irreversible hashes and are NOT flagged; Type 7 is handled by
        # its own rule above.
        re.compile(r"(?i)\b(?:username\s+\S+\s+password|enable\s+password|password|secret)\s+(?:0\s+)?(?!5\s|7\s|8\s|9\s)(\S{3,})\s*$"),
        1,
    ),
    (
        "radius_tacacs_key",
        re.compile(r"(?i)\b(?:radius-server|tacacs-server|tacacs\s+server\s+\S+\s*\n?\s*key)\s+key\s+(\S+)"),
        1,
    ),
    (
        "wifi_psk",
        re.compile(r"(?i)\bwpa-psk\s+ascii\s+(\S+)"),
        1,
    ),
    (
        "junos_secret_plaintext",
        # Junos `... secret "plaintext"` before it's been committed and
        # re-displayed as `$9$...` -- a golden-config template authored
        # by hand can easily still have the plaintext form.
        re.compile(r'(?i)\bsecret\s+"([^"$][^"]*)"'),
        1,
    ),
    (
        "private_key_block",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        0,
    ),
]


def _redact(line: str, match: re.Match, group: int) -> str:
    start, end = match.span(group if group else 0)
    return line[:start] + "***REDACTED***" + line[end:]


def scan_text(text: str) -> list[SecretFinding]:
    """Scans `text` (a config template body) line by line against every
    rule in _RULES. Returns every match found -- callers decide whether
    to block (git_sync_service push path) or just report (anywhere a
    softer warning is appropriate).
    """
    findings: list[SecretFinding] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for rule, pattern, group in _RULES:
            match = pattern.search(line)
            if match:
                findings.append(
                    SecretFinding(rule=rule, line_number=line_number, excerpt=_redact(line.strip(), match, group))
                )
    return findings


def redact_text(text: str) -> tuple[str, bool]:
    """Redacts every rule match in `text` in place (not line-indexed --
    used for arbitrary chunks, e.g. terminal session recordings, that
    don't reliably land on line boundaries the way a config template
    body does). Returns (redacted_text, any_redacted).
    """
    redacted_any = False
    out_lines = []
    for line in text.splitlines(keepends=True):
        new_line = line
        for _rule, pattern, group in _RULES:
            match = pattern.search(new_line)
            if match:
                new_line = _redact(new_line.rstrip("\r\n"), match, group) + new_line[len(new_line.rstrip("\r\n")):]
                redacted_any = True
        out_lines.append(new_line)
    return "".join(out_lines), redacted_any


def format_findings(findings: list[SecretFinding]) -> str:
    return "; ".join(f"line {f.line_number} [{f.rule}]: {f.excerpt}" for f in findings)
