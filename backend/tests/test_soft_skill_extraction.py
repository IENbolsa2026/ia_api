"""Soft skill evidence and versioning stay within the existing packet protocol."""

import copy
import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.core.config import settings
from app.services.cv_consolidation import validate_and_build_profile
from app.services.cv_packets import PacketsIdentityError, make_packet_identity, new_manifest
from app.services.cv_pipeline import CV_EXTRACTION_PROMPT_VERSION, process_cv_text
from app.services.cv_compact_contract import validate_fragment
from app.services.cv_skill_taxonomy import SkillTaxonomyError, SOFT_SKILL_TAXONOMY


class SoftSkillExtractionTests(unittest.IsolatedAsyncioTestCase):
    async def extract(self, text, skills, *, first_response=None, checkpoints=None):
        packet_calls = 0
        async def provider(_system, payload, marker, max_output_tokens=None):
            nonlocal packet_calls
            self.assertEqual(max_output_tokens, 950)
            if marker.endswith('consolidation-plan'):
                facts = json.loads(payload)['facts']
                return {'h': None, 's': None, **{
                    section: [[item['id']] for item in rows] for section, rows in facts.items()
                }}
            packet_calls += 1
            return {'sk': copy.deepcopy(first_response if packet_calls == 1 and first_response is not None else skills)}

        with patch('app.services.cv_pipeline._call_async', new_callable=AsyncMock, side_effect=provider) as call, \
             patch.object(settings, 'CV_EXTRACTION_MAX_TOKENS', 950), \
             patch.object(settings, 'CV_PACKET_CHARACTERS', 12000), \
             patch.object(settings, 'CV_BLOQUE_CARACTERES', 12000):
            result = await process_cv_text(text, 42, on_checkpoint=checkpoints.append if checkpoints is not None else None)
        self.assertEqual(call.await_count, 2 if first_response is not None else 1)
        return result

    async def test_preserves_inferred_and_declared_soft_skills_with_distinct_evidence(self):
        text = 'Habilidades: Python; Trabajo en equipo. Expliqué procedimientos a usuarios y prioricé entregas.'
        raw = [
            {'n': 'Python', 'k': 'technical', 'o': 'declared', 'e': ['Menciona Python.'], 'q': ['Python']},
            {'n': 'Trabajo en equipo', 'k': 'soft', 'o': 'declared', 'ci': 'soft.teamwork',
             'e': ['Declara trabajo en equipo.'], 'q': ['Trabajo en equipo']},
            {'n': 'Comunicación', 'k': 'soft', 'o': 'inferred', 'ci': 'soft.communication',
             'e': ['Explica procedimientos a usuarios.'], 'q': ['Expliqué procedimientos a usuarios']},
            {'n': 'Gestión del tiempo', 'k': 'soft', 'o': 'inferred', 'ci': 'soft.time_management',
             'e': ['Prioriza entregas.'], 'q': ['prioricé entregas']},
        ]
        # Time management is outside the closed taxonomy. It must trigger the
        # bounded retry, never be relabelled Responsibility from prioritization.
        with self.assertRaises(SkillTaxonomyError) as error:
            validate_fragment({'sk': raw}, text)
        self.assertEqual(error.exception.code, 'unsupported_soft_skill')
        supported = raw[:3]
        checkpoints = []
        result = await self.extract(text, supported, first_response=raw, checkpoints=checkpoints)
        skills = {item['name']: item for item in result['skills']}
        self.assertEqual(set(skills), {'Python', 'Colaboración', 'Comunicación'})
        for item in supported:
            saved = skills['Colaboración' if item['n'] == 'Trabajo en equipo' else item['n']]
            self.assertEqual(saved['kind'], item['k'])
            self.assertEqual(saved['origin'], item['o'])
            self.assertEqual(saved['source_quotes'], item['q'])
            self.assertEqual(saved['evidence'], item['e'])
            self.assertEqual(saved['level'], 'unknown')
        self.assertEqual(skills['Colaboración']['concept_id'], 'soft.collaboration')
        self.assertNotIn('Responsabilidad', skills)
        self.assertNotIn('Gestión del tiempo', json.dumps(checkpoints, ensure_ascii=False))

    async def test_poor_source_does_not_gain_soft_skills_to_satisfy_a_ratio(self):
        text = 'Desarrollador backend. Dominios: comercio y ambiente. Python, Linux, DNS.'
        result = await self.extract(text, [
            {'n': name, 'k': 'technical', 'o': 'declared', 'e': [f'Menciona {name}.'], 'q': [name]}
            for name in ('Python', 'Linux', 'DNS')
        ])
        self.assertEqual(len(result['skills']), 3)
        self.assertFalse(any(item['kind'] == 'soft' for item in result['skills']))

    async def test_preserves_multisector_evidence_and_does_not_add_skills_from_titles_or_tools(self):
        cases = json.loads((Path(__file__).parent / 'fixtures' / 'cv_multisector_skills.json').read_text(encoding='utf-8'))
        for case in cases:
            with self.subTest(sector=case['sector']):
                rejected = case.get('rejected_skills')
                if rejected:
                    with self.assertRaises(SkillTaxonomyError):
                        validate_fragment({'sk': rejected}, case['text'])
                result = await self.extract(case['text'], case['skills'], first_response=rejected)
                self.assertEqual(len(result['skills']), len(case['skills']))
                for saved, expected in zip(result['skills'], case['skills']):
                    self.assertEqual(saved['name'], expected['n'])
                    self.assertEqual(saved['kind'], expected['k'])
                    self.assertEqual(saved['origin'], expected['o'])
                    self.assertEqual(saved['level'], 'unknown')
                    self.assertEqual(saved['source_quotes'], expected['q'])
                    self.assertEqual(saved['evidence'], expected['e'])
                    self.assertEqual(saved['concept_id'], expected.get('ci'))
                    if saved['kind'] == 'soft':
                        self.assertIn(saved['name'], SOFT_SKILL_TAXONOMY)
                if case['sector'] == 'títulos y herramientas sin acciones':
                    self.assertFalse(any(skill['kind'] == 'soft' for skill in result['skills']))

    async def test_repeated_out_of_taxonomy_response_fails_without_publishing_a_skill(self):
        text = 'Priorizo mis tareas antes del vencimiento.'
        raw = [{'n': 'Gestión del tiempo', 'k': 'soft', 'o': 'inferred',
                'e': ['Prioriza tareas antes del vencimiento.'], 'q': [text]}]
        checkpoints = []
        with patch('app.services.cv_pipeline._call_async', new_callable=AsyncMock, return_value={'sk': raw}) as provider:
            with self.assertRaises(SkillTaxonomyError):
                await process_cv_text(text, 42, on_checkpoint=checkpoints.append)
        self.assertEqual(provider.await_count, 2)
        self.assertFalse(any(node['status'] == 'completed' for snapshot in checkpoints for node in snapshot['packets'].values()))

    async def test_old_prompt_identity_and_checkpoint_cannot_mix_with_new_extraction(self):
        text = 'Synthetic source.'
        old = make_packet_identity(
            job_id='synthetic-old', analysis_id=1, candidate_id=42, cv_version_id=1,
            source_hash=hashlib.sha256(text.encode()).hexdigest(), text=text,
            prompt_version='cv-extraction.v2-packets-soft.v1', provider=settings.LLM_PROVIDER,
            model=settings.OPENAI_MODEL if settings.LLM_PROVIDER == 'openai' else settings.CV_MODEL,
            packet_chars=settings.CV_PACKET_CHARACTERS, block_chars=settings.CV_BLOQUE_CARACTERES,
            budget=settings.CV_EXTRACTION_MAX_TOKENS,
        )
        current = {**old, 'prompt_version': CV_EXTRACTION_PROMPT_VERSION}
        for kwargs in ({'identity': old}, {'identity': current, 'checkpoint': new_manifest(text, old)}):
            with self.subTest(kwargs=kwargs):
                with patch('app.services.cv_pipeline._call_async', new_callable=AsyncMock) as provider:
                    with self.assertRaises(PacketsIdentityError):
                        await process_cv_text(text, 42, **kwargs)
                provider.assert_not_called()

    def test_consolidation_merges_exact_ai_concept_but_keeps_dns_administration_distinct(self):
        fragments = [{'sk': [
            {'n': 'IA', 'k': 'technical', 'ci': 'technical.artificial_intelligence', 'e': ['Menciona IA'], 'q': ['IA']},
            {'n': 'Inteligencia artificial', 'k': 'technical', 'ci': 'technical.artificial_intelligence',
             'e': ['Menciona inteligencia artificial'], 'q': ['Inteligencia artificial']},
            {'n': 'DNS', 'k': 'technical', 'e': ['Menciona DNS'], 'q': ['DNS']},
            {'n': 'Administración de DNS', 'k': 'technical', 'e': ['Administra DNS'], 'q': ['Administración de DNS']},
        ]}]
        result = validate_and_build_profile({
            'h': None, 's': None, 'sk': [['sk0', 'sk1'], ['sk2', 'sk3']],
            'x': [], 'ed': [], 'l': [], 'c': [],
        }, fragments, 42)
        self.assertEqual(len(result['skills']), 3)
        self.assertEqual(result['skills'][0]['source_quotes'], ['IA', 'Inteligencia artificial'])
        self.assertEqual([item['name'] for item in result['skills'][1:]], ['DNS', 'Administración de DNS'])
