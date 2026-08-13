"""Intelligent Configuration Diff.

Produces a unified, line-by-line diff between current and proposed
configuration, suitable for rendering with color coding in the frontend.
"""
import difflib


def generate_diff(current_config: str | None, proposed_config: str | None) -> str:
    current_lines = (current_config or "").splitlines()
    proposed_lines = (proposed_config or "").splitlines()

    diff = difflib.unified_diff(
        current_lines,
        proposed_lines,
        fromfile="current_configuration",
        tofile="proposed_configuration",
        lineterm="",
    )
    return "\n".join(diff)


def _line_delta(diff_text: str) -> tuple[int, int]:
    """(added, removed) line counts from a unified diff, ignoring the
    +++/---/@@ header/hunk lines. Shared counting logic so every diff
    (two-way or three-way) reports the same way."""
    added = removed = 0
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed


def three_way_diff(
    golden_config: str | None,
    current_config: str | None,
    proposed_config: str | None,
) -> dict:
    """Three-way config comparison: running (current) vs. golden
    (approved baseline) vs. proposed (the pending change) -- not just the
    plain "what does this change do" two-way diff, but "does this change
    move the device toward or away from its approved baseline".

    Three pairwise unified diffs are returned so a reviewer (or the UI)
    can render any of the three legs on its own:
      - current_vs_proposed: the change itself (same as generate_diff)
      - golden_vs_current:   how far the device has already drifted
      - golden_vs_proposed:  how far the device WOULD be from golden
                              after this change ships

    `drift_direction` is the headline signal for a reviewer scanning a
    queue of change requests: whether the proposed config is closer to,
    farther from, or equally far from the golden baseline than the
    device's current config already is. Distance is measured as
    added+removed line count vs. golden -- coarse (line-based, not
    semantic), but the same measure used everywhere else in this
    codebase's diffs, so it stays consistent with what the UI already
    shows for a two-way diff.

    `golden_available` is False when no golden config is set for the
    device yet -- the three pairwise diffs are still computed with an
    empty golden side in that case (so the response shape never changes),
    but callers should treat drift_direction as meaningless without a
    real baseline to compare against.
    """
    golden_available = golden_config is not None

    current_vs_proposed = generate_diff(current_config, proposed_config)
    golden_vs_current = generate_diff(golden_config, current_config)
    golden_vs_proposed = generate_diff(golden_config, proposed_config)

    current_added, current_removed = _line_delta(golden_vs_current)
    proposed_added, proposed_removed = _line_delta(golden_vs_proposed)
    current_drift_lines = current_added + current_removed
    proposed_drift_lines = proposed_added + proposed_removed

    if not golden_available:
        drift_direction = "unknown"
    elif current_drift_lines == 0 and proposed_drift_lines == 0:
        drift_direction = "unchanged"  # already compliant, stays compliant
    elif proposed_drift_lines < current_drift_lines:
        drift_direction = "toward_compliance"
    elif proposed_drift_lines > current_drift_lines:
        drift_direction = "away_from_compliance"
    else:
        drift_direction = "unchanged"

    return {
        "golden_available": golden_available,
        "current_vs_proposed": current_vs_proposed,
        "golden_vs_current": golden_vs_current,
        "golden_vs_proposed": golden_vs_proposed,
        "current_drift_lines": current_drift_lines,
        "proposed_drift_lines": proposed_drift_lines,
        "drift_direction": drift_direction,
    }
