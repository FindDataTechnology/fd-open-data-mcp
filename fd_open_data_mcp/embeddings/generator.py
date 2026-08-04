"""Entity embedding generator using sentence transformers."""
from __future__ import annotations

import logging
from typing import List, Dict, Any, Optional
import json

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

logger = logging.getLogger(__name__)


class EntityEmbeddingGenerator:
    """Generate vector embeddings for entities using sentence transformers."""

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        """
        Initialize the embedding generator.

        Args:
            model_name: Name of the sentence transformer model to use
        """
        try:
            from sentence_transformers import SentenceTransformer
            self.model = SentenceTransformer(model_name)
            self.model_name = model_name
            logger.info(f"Loaded embedding model: {model_name}")
        except ImportError:
            raise ImportError(
                "sentence-transformers is required. Install with: "
                "pip install sentence-transformers"
            )

    def generate_embedding(self, text: str) -> List[float]:
        """
        Generate embedding for a single text.

        Args:
            text: Input text to embed

        Returns:
            List of floats representing the embedding vector
        """
        embedding = self.model.encode(text)
        return embedding.tolist()

    def generate_embeddings_batch(self, texts: List[str]) -> List[List[float]]:
        """
        Generate embeddings for a batch of texts.

        Args:
            texts: List of input texts to embed

        Returns:
            List of embedding vectors
        """
        embeddings = self.model.encode(texts, show_progress_bar=True)
        return [emb.tolist() for emb in embeddings]

    def prepare_entity_text(self, entity: Dict[str, Any]) -> str:
        """
        Prepare text representation of an entity for embedding.

        Args:
            entity: Entity dictionary with metadata

        Returns:
            Text string representing the entity
        """
        parts = []

        # Add entity type
        if entity.get("entity_type"):
            parts.append(entity["entity_type"])

        # Add names
        if entity.get("name_en"):
            parts.append(entity["name_en"])
        if entity.get("name_zh"):
            parts.append(entity["name_zh"])

        # Add code
        if entity.get("code"):
            parts.append(entity["code"])

        # Add metadata
        metadata = entity.get("metadata_json")
        if metadata:
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except json.JSONDecodeError:
                    metadata = {}

            if isinstance(metadata, dict):
                # Add key metadata fields
                for key in ["sector", "industry", "region", "country"]:
                    if key in metadata and metadata[key]:
                        parts.append(str(metadata[key]))

        return " | ".join(parts)

    def generate_all_entity_embeddings(
        self,
        database_url: str,
        batch_size: int = 100
    ) -> Dict[str, Any]:
        """
        Generate embeddings for all entities in the database.

        Args:
            database_url: Database connection URL
            batch_size: Number of entities to process in each batch

        Returns:
            Dictionary with generation statistics
        """
        engine = create_engine(database_url)
        Session = sessionmaker(bind=engine)
        session = Session()

        try:
            # Get all entities
            result = session.execute(text("""
                SELECT id, entity_type, code, name_en, name_zh, metadata_json
                FROM entities
            """))

            entities = []
            for row in result:
                entities.append({
                    "id": row.id,
                    "entity_type": row.entity_type,
                    "code": row.code,
                    "name_en": row.name_en,
                    "name_zh": row.name_zh,
                    "metadata_json": row.metadata_json
                })

            logger.info(f"Found {len(entities)} entities to embed")

            # Check which entities already have embeddings
            result = session.execute(text("""
                SELECT entity_id FROM entity_embeddings
                WHERE model = :model
            """), {"model": self.model_name})

            existing_ids = {row.entity_id for row in result}
            entities_to_embed = [e for e in entities if e["id"] not in existing_ids]

            logger.info(f"Found {len(entities_to_embed)} entities without embeddings")

            if not entities_to_embed:
                return {
                    "status": "success",
                    "total_entities": len(entities),
                    "already_embedded": len(existing_ids),
                    "newly_embedded": 0,
                    "message": "All entities already have embeddings"
                }

            # Generate embeddings in batches
            total_embedded = 0
            for i in range(0, len(entities_to_embed), batch_size):
                batch = entities_to_embed[i:i + batch_size]

                # Prepare texts
                texts = [self.prepare_entity_text(e) for e in batch]

                # Generate embeddings
                embeddings = self.generate_embeddings_batch(texts)

                # Insert into database
                for entity, embedding in zip(batch, embeddings):
                    session.execute(text("""
                        INSERT INTO entity_embeddings (entity_id, embedding, model)
                        VALUES (:entity_id, :embedding, :model)
                        ON CONFLICT (entity_id, model) DO UPDATE
                        SET embedding = EXCLUDED.embedding
                    """), {
                        "entity_id": entity["id"],
                        "embedding": json.dumps(embedding),
                        "model": self.model_name
                    })

                    total_embedded += 1

                session.commit()
                logger.info(f"Embedded {total_embedded}/{len(entities_to_embed)} entities")

            return {
                "status": "success",
                "total_entities": len(entities),
                "already_embedded": len(existing_ids),
                "newly_embedded": total_embedded,
                "model": self.model_name
            }

        except Exception as e:
            session.rollback()
            logger.error(f"Failed to generate embeddings: {e}")
            return {
                "status": "error",
                "error": str(e),
                "total_entities": len(entities) if 'entities' in locals() else 0,
                "newly_embedded": total_embedded if 'total_embedded' in locals() else 0
            }
        finally:
            session.close()
