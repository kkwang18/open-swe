"""Allow Linear user identities"""

from alembic import op

revision = "1af046b4bf9f"
down_revision = ["1a27b64154a3", "52fab62a7608"]
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE user_identity DROP CONSTRAINT user_identity_provider_check")
    op.execute(
        "ALTER TABLE user_identity ADD CONSTRAINT user_identity_provider_check "
        "CHECK (provider IN ('github', 'slack', 'linear'))"
    )


def downgrade() -> None:
    raise NotImplementedError
