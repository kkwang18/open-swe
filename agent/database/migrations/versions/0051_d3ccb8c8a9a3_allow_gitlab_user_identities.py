"""Allow GitLab user identities"""

from alembic import op

revision = "d3ccb8c8a9a3"
down_revision = "1af046b4bf9f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE user_identity DROP CONSTRAINT user_identity_provider_check")
    op.execute(
        "ALTER TABLE user_identity ADD CONSTRAINT user_identity_provider_check "
        "CHECK (provider IN ('github', 'slack', 'linear', 'gitlab'))"
    )


def downgrade() -> None:
    raise NotImplementedError
