"""Add entity tables and update existing tables for protocol optimization.

Revision ID: 001_add_entity_tables
Revises:
Create Date: 2026-08-03

Adds:
- entities table (canonical entity registry)
- entity_relationships table (entity-to-entity links)
- Updates to sources, functions, columns tables for protocol v2
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '001_add_entity_tables'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add entity tables and update existing tables."""

    # Create entities table
    op.create_table(
        'entities',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('entity_type', sa.String(length=32), nullable=False),
        sa.Column('code', sa.String(length=128), nullable=False),
        sa.Column('name_en', sa.String(length=255), nullable=True),
        sa.Column('name_zh', sa.String(length=255), nullable=True),
        sa.Column('metadata_json', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('entity_type', 'code', name='uq_entity_type_code')
    )
    op.create_index('ix_entities_entity_type', 'entities', ['entity_type'], unique=False)
    op.create_index('ix_entities_code', 'entities', ['code'], unique=False)

    # Create entity_relationships table
    op.create_table(
        'entity_relationships',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('source_id', sa.Integer(), nullable=False),
        sa.Column('relation_type', sa.String(length=64), nullable=False),
        sa.Column('target_id', sa.Integer(), nullable=False),
        sa.Column('valid_from', sa.DateTime(), nullable=True),
        sa.Column('valid_to', sa.DateTime(), nullable=True),
        sa.Column('metadata_json', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['source_id'], ['entities.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['target_id'], ['entities.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('source_id', 'relation_type', 'target_id', 'valid_from', name='uq_rel_edges')
    )
    op.create_index('ix_entity_relationships_source_id', 'entity_relationships', ['source_id'], unique=False)
    op.create_index('ix_entity_relationships_relation_type', 'entity_relationships', ['relation_type'], unique=False)
    op.create_index('ix_entity_relationships_target_id', 'entity_relationships', ['target_id'], unique=False)

    # Add metadata_json to sources table (for relationship resolver info)
    op.add_column('sources', sa.Column('metadata_json', postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    """Remove entity tables and revert changes."""

    # Remove metadata_json from sources
    op.drop_column('sources', 'metadata_json')

    # Drop entity_relationships table
    op.drop_index('ix_entity_relationships_target_id', table_name='entity_relationships')
    op.drop_index('ix_entity_relationships_relation_type', table_name='entity_relationships')
    op.drop_index('ix_entity_relationships_source_id', table_name='entity_relationships')
    op.drop_table('entity_relationships')

    # Drop entities table
    op.drop_index('ix_entities_code', table_name='entities')
    op.drop_index('ix_entities_entity_type', table_name='entities')
    op.drop_table('entities')
