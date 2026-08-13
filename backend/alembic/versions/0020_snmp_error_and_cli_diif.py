"""snmp poll error + cli-diff columns

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-03

Three independent, additive changes bundled into one migration since
they're all part of the same pass:

1. devices.last_snmp_poll_at / last_snmp_poll_error -- the SNMP
   in-process poll loop (app.main._snmp_inprocess_poll_loop) already
   catches and logs every per-device polling failure (unreachable,
   missing credentials, etc.) but the *result* of that only ever went to
   the server log. A device with "no metrics" and an operator looking at
   the UI had no way to tell "never been polled", "credentials
   incomplete", and "device unreachable" apart -- they all just look like
   an empty Health tab. Surfacing the last error (and when it happened)
   turns that into an actual diagnostic instead of a guess.

2. config_drifts.cli_diff -- app.services.config_format_service can now
   translate a structural XML diff into IOS-style CLI-equivalent lines
   (best-effort, not a full YANG compiler) instead of the raw
   `diff_text`/`ai_summary` sometimes showing un-decoded XML when the
   configured LLM isn't reachable.

3. change_requests.config_diff_cli / config_diff_summary -- same
   translation applied to the change-request "Configuration Diff" panel,
   which previously only ever showed the raw unified diff.

All three are nullable/no server_default requiring backfill -- existing
rows simply have no value until the next poll/drift-scan/change request
recomputes them.
"""
import sqlalchemy as sa

from migration_helpers import (
    add_column_if_missing,
    column_exists,
    drop_column_if_exists,
)

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None



def upgrade() -> None:
    if not column_exists("devices", "last_snmp_poll_at"):
        add_column_if_missing("devices", sa.Column("last_snmp_poll_at", sa.DateTime(timezone=True), nullable=True))
    if not column_exists("devices", "last_snmp_poll_error"):
        add_column_if_missing("devices", sa.Column("last_snmp_poll_error", sa.Text(), nullable=True))

    if not column_exists("config_drifts", "cli_diff"):
        add_column_if_missing("config_drifts", sa.Column("cli_diff", sa.Text(), nullable=True))

    if not column_exists("change_requests", "config_diff_cli"):
        add_column_if_missing("change_requests", sa.Column("config_diff_cli", sa.Text(), nullable=True))
    if not column_exists("change_requests", "config_diff_summary"):
        add_column_if_missing("change_requests", sa.Column("config_diff_summary", sa.Text(), nullable=True))


def downgrade() -> None:
    if column_exists("change_requests", "config_diff_summary"):
        drop_column_if_exists("change_requests", "config_diff_summary")
    if column_exists("change_requests", "config_diff_cli"):
        drop_column_if_exists("change_requests", "config_diff_cli")
    if column_exists("config_drifts", "cli_diff"):
        drop_column_if_exists("config_drifts", "cli_diff")
    if column_exists("devices", "last_snmp_poll_error"):
        drop_column_if_exists("devices", "last_snmp_poll_error")
    if column_exists("devices", "last_snmp_poll_at"):
        drop_column_if_exists("devices", "last_snmp_poll_at")
