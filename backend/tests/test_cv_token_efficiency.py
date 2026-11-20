"""Token savings must not lose source, evidence, identity or resumable progress."""

import copy
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.core.config import settings
from app.services.cv_compact_contract import fragment_schema, validate_expanded_profile
from app.services.cv_consolidation import local_consolidation_plan, validate_and_build_profile
from app.services.cv_packets import (
    CV_EFFICIENT_PROMPT_VERSION, CV_PREVIOUS_PROMPT_VERSION, CVProfileValidationError,
    PacketsIdentityError, _quote_catalog, _resolve_quote_ids, make_packet_identity,
    process_packetized_cv_text, validate_manifest,
)
from app.services.cv_pipeline import process_cv_text
from app.services.cv_request_budget import annotated_source, prepare_gemini_input
from app.services.rate_limiter import ProviderInputLimitExceeded
from app.services.skill_catalog_store import SkillCatalogStore
from app.services.skill_context import build_cv_skill_context, seed_concepts
from app.services.document_extractor_runner import extract_in_subprocess


def synthetic_text(size):
    return ''.join(f'Proyecto {i:04d}: Desarrollo de aplicaciones en Python y Django. '
                   'Implementación de APIs, pruebas, SQL y documentación para clientes.\n'
                   for i in range(1000))[:size]


def identity(text, *, version=CV_EFFICIENT_PROMPT_VERSION):
    return make_packet_identity(
        job_id='synthetic-efficient', analysis_id=11, candidate_id=42, cv_version_id=12,
        source_hash='a' * 64, text=text, prompt_version=version,
        provider='gemini', model='synthetic-model', packet_chars=20000, block_chars=20000, budget=16384,
    )


def synthetic_pdf(pages=20):
    """Small valid ASCII PDF fixture, generated without document libraries."""
    page_ids = [4 + index * 2 for index in range(pages)]
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               f'<< /Type /Pages /Count {pages} /Kids [{" ".join(f"{n} 0 R" for n in page_ids)}] >>'.encode(),
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    for index, page_id in enumerate(page_ids):
        objects.append(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] '
                       f'/Resources << /Font << /F1 3 0 R >> >> /Contents {page_id + 1} 0 R >>'.encode())
        lines = [f'Page {index + 1:02d} task {row:02d}: I used Python and Django to maintain APIs '
                 'and automated SQL reports for clients.' for row in range(40)]
        stream = ('BT /F1 9 Tf 40 760 Td 16 TL\n' +
                  '\n'.join(f'({line}) Tj T*' for line in lines) + '\nET').encode('ascii')
        objects.append(f'<< /Length {len(stream)} >>\nstream\n'.encode() + stream + b'\nendstream')
    result = bytearray(b'%PDF-1.4\n')
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f'{index} 0 obj\n'.encode() + obj + b'\nendobj\n')
    xref = len(result)
    result.extend(f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode())
    result.extend(''.join(f'{offset:010d} 00000 n \n' for offset in offsets[1:]).encode())
    result.extend(f'trailer << /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    return bytes(result)


class EfficientTransportTests(unittest.TestCase):
    def test_annotation_is_lossless_including_whitespace_unicode_and_repeated_text(self):
        source = '  👩‍💻 Perfil\nTecnicatura en Desarrollo de\nSoftware (En curso).\nPython.\nPython.\t尾'
        catalog = _quote_catalog(source)
        rendered = annotated_source(source, catalog)
        self.assertEqual(re.sub(r'\[/?q\d+\]', '', rendered), source)
        # The wrapped program/state quote wins over its shorter first line.
        self.assertIn('Tecnicatura en Desarrollo de\nSoftware (En curso).[/q', rendered)
        labels = re.findall(r'\[(q\d+)\]', rendered)
        resolved = _resolve_quote_ids({'ed': [{'q': labels}]}, catalog)
        self.assertTrue(all(quote in source for quote in resolved['ed'][0]['q']))

    def test_native_schema_and_source_are_sent_once_and_input_includes_schema(self):
        source = 'Uso Python.\n' * 50
        payload = {'packet_id': 'synthetic', 'text': source, 'source_quotes': _quote_catalog(source),
                   'source_format': 'annotated-quotes.v1', 'fragment_schema': fragment_schema()}
        content, schema, estimate = prepare_gemini_input('system', json.dumps(payload))
        sent = json.loads(content)
        self.assertEqual(set(sent), {'packet_id', 'source_text'})
        self.assertEqual(re.sub(r'\[/?q\d+\]', '', sent['source_text']), source)
        self.assertIsInstance(schema, dict)
        self.assertGreater(estimate, len(content) // 2)
        # An unrelated request must not be reserialized or given a CV grammar.
        plain = '{ "candidate_profile": {} }'
        self.assertEqual(prepare_gemini_input('system', plain)[:2], (plain, None))


class LocalConsolidationTests(unittest.TestCase):
    def test_distinct_durations_for_same_job_are_not_discarded(self):
        fragments = [{'x': [{'t': 'Auxiliar', 'o': 'Empresa', 'm': months}]} for months in (12, 24)]
        profile = validate_and_build_profile(local_consolidation_plan(fragments), fragments, 42)
        self.assertEqual([row['duration_months'] for row in profile['work_experience']], [12, 24])

    def test_cross_packet_duplicates_merge_without_losing_quotes_preferences_or_projects(self):
        fragments = [
            {'sk': [{'n': 'Python', 'k': 'technical', 'e': ['Practica Python'], 'q': ['Uso Python.']}],
             'pr': [{'n': 'Portal', 'q': ['Proyecto Portal.']}],
             'p': {'l': ['Rosario'], 'w': ['remote']}},
            {'sk': [{'n': 'Python', 'k': 'technical', 'e': ['Usa Python'], 'q': ['Desarrollo con Python.']}],
             'pr': [{'n': 'Portal', 'q': ['Portal con Python.']}],
             'p': {'l': ['Córdoba'], 'w': ['hybrid']}},
        ]
        plan = local_consolidation_plan(fragments)
        self.assertEqual(plan['sk'], [['sk0', 'sk1']])
        self.assertEqual(plan['pr'], [['pr0', 'pr1']])
        profile = validate_and_build_profile(plan, fragments, 42)
        self.assertEqual(profile['skills'][0]['source_quotes'], ['Uso Python.', 'Desarrollo con Python.'])
        self.assertEqual(profile['projects'][0]['source_quotes'], ['Proyecto Portal.', 'Portal con Python.'])
        self.assertEqual(profile['preferences']['locations'], ['Rosario', 'Córdoba'])

    def test_conflicts_and_partial_identity_never_force_a_merge(self):
        fragments = [{'ed': [
            {'l': 'bachelor', 'f': 'Historia', 'c': False, 'st': 'in_progress', 'q': ['Cursa Historia.']},
            {'l': 'bachelor', 'f': 'Historia', 'c': True, 'st': 'completed', 'q': ['Historia finalizada.']},
        ], 'x': [{'t': 'Auxiliar', 'h': ['Ventas']}, {'t': 'Auxiliar', 'h': ['Administración']}],
            'sk': [{'n': 'Python', 'l': 'beginner'}, {'n': 'Python', 'l': 'advanced'}]}]
        plan = local_consolidation_plan(fragments)
        for section in ('ed', 'x', 'sk'):
            self.assertEqual(plan[section], [[f'{section}0'], [f'{section}1']])
        profile = validate_and_build_profile(plan, fragments, 42)
        self.assertEqual([row['completed'] for row in profile['education']], [False, True])

    def test_evidence_bounds_keep_all_facts_in_separate_groups(self):
        fragments = [{'sk': [{'n': 'Python', 'q': [f'Cita {i}-{j}' for j in range(5)]}]} for i in range(3)]
        plan = local_consolidation_plan(fragments)
        self.assertEqual(plan['sk'], [['sk0'], ['sk1'], ['sk2']])
        profile = validate_and_build_profile(plan, fragments, 42)
        self.assertEqual(sum(len(row['source_quotes']) for row in profile['skills']), 15)

    def test_many_soft_skill_quotes_remain_in_valid_bounded_records_without_losing_evidence(self):
        quotes = [f'Colaboré con el equipo en la tarea {i}.' for i in range(10)]
        fragments = [{'sk': [{'n': 'Colaboración', 'k': 'soft', 'ci': 'soft.collaboration',
                              'e': ['Colabora con su equipo.'], 'q': quotes[start:start + 5]}]}
                     for start in (0, 5)]
        plan = local_consolidation_plan(fragments)
        profile = validate_and_build_profile(plan, fragments, 42)
        result = validate_expanded_profile(profile, '\n'.join(quotes), 42, preserve_skill_groups=True)
        self.assertEqual(len(result['skills']), 2)
        self.assertEqual([q for row in result['skills'] for q in row['source_quotes']], quotes)
        self.assertTrue(all(len(row['source_quotes']) <= 8 for row in result['skills']))


class EfficientPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_twenty_page_pdf_parses_in_child_and_completes_with_mocked_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'synthetic-twenty-pages.pdf'
            path.write_bytes(synthetic_pdf())
            text = extract_in_subprocess(path, 'pdf')
        self.assertGreater(len(text), 60000)
        self.assertIn('Page 20 task 39', text)
        calls = []
        async def provider(system, payload, marker, **kwargs):
            self.assertFalse(marker.endswith('consolidation-plan'))
            estimate = prepare_gemini_input(system, payload)[2]
            if estimate > 20000:
                raise ProviderInputLimitExceeded(estimate, 20000)
            calls.append(estimate)
            return {'sk': [{'n': 'Python', 'k': 'technical',
                            'e': ['Usa Python.'], 'q': ['Python']}]}
        profile, _model = await process_packetized_cv_text(
            text, 42, identity=identity(text), checkpoint=None, on_checkpoint=None,
            on_heartbeat=None, call_provider=provider,
        )
        self.assertLessEqual(len(calls), 10)
        self.assertEqual([row['name'] for row in profile['skills']], ['Python'])

    async def test_long_cv_stays_bounded_consolidates_locally_and_resumes_without_provider(self):
        text = synthetic_text(100000)
        snapshots, admitted = [], []

        async def provider(system, payload, marker, **kwargs):
            self.assertFalse(marker.endswith('consolidation-plan'))
            _content, _schema, estimate = prepare_gemini_input(system, payload)
            if estimate > 20000:
                raise ProviderInputLimitExceeded(estimate, 20000)
            admitted.append(estimate)
            return {'sk': [{'n': 'Python', 'k': 'technical',
                            'e': ['Menciona Python.'], 'q': ['Python']}]}

        profile, _model = await process_packetized_cv_text(
            text, 42, identity=identity(text), checkpoint=None, on_checkpoint=snapshots.append,
            on_heartbeat=None, call_provider=provider,
        )
        self.assertLessEqual(len(admitted), 7)  # Baseline: 21 extraction requests, plus a merge call.
        self.assertLess(sum(admitted), 120000)  # Baseline estimate: 344952 excluding merge/retries.
        self.assertEqual([row['name'] for row in profile['skills']], ['Python'])
        self.assertEqual(snapshots[-1]['consolidation']['status'], 'completed')
        async def forbidden(*args, **kwargs):
            self.fail('Resuming a saved result must not spend more tokens')
        restored, _ = await process_packetized_cv_text(
            text, 42, identity=identity(text), checkpoint=snapshots[-1], on_checkpoint=None,
            on_heartbeat=None, call_provider=forbidden,
        )
        self.assertEqual(restored, profile)
        # A crash after the last leaf save still needs no extra provider call.
        restored, _ = await process_packetized_cv_text(
            text, 42, identity=identity(text), checkpoint=snapshots[-2], on_checkpoint=None,
            on_heartbeat=None, call_provider=forbidden,
        )
        self.assertEqual(restored, profile)
        tampered = copy.deepcopy(snapshots[-1])
        tampered['consolidation']['result']['skills'] = []
        with self.assertRaises(PacketsIdentityError):
            validate_manifest(tampered, text, identity(text))

    async def test_invalid_packet_stops_after_two_responses_without_whole_task_retries(self):
        provider = AsyncMock(return_value={'sk': 'invalid'})
        with self.assertRaises(CVProfileValidationError):
            await process_packetized_cv_text(
                'Uso Python.', 42, identity=identity('Uso Python.'), checkpoint=None,
                on_checkpoint=None, on_heartbeat=None, call_provider=provider,
            )
        self.assertEqual(provider.await_count, 2)

    async def test_fragment_fact_overflow_splits_without_paying_for_a_second_parent_response(self):
        text = 'Uso Python en proyectos de desarrollo.\n' * 7
        sizes = []
        async def provider(_system, payload, _marker, **kwargs):
            chunk = json.loads(payload)['text']
            sizes.append(len(chunk))
            row = {'n': 'Python', 'k': 'technical', 'e': ['Usa Python.'], 'q': ['Python']}
            return {'sk': [row] * 101 if len(chunk) > 200 else [row]}
        result, _model = await process_packetized_cv_text(
            text, 42, identity=identity(text), checkpoint=None, on_checkpoint=None,
            on_heartbeat=None, call_provider=provider,
        )
        self.assertEqual(len(sizes), 3)
        self.assertEqual(sizes.count(len(text)), 1)
        self.assertEqual(len(result['skills']), 1)

    async def test_previous_prompt_checkpoint_can_finish_without_reextracting_its_leaf(self):
        text = 'Synthetic source.'
        snapshots = []
        old_identity = identity(text, version=CV_PREVIOUS_PROMPT_VERSION)
        async def provider(*args, **kwargs):
            return {}
        await process_packetized_cv_text(
            text, 42, identity=old_identity, checkpoint=None, on_checkpoint=snapshots.append,
            on_heartbeat=None, call_provider=provider,
        )
        with patch.object(settings, 'LLM_PROVIDER', 'gemini'), \
             patch.object(settings, 'GEMINI_MODEL', 'synthetic-model'), \
             patch.object(settings, 'CV_PACKET_CHARACTERS', 20000), \
             patch.object(settings, 'CV_BLOQUE_CARACTERES', 20000), \
             patch.object(settings, 'CV_EXTRACTION_MAX_TOKENS', 16384), \
             patch('app.services.cv_pipeline._call_cv_provider', side_effect=AssertionError('No new provider call')):
            for snapshot in snapshots[-2:]:
                restored = await process_cv_text(text, 42, identity=old_identity, checkpoint=snapshot)
                self.assertEqual(restored['profile_id'], '42')

    async def test_compact_rag_is_bounded_and_does_not_repeat_full_taxonomy(self):
        store = SkillCatalogStore(concepts=seed_concepts())
        context = await build_cv_skill_context('Uso Python, Django, SQL y Excel.', store=store, compact=True)
        self.assertLessEqual(len(context), 2000)
        self.assertIn('Python', context)
        self.assertNotIn('## Habilidades blandas', context)
