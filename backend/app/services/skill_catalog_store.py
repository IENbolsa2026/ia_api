"""Optional asynchronous PostgreSQL/pgvector catalog with a local lexical fallback.

The catalog retrieves definitions; a retrieval hit is never evidence that a CV
demonstrates a skill. Importing a catalog is explicit and never runs on startup.
The asyncpg dependency is lazy so offline/default deployments still work.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import logging
import math
import os
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence
import unicodedata


logger = logging.getLogger(__name__)
DEFAULT_EMBEDDING_DIMENSIONS = 384
_TOKEN_RE = re.compile(r"\.\w+|\w+(?:\+\+|#)?", re.UNICODE)


def _normalize(value: str) -> str:
    value = unicodedata.normalize('NFKD', value)
    value = ''.join(c for c in value if not unicodedata.combining(c))
    return ' '.join(value.casefold().split())


def _strings(value: Any, field: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ValueError(f'{field} must be a list of strings')
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f'{field} must contain non-empty strings')
    return tuple(dict.fromkeys(item.strip() for item in value))


def _vector(value: Sequence[float], dimensions: int | None = None) -> tuple[float, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError('embedding must be a list of finite numbers')
    if not value or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in value):
        raise ValueError('embedding must be a non-empty list of finite numbers')
    result = tuple(float(v) for v in value)
    if not all(math.isfinite(v) for v in result) or not any(result):
        raise ValueError('embedding must be finite and nonzero')
    if dimensions is not None and len(result) != dimensions:
        raise ValueError(f'embedding must have {dimensions} dimensions')
    return result


@dataclass(frozen=True)
class SkillConcept:
    concept_id: str
    label: str
    aliases: tuple[str, ...] = ()
    definition: str = ''
    positive_signals: tuple[str, ...] = ()
    negative_signals: tuple[str, ...] = ()
    domain: str = ''
    embedding: tuple[float, ...] | None = None
    version: str = '1'
    source: str = 'custom'

    @classmethod
    def from_mapping(cls, item: Mapping[str, Any]) -> SkillConcept:
        values: dict[str, Any] = {}
        for field in ('concept_id', 'label', 'definition', 'domain', 'version', 'source'):
            value = item.get(field, cls.__dataclass_fields__[field].default)
            if not isinstance(value, str):
                raise ValueError(f'{field} must be a string')
            values[field] = value.strip()
        if any(not values[key] for key in ('concept_id', 'label', 'version', 'source')):
            raise ValueError('concept_id, label, version, and source are required')
        for field in ('aliases', 'positive_signals', 'negative_signals'):
            values[field] = _strings(item.get(field, []), field)
        embedding = item.get('embedding')
        values['embedding'] = None if embedding is None else _vector(embedding)
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        for field in ('aliases', 'positive_signals', 'negative_signals'):
            result[field] = list(result[field])
        result['embedding'] = list(self.embedding) if self.embedding is not None else None
        return result


@dataclass(frozen=True)
class SkillCatalogHit:
    concept: SkillConcept
    score: float
    method: str

    def to_dict(self) -> dict[str, Any]:
        return {**self.concept.to_dict(), 'score': self.score, 'method': self.method}


def _concept(item: SkillConcept | Mapping[str, Any]) -> SkillConcept:
    # Validate dataclass instances too: annotations alone do not enforce input types.
    return SkillConcept.from_mapping(item.to_dict() if isinstance(item, SkillConcept) else item)


def _search_text(concept: SkillConcept) -> str:
    return _normalize(' '.join((concept.label, *concept.aliases, concept.definition,
                               *concept.positive_signals, concept.domain)))


def _lexical_hits(concepts: Iterable[SkillConcept], query: str, limit: int,
                  domain: str | None) -> list[SkillCatalogHit]:
    query = _normalize(query)
    tokens = set(_TOKEN_RE.findall(query))
    if not tokens:
        return []
    hits = []
    for concept in concepts:
        if domain is not None and concept.domain != domain:
            continue
        names = [_normalize(name) for name in (concept.label, *concept.aliases)]
        if query in names:
            score, method = 1.0, 'exact'
        else:
            # Phrase boundaries keep Python out of Pythonista and C# apart from C++.
            phrase_match = any(set(_TOKEN_RE.findall(name)) <= tokens and
                               re.search(r'(?<!\w)' + re.escape(name) + r'(?!\w)', query)
                               for name in names)
            document_tokens = set(_TOKEN_RE.findall(_search_text(concept)))
            common = tokens & document_tokens
            if not common and not phrase_match:
                continue
            score = 0.99 if phrase_match else 0.3 * len(common) / len(tokens)
            method = 'exact' if phrase_match else 'lexical'
        hits.append(SkillCatalogHit(concept, score, method))
    return sorted(hits, key=lambda h: (-h.score, h.concept.concept_id))[:limit]


_COLUMNS = ('concept_id, label, aliases, definition, positive_signals, '
            'negative_signals, domain, embedding::text AS embedding, version, source')


class SkillCatalogStore:
    def __init__(self, dsn: str | None = None, *,
                 concepts: Iterable[SkillConcept | Mapping[str, Any]] = (),
                 embedding_dimensions: int = DEFAULT_EMBEDDING_DIMENSIONS) -> None:
        if isinstance(embedding_dimensions, bool) or not isinstance(embedding_dimensions, int) or embedding_dimensions < 1:
            raise ValueError('embedding_dimensions must be a positive integer')
        self.dsn = dsn.strip() if dsn else None
        self.embedding_dimensions = embedding_dimensions
        self._concepts = {item.concept_id: item for item in map(_concept, concepts)}
        self._pool: Any = None
        self._pool_lock = asyncio.Lock()
        self._database_failed = False
        self.last_search_backend = 'none'
        self.last_fallback_reason: str | None = None

    @classmethod
    def from_env(cls, *, concepts: Iterable[SkillConcept | Mapping[str, Any]] = ()) -> SkillCatalogStore:
        # Settings also reads backend/.env; os.getenv alone misses that file.
        from app.core.config import settings

        dsn = getattr(settings, 'SKILL_CATALOG_DATABASE_URL', None) or os.getenv('SKILL_CATALOG_DATABASE_URL')
        dimensions = getattr(settings, 'SKILL_CATALOG_EMBEDDING_DIMENSIONS', None)
        if dimensions is None:
            dimensions = int(os.getenv('SKILL_CATALOG_EMBEDDING_DIMENSIONS', '384'))
        return cls(dsn, concepts=concepts, embedding_dimensions=dimensions)

    @property
    def configured(self) -> bool:
        return bool(self.dsn)

    async def _get_pool(self) -> Any:
        if not self.dsn:
            return None
        async with self._pool_lock:
            if self._pool is None:
                try:
                    import asyncpg
                except ImportError as exc:
                    raise RuntimeError('PostgreSQL catalog requires asyncpg') from exc
                self._pool = await asyncpg.create_pool(
                    self.dsn, min_size=0, max_size=4, timeout=5, command_timeout=10,
                )
        return self._pool

    async def initialize(self) -> None:
        """Explicit administrative setup; the runtime never creates extensions."""
        pool = await self._get_pool()
        if pool is not None:
            schema = await asyncio.to_thread(Path(__file__).with_name('skill_catalog_schema.sql').read_text,
                                             encoding='utf-8')
            await pool.execute(schema)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def import_concepts(self, items: Iterable[SkillConcept | Mapping[str, Any]], *,
                              replace: bool = False) -> int:
        """Validate the whole import, then atomically upsert it. Failures propagate.

        With no DSN this updates only the local fallback. ``replace`` explicitly
        replaces the entire catalog, including records from other sources.
        """
        concepts = [_concept(item) for item in items]
        if len({c.concept_id for c in concepts}) != len(concepts):
            raise ValueError('duplicate concept_id in catalog import')
        for concept in concepts:
            if concept.embedding is not None:
                _vector(concept.embedding, self.embedding_dimensions)
        pool = await self._get_pool()
        if pool is not None:
            rows = [(c.concept_id, c.label, list(c.aliases), c.definition,
                     list(c.positive_signals), list(c.negative_signals), c.domain,
                     self._vector_text(c.embedding), c.version, c.source,
                     [_normalize(n) for n in (c.label, *c.aliases)], _search_text(c))
                    for c in concepts]
            async with pool.acquire(timeout=5) as connection:
                async with connection.transaction():
                    if replace:
                        await connection.execute('DELETE FROM skill_catalog')
                    await connection.executemany('''
                        INSERT INTO skill_catalog
                        (concept_id,label,aliases,definition,positive_signals,negative_signals,
                         domain,embedding,version,source,search_names,search_text)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8::vector,$9,$10,$11,$12)
                        ON CONFLICT (concept_id) DO UPDATE SET
                        label=EXCLUDED.label, aliases=EXCLUDED.aliases,
                        definition=EXCLUDED.definition, positive_signals=EXCLUDED.positive_signals,
                        negative_signals=EXCLUDED.negative_signals, domain=EXCLUDED.domain,
                        embedding=EXCLUDED.embedding, version=EXCLUDED.version,
                        source=EXCLUDED.source, search_names=EXCLUDED.search_names,
                        search_text=EXCLUDED.search_text, updated_at=now()
                    ''', rows)
        if replace:
            self._concepts.clear()
        self._concepts.update((c.concept_id, c) for c in concepts)
        return len(concepts)

    @staticmethod
    def _vector_text(vector: Sequence[float] | None) -> str | None:
        return None if vector is None else '[' + ','.join(map(str, vector)) + ']'

    @staticmethod
    def _row_concept(row: Mapping[str, Any]) -> SkillConcept:
        values = dict(row)
        values.pop('distance', None)
        embedding = values.get('embedding')
        if isinstance(embedding, str):
            values['embedding'] = [float(v) for v in embedding.strip('[]').split(',')]
        return SkillConcept.from_mapping(values)

    async def search(self, query: str, *, embedding: Sequence[float] | None = None,
                     limit: int = 8, domain: str | None = None) -> list[SkillCatalogHit]:
        if not isinstance(query, str) or len(query) > 10000:
            raise ValueError('query must be a string of at most 10000 characters')
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError('limit must be between 1 and 100')
        vector = None if embedding is None else _vector(embedding, self.embedding_dimensions)
        self.last_search_backend = 'none'
        self.last_fallback_reason = None
        if not query.strip():
            return []
        hits = _lexical_hits(self._concepts.values(), query, limit, domain)
        db_hits: list[SkillCatalogHit] = []
        if self.dsn and not self._database_failed:
            try:
                pool = await self._get_pool()
                if vector is not None:
                    rows = await pool.fetch(f'''
                        SELECT {_COLUMNS}, embedding <=> $1::vector AS distance
                        FROM skill_catalog
                        WHERE embedding IS NOT NULL AND vector_dims(embedding)=$2
                          AND ($3::text IS NULL OR domain=$3)
                        ORDER BY embedding <=> $1::vector, concept_id LIMIT $4
                    ''', self._vector_text(vector), len(vector), domain, limit)
                    if rows:
                        self.last_search_backend = 'postgres_vector'
                        db_hits.extend(SkillCatalogHit(self._row_concept(row),
                                                      1.0 - float(row['distance']), 'vector') for row in rows)
                tokens = list(dict.fromkeys(_TOKEN_RE.findall(_normalize(query))))[:32]
                patterns = ['%' + token.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
                            for token in tokens]
                rows = await pool.fetch(f'''
                    SELECT {_COLUMNS} FROM skill_catalog
                    WHERE ($1::text IS NULL OR domain=$1) AND search_text ILIKE ANY($2::text[])
                    ORDER BY ($3=ANY(search_names)) DESC, concept_id LIMIT 1000
                ''', domain, patterns, _normalize(query))
                lexical = _lexical_hits([self._row_concept(row) for row in rows], query, limit, domain)
                if lexical:
                    self.last_search_backend = 'postgres_hybrid' if db_hits else 'postgres_lexical'
                    db_hits.extend(lexical)
                if not db_hits:
                    self.last_fallback_reason = 'database_no_matches'
            except Exception:
                # Neither query text, catalog content, credentials nor driver error
                # messages should leak into runtime logs.
                logger.warning('Skill catalog database lookup failed; using local fallback')
                self._database_failed = True
                self.last_fallback_reason = 'database_unavailable'
        elif self.dsn:
            self.last_fallback_reason = 'database_unavailable'
        else:
            self.last_fallback_reason = 'database_not_configured'
        if not db_hits:
            self.last_search_backend = 'local_lexical'
        # Explicit names/aliases outrank semantic neighbors. Keep local seed
        # candidates even if only a custom catalog has been imported into PG.
        priority = {'exact': 0, 'vector': 1, 'lexical': 2}
        merged: dict[str, SkillCatalogHit] = {}
        for hit in (*db_hits, *hits):
            previous = merged.get(hit.concept.concept_id)
            rank = (priority[hit.method], -hit.score)
            if previous is None or rank < (priority[previous.method], -previous.score):
                merged[hit.concept.concept_id] = hit
        return sorted(merged.values(), key=lambda h: (priority[h.method], -h.score, h.concept.concept_id))[:limit]
