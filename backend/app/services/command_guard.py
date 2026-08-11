"""
Terminal line buffering and destructive command guard.

To prevent operators from accidentally rebooting or wiping devices over the
browser terminal, this buffers typed characters until a newline. If the
resulting command matches a destructive pattern (like 'reload'), it blocks
the newline from being forwarded to the device, leaving the command untriggered,
and returns the blocked rule to the caller so they can surface a warning.
"""
import re
from dataclasses import dataclass

OVERRIDE_TOKEN = "FORCE"

@dataclass
class LineGuardState:
    device_id: str
    username: str
    forwarded_override_count: int = 0
    _line: str = ""

_DESTRUCTIVE_RULES = [
    (re.compile(r"^\s*reload(\s|$)", re.IGNORECASE), "reload"),
    (re.compile(r"^\s*write\s+erase(\s|$)", re.IGNORECASE), "write erase"),
    (re.compile(r"^\s*erase\s+startup-config(\s|$)", re.IGNORECASE), "erase startup-config"),
    (re.compile(r"^\s*request\s+system\s+(reboot|halt)(\s|$)", re.IGNORECASE), "request system reboot/halt"),
]

def feed_keystroke(state: LineGuardState, data: str) -> tuple[str, str | None, str | None]:
    to_forward = ""
    blocked_rule = None

    for char in data:
        if char in ("\r", "\n"):
            command = state._line.strip()

            matched_rule = None
            for p, rule_name in _DESTRUCTIVE_RULES:
                if p.search(command):
                    matched_rule = rule_name
                    break

            if matched_rule:
                if command.endswith(OVERRIDE_TOKEN):
                    state.forwarded_override_count += 1
                    to_forward += char
                else:
                    blocked_rule = matched_rule
                    # do not forward the newline character!
            else:
                to_forward += char

            state._line = ""
        elif char in ("\b", "\x7f"):
            if state._line:
                state._line = state._line[:-1]
            to_forward += char
        else:
            if char.isprintable() or char == " ":
                state._line += char
            to_forward += char

    return "", (to_forward if to_forward else None), blocked_rule
