"""Intelligent Configuration Diff.

Produces a unified, line-by-line diff between current and proposed
configuration, suitable for rendering with color coding in the frontend.
"""
import difflib


def generate_diff(current_config: str | None, proposed_config: str) -> str:
    current_lines = (current_config or "").splitlines()
    proposed_lines = proposed_config.splitlines()

    diff = difflib.unified_diff(
        current_lines,
        proposed_lines,
        fromfile="current_configuration",
        tofile="proposed_configuration",
        lineterm="",
    )
    return "\n".join(diff)
