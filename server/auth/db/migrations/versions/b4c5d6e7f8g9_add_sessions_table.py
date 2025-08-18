"""add sessions table

Revision ID: b4c5d6e7f8g9
Revises: 8606fa83a998
Create Date: 2024-01-01 12:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b4c5d6e7f8g9"
down_revision = "8606fa83a998"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True),
        sa.Column("session_id", sa.String(length=255), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=True),
        sa.Column("expiry", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("session_id"),
    )


def downgrade() -> None:
    op.drop_table("sessions")
