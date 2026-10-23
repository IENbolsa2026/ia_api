"""Small multilingual embeddings for catalog retrieval, never for CV evidence."""

from __future__ import annotations

import asyncio
import logging
from threading import Lock
from typing import Iterable

from app.core.config import settings

logger = logging.getLogger(__name__)
DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
_lock = Lock()
_model = None
_model_name: str | None = None


def _load_model(*, allow_download: bool):
    global _model, _model_name
    name = settings.SKILL_CATALOG_EMBEDDING_MODEL or DEFAULT_MODEL
    with _lock:
        if _model is None or _model_name != name:
            from fastembed import TextEmbedding

            _model = TextEmbedding(model_name=name, local_files_only=not allow_download, threads=2)
            _model_name = name
        return _model


def _embed_texts(texts: list[str], *, passage: bool, allow_download: bool) -> list[list[float]]:
    model = _load_model(allow_download=allow_download)
    vectors = model.passage_embed(texts) if passage else model.query_embed(texts)
    return [vector.astype(float).tolist() for vector in vectors]


async def embed_queries(texts: Iterable[str]) -> list[list[float]] | None:
    """Return None when the optional local model has not been preloaded."""
    values = [text.strip() for text in texts if text and text.strip()]
    if not values:
        return []
    try:
        return await asyncio.to_thread(_embed_texts, values, passage=False, allow_download=False)
    except (ImportError, ValueError, OSError, RuntimeError) as exc:
        logger.warning("Skill embedding unavailable (%s); using lexical retrieval", type(exc).__name__)
        return None


async def embed_passages(texts: Iterable[str], *, allow_download: bool = False) -> list[list[float]]:
    """Used by the administrative importer to embed catalog descriptions."""
    values = [text.strip() for text in texts]
    if any(not value for value in values):
        raise ValueError("cannot embed empty catalog passages")
    if not values:
        return []
    return await asyncio.to_thread(_embed_texts, values, passage=True, allow_download=allow_download)

