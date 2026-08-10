"""Section-level config parsing (Partial / Section Rollback).

Full-device rollback (rollback_service) restores an entire running-config
from a snapshot. That's the safest option, but it's also the biggest
possible blast radius: if only one ACL or one interface is broken, an
admin often doesn't want NetGuard touching anything else on the box
(other interfaces may have been legitimately reconfigured since that
snapshot was taken).

This module lets a caller identify and extract a single logical
"section" of a config -- an ACL, a VLAN, an interface stanza, a route-map,
etc -- from both the live/current config and a target snapshot, and
build a new proposed config that is the *current* config with just that
one section swapped for the snapshot's version. Everything outside the
selected section is left completely untouched.

Parsing is intentionally simple and vendor-aware only at the "what does
a block boundary look like" level (indentation for Cisco/Arista-style
configs, braces for Juniper-style configs) -- it does not attempt to
build a full semantic config tree.
"""
import re
from dataclasses import dataclass

# Header patterns that mark the start of a distinct, independently
# revertible section, keyed by the vendor family's syntax style.
# Each pattern's first capture group is used as the section's short label.
_IOS_STYLE_HEADERS = [
    (re.compile(r"^ip access-list \S+ (\S+)", re.I), "ACL"),
    (re.compile(r"^access-list (\d+)", re.I), "ACL"),
    (re.compile(r"^vlan (\d+)", re.I), "VLAN"),
    (re.compile(r"^interface (\S+)", re.I), "Interface"),
    (re.compile(r"^route-map (\S+)", re.I), "Route-map"),
    (re.compile(r"^router (\S+)", re.I), "Routing process"),
    (re.compile(r"^class-map (\S+)", re.I), "Class-map"),
    (re.compile(r"^policy-map (\S+)", re.I), "Policy-map"),
    (re.compile(r"^ip prefix-list (\S+)", re.I), "Prefix-list"),
]

# Juniper's `set`-style config has no indentation blocks; a "section" is
# every line sharing a common hierarchy prefix instead (e.g. everything
# under `firewall filter FOO ...`, or `vlans BAR ...`).
_JUNOS_PREFIXES = [
    (re.compile(r"^set firewall filter (\S+)"), "ACL"),
    (re.compile(r"^set vlans (\S+)"), "VLAN"),
    (re.compile(r"^set interfaces (\S+)"), "Interface"),
    (re.compile(r"^set policy-options policy-statement (\S+)"), "Route-map"),
    (re.compile(r"^set protocols (\S+)"), "Routing process"),
]


@dataclass
class ConfigSection:
    key: str            # stable identifier, e.g. "ACL:BLOCK_TELNET"
    kind: str            # "ACL", "VLAN", "Interface", ...
    name: str            # e.g. "BLOCK_TELNET"
    start_line: int
    end_line: int         # exclusive
    lines: list[str]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def _is_junos_style(config_text: str) -> bool:
    sample = config_text.splitlines()[:50]
    return any(line.strip().startswith("set ") for line in sample)


def _parse_ios_style(config_text: str) -> list[ConfigSection]:
    """Indentation-block parsing: a section starts at a non-indented
    header line matching one of `_IOS_STYLE_HEADERS` and runs until the
    next non-indented line (Cisco IOS/NX-OS, Arista EOS all use this
    style)."""
    lines = config_text.splitlines()
    sections: list[ConfigSection] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if line and not line[0].isspace() and stripped:
            for pattern, kind in _IOS_STYLE_HEADERS:
                m = pattern.match(stripped)
                if m:
                    name = m.group(1)
                    start = i
                    j = i + 1
                    while j < n and (lines[j] == "" or lines[j][0].isspace()):
                        # allow a single blank line inside a block, but not
                        # two in a row (that's normally a section separator)
                        if lines[j] == "" and j + 1 < n and lines[j + 1] == "":
                            break
                        j += 1
                    sections.append(
                        ConfigSection(
                            key=f"{kind}:{name}",
                            kind=kind,
                            name=name,
                            start_line=start,
                            end_line=j,
                            lines=lines[start:j],
                        )
                    )
                    i = j
                    break
            else:
                i += 1
        else:
            i += 1
    return sections


def _parse_junos_style(config_text: str) -> list[ConfigSection]:
    lines = config_text.splitlines()
    buckets: dict[str, list[tuple[int, str]]] = {}
    kinds: dict[str, str] = {}
    for idx, line in enumerate(lines):
        stripped = line.strip()
        for pattern, kind in _JUNOS_PREFIXES:
            m = pattern.match(stripped)
            if m:
                name = m.group(1)
                key = f"{kind}:{name}"
                buckets.setdefault(key, []).append((idx, line))
                kinds[key] = kind
                break
    sections = []
    for key, entries in buckets.items():
        kind = kinds[key]
        name = key.split(":", 1)[1]
        idxs = [e[0] for e in entries]
        sections.append(
            ConfigSection(
                key=key,
                kind=kind,
                name=name,
                start_line=min(idxs),
                end_line=max(idxs) + 1,
                lines=[lines[i] for i in sorted(idxs)],
            )
        )
    return sections


def list_sections(config_text: str) -> list[ConfigSection]:
    """Every independently revertible section found in a config, in the
    order they appear. Used to populate the "pick what to roll back"
    list in the UI."""
    if not config_text:
        return []
    if _is_junos_style(config_text):
        return _parse_junos_style(config_text)
    return _parse_ios_style(config_text)


def get_section(config_text: str, section_key: str) -> ConfigSection | None:
    for section in list_sections(config_text):
        if section.key == section_key:
            return section
    return None


def build_partial_config(current_config: str, target_config: str, section_key: str) -> tuple[str, dict]:
    """Returns (new_config, info) where new_config is `current_config`
    with only `section_key` replaced by its version from `target_config`
    (or removed entirely, if it doesn't exist in the target -- e.g.
    restoring "this ACL didn't exist yet" at that snapshot). Every other
    line of `current_config` is byte-for-byte unchanged.

    Raises ValueError if the section isn't present in the *current*
    config -- there is nothing to replace, so a partial rollback isn't a
    meaningful operation (a full rollback or a plain deploy would be).
    """
    current_sections = {s.key: s for s in list_sections(current_config)}
    target_sections = {s.key: s for s in list_sections(target_config)}

    if section_key not in current_sections:
        raise ValueError(
            f"Section '{section_key}' was not found in the current configuration -- "
            "nothing to partially roll back."
        )

    current_section = current_sections[section_key]
    target_section = target_sections.get(section_key)

    current_lines = current_config.splitlines()
    new_lines = (
        current_lines[: current_section.start_line]
        + (target_section.lines if target_section else [])
        + current_lines[current_section.end_line :]
    )

    return "\n".join(new_lines), {
        "kind": current_section.kind,
        "name": current_section.name,
        "existed_in_target": target_section is not None,
        "current_line_count": len(current_section.lines),
        "target_line_count": len(target_section.lines) if target_section else 0,
    }
