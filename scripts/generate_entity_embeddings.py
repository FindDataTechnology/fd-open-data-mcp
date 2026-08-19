"""Generate embeddings for all entities in the database."""
import os
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from fd_open_data_mcp.embeddings.generator import EntityEmbeddingGenerator


def main():
    """Generate embeddings for all entities."""
    database_url = os.environ.get(
        "FD_OPEN_DATA_MCP_DATABASE_URL",
        "postgresql://fd:FD_PG_PASSWORD@guangzhou-xinru:30432/fd_open_data"
    )

    print("Initializing embedding generator...")
    generator = EntityEmbeddingGenerator(model_name="all-MiniLM-L6-v2")

    print("Generating embeddings for all entities...")
    result = generator.generate_all_entity_embeddings(
        database_url=database_url,
        batch_size=100
    )

    print("\n" + "="*60)
    print("Embedding Generation Complete")
    print("="*60)
    print(f"Status: {result['status']}")
    print(f"Total entities: {result['total_entities']}")
    print(f"Already embedded: {result['already_embedded']}")
    print(f"Newly embedded: {result['newly_embedded']}")

    if result['status'] == 'error':
        print(f"Error: {result.get('error', 'Unknown error')}")
        sys.exit(1)
    else:
        print("\n✓ All entities embedded successfully!")


if __name__ == "__main__":
    main()
