"""Catalog retrieval is bounded, optional, parameterized and importable offline."""

import csv
import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app.services.skill_catalog_store import SkillCatalogStore, SkillConcept
from tools.import_skill_catalog import _embed_concepts, _run, load_concepts


CATALOG = [
    {'concept_id': 'tech.python', 'label': 'Python', 'aliases': ['Python 3'],
     'definition': 'Programación con Python', 'domain': 'software'},
    {'concept_id': 'tech.cpp', 'label': 'C++', 'domain': 'software'},
    {'concept_id': 'tech.csharp', 'label': 'C#', 'domain': 'software'},
    {'concept_id': 'tech.c', 'label': 'C', 'domain': 'software'},
    {'concept_id': 'tech.net', 'label': '.NET', 'domain': 'software'},
    {'concept_id': 'soft.communication', 'label': 'Comunicación',
     'aliases': ['communication'], 'definition': 'Explicar ideas claramente',
     'positive_signals': ['Presenté resultados'], 'negative_signals': ['Solo asistí'],
     'domain': 'soft', 'version': '2', 'source': 'test'},
]


def db_row(**overrides):
    row = SkillConcept.from_mapping(CATALOG[0]).to_dict()
    row.update(overrides)
    return row


class SkillCatalogStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_from_env_uses_central_settings_including_dotenv(self):
        from app.core.config import settings

        with patch.object(settings, 'SKILL_CATALOG_DATABASE_URL', 'postgresql://dotenv/catalog'), \
             patch.object(settings, 'SKILL_CATALOG_EMBEDDING_DIMENSIONS', 3):
            store = SkillCatalogStore.from_env(concepts=CATALOG)
        self.assertEqual(store.dsn, 'postgresql://dotenv/catalog')
        self.assertEqual(store.embedding_dimensions, 3)

    async def test_explicit_embedding_import_and_standalone_prewarm(self):
        concept = SkillConcept.from_mapping({**CATALOG[-1], 'embedding': [0, 1, 0]})
        with patch('tools.import_skill_catalog.embed_passages', new_callable=AsyncMock) as embed:
            embed.return_value = [[1.0, 0.0, 0.0]]
            embedded = await _embed_concepts([concept])
            self.assertEqual(embedded[0].embedding, (1, 0, 0))
            self.assertIn('Positive signals: Presenté resultados', embed.call_args.args[0][0])
            self.assertIn('Negative signals: Solo asistí', embed.call_args.args[0][0])
            self.assertTrue(embed.call_args.kwargs['allow_download'])
            self.assertEqual(concept.embedding, (0, 1, 0))
            count = await _run(argparse.Namespace(path=None, prewarm=True, embed=False))
            self.assertEqual(count, 0)

    async def test_no_database_and_exact_technical_tokens(self):
        store = SkillCatalogStore(concepts=CATALOG)
        for text, expected in [('Python', 'tech.python'), ('C++', 'tech.cpp'),
                               ('C#', 'tech.csharp'), ('.NET', 'tech.net'),
                               ('Trabajo en C++', 'tech.cpp'), ('Python 3', 'tech.python')]:
            with self.subTest(text=text):
                hits = await store.search(text)
                self.assertEqual([h.concept.concept_id for h in hits], [expected])
                self.assertEqual(hits[0].method, 'exact')
        self.assertEqual(await store.search('Pythonista'), [])
        self.assertEqual(store.last_search_backend, 'local_lexical')
        self.assertEqual(store.last_fallback_reason, 'database_not_configured')

    async def test_domain_accent_alias_metadata_and_bounds(self):
        store = SkillCatalogStore(concepts=CATALOG)
        hit = (await store.search('COMUNICACION', domain='soft'))[0]
        self.assertEqual(hit.concept.version, '2')
        self.assertEqual(hit.concept.source, 'test')
        self.assertEqual(hit.to_dict()['negative_signals'], ['Solo asistí'])
        self.assertEqual(await store.search('communication', domain='software'), [])
        for limit in (0, 101, True, '1'):
            with self.assertRaises(ValueError):
                await store.search('python', limit=limit)

    async def test_atomic_validation_and_replace_in_memory(self):
        store = SkillCatalogStore(concepts=CATALOG, embedding_dimensions=3)
        with self.assertRaises(ValueError):
            await store.import_concepts([{'concept_id': 'x', 'label': 'X'}, {'label': 'missing id'}], replace=True)
        self.assertTrue(await store.search('Python'))
        with self.assertRaises(ValueError):
            await store.import_concepts([{'concept_id': 'x', 'label': 'X', 'embedding': [1, 2]}])
        with self.assertRaises(ValueError):
            await store.import_concepts([CATALOG[0], CATALOG[0]])
        self.assertEqual(await store.import_concepts([CATALOG[-1]], replace=True), 1)
        self.assertEqual(await store.search('Python'), [])

    async def test_vector_and_lexical_merge_exact_first_and_keep_local_seed(self):
        store = SkillCatalogStore('postgresql://unused', concepts=CATALOG, embedding_dimensions=3)
        pool = AsyncMock()
        pool.fetch.side_effect = [
            [db_row(concept_id='custom.semantic', label='Programación', embedding='[1,0,0]', distance=0.01),
             db_row(embedding='[1,0,0]', distance=0.4)],
            [db_row()],
        ]
        store._pool = pool
        hits = await store.search('Python', embedding=[1, 0, 0])
        self.assertEqual([h.concept.concept_id for h in hits], ['tech.python', 'custom.semantic'])
        self.assertEqual([h.method for h in hits], ['exact', 'vector'])
        self.assertEqual(store.last_search_backend, 'postgres_hybrid')
        self.assertIsNone(store.last_fallback_reason)
        # A seed-only alias remains available when a custom PG catalog is present.
        pool.fetch.side_effect = [[db_row(concept_id='custom.semantic', label='Programación',
                                         embedding='[1,0,0]', distance=0.01)], []]
        hits = await store.search('communication', embedding=[1, 0, 0])
        self.assertEqual(hits[0].concept.concept_id, 'soft.communication')

    async def test_database_error_falls_back_without_logging_secrets(self):
        store = SkillCatalogStore('postgresql://unused', concepts=CATALOG)
        store._pool = AsyncMock()
        store._pool.fetch.side_effect = RuntimeError('secret password and CV text')
        with self.assertLogs('app.services.skill_catalog_store', level='WARNING') as logs:
            hits = await store.search('Python')
        self.assertEqual(hits[0].concept.concept_id, 'tech.python')
        self.assertEqual(store.last_fallback_reason, 'database_unavailable')
        self.assertNotIn('secret', ' '.join(logs.output))
        self.assertEqual(len(store._pool.fetch.call_args_list), 1)
        self.assertTrue(await store.search('Python'))
        self.assertEqual(len(store._pool.fetch.call_args_list), 1)

    async def test_query_text_is_bound_as_data(self):
        store = SkillCatalogStore('postgresql://unused', embedding_dimensions=3)
        store._pool = AsyncMock()
        store._pool.fetch.return_value = []
        attack = "python'); DROP TABLE skill_catalog; --"
        await store.search(attack, embedding=[1, 0, 0], domain=attack)
        for call in store._pool.fetch.call_args_list:
            self.assertNotIn(attack, call.args[0])
            self.assertIn(attack, call.args[1:])
        vector_call = store._pool.fetch.call_args_list[0]
        self.assertIn('ORDER BY embedding <=> $1::vector', vector_call.args[0])
        self.assertEqual(vector_call.args[1], '[1.0,0.0,0.0]')

    async def test_invalid_vectors_rejected_before_database_access(self):
        store = SkillCatalogStore(embedding_dimensions=3)
        for vector in ([], [0, 0, 0], [1, float('nan'), 0], [1, float('inf'), 0], [True, 0, 0], [1, 2]):
            with self.subTest(vector=vector), self.assertRaises(ValueError):
                await store.search('Python', embedding=vector)


class SkillCatalogImportTests(unittest.TestCase):
    def test_json_and_csv_round_trip_custom_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'catalog.json'
            path.write_text(json.dumps({'concepts': CATALOG}), encoding='utf-8')
            self.assertEqual(len(load_concepts(path)), len(CATALOG))
            path = Path(directory) / 'catalog.csv'
            with path.open('w', encoding='utf-8', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=['concept_id', 'label', 'aliases', 'embedding'])
                writer.writeheader()
                writer.writerow({'concept_id': 'custom.sql', 'label': 'SQL',
                                 'aliases': 'Structured Query Language|Consultas SQL',
                                 'embedding': '[1, 0, 0]'})
            concept = load_concepts(path)[0]
            self.assertEqual(concept.aliases, ('Structured Query Language', 'Consultas SQL'))
            self.assertEqual(concept.embedding, (1, 0, 0))

    def test_schema_contains_all_catalog_fields_and_vector(self):
        path = Path(__file__).parents[1] / 'app/services/skill_catalog_schema.sql'
        sql = path.read_text(encoding='utf-8')
        for field in SkillConcept.__dataclass_fields__:
            self.assertIn(field, sql)
        self.assertIn('CREATE EXTENSION IF NOT EXISTS vector', sql)


if __name__ == '__main__':
    unittest.main()
