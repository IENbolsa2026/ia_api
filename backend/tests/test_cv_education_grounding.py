import copy
import json
import unittest
from unittest.mock import AsyncMock, patch

from app.core.config import settings
from app.services.cv_compact_contract import validate_fragment
from app.services.cv_consolidation import validate_and_build_profile
from app.services.cv_education_guard import ground_education, safe_academic_narrative
from app.services.cv_pipeline import process_cv_text


class AcademicGroundingTests(unittest.TestCase):
    def test_status_requires_explicit_source_and_never_uses_dates_as_graduation(self):
        cases = [
            ('Doctorado en Informática, en curso.', 'in_progress', False),
            ('Doctorado en Informática, 2025–actualidad.', 'in_progress', False),
            ('Doctoranda en Informática.', 'in_progress', False),
            ('PhD student in Computer Science.', 'in_progress', False),
            ('Doctorado en Informática, tesis pendiente.', 'incomplete', False),
            ('Doctorado en Informática: cursada completa, defensa pendiente.', 'incomplete', False),
            ('Doctorado en Informática, abandonado.', 'incomplete', False),
            ('Doctorado en Informática 2019–2025.', 'unknown', None),
            ('Doctorado en Informática, graduada en 2025.', 'completed', True),
            ('Doctorate in Computer Science, completed in 2025.', 'completed', True),
        ]
        for source, expected, completed in cases:
            with self.subTest(source=source):
                row = {'level': 'doctorate', 'completed': True, 'source_quotes': [source]}
                grounded = ground_education([row], source)[0]
                self.assertEqual(grounded['completion_status'], expected)
                self.assertIs(grounded['completed'], completed)
                self.assertTrue(row['completed'])  # Does not mutate provider/cache input.

    def test_education_needs_a_literal_quote_for_the_relevant_level(self):
        for quotes in ([], ['Doctorado completado.'], ['Licenciatura en curso.']):
            with self.subTest(quotes=quotes), self.assertRaises(ValueError):
                ground_education([{'level': 'doctorate', 'source_quotes': quotes}], 'Doctorado en curso. Licenciatura en curso.')

    def test_in_progress_bachelor_does_not_inherit_completed_master(self):
        source = 'Maestría en Gestión, graduada. Licenciatura en Enfermería, en curso.'
        result = ground_education([
            {'level': 'master', 'source_quotes': ['Maestría en Gestión, graduada.']},
            {'level': 'bachelor', 'source_quotes': ['Licenciatura en Enfermería, en curso.']},
        ], source)
        self.assertTrue(result[0]['completed'])
        self.assertFalse(result[1]['completed'])

    def test_quote_span_with_multiple_degrees_keeps_each_completion_separate(self):
        source = 'Maestría en Gestión, graduada. Doctorado en Informática, en curso.'
        rows = ground_education([
            {'level': 'master', 'source_quotes': [source]},
            {'level': 'doctorate', 'source_quotes': [source]},
        ], source)
        self.assertTrue(rows[0]['completed'])
        self.assertFalse(rows[1]['completed'])

    def test_same_level_different_fields_do_not_share_a_graduation(self):
        for source in ('Doctorado en Historia completado. Doctorado en Informática.',
                       'Doctorado en Historia completado y Doctorado en Informática.'):
            with self.subTest(source=source):
                rows = ground_education([
                    {'level': 'doctorate', 'field': 'Informática', 'source_quotes': [source]},
                ], source)
                self.assertIsNone(rows[0]['completed'])
                self.assertEqual(rows[0]['completion_status'], 'unknown')

    def test_pending_thesis_keeps_explicit_current_candidacy_without_awarding_degree(self):
        source = 'Doctorado en Informática en curso; tesis pendiente.'
        row = ground_education([{'level': 'doctorate', 'field': 'Informática', 'source_quotes': [source]}], source)[0]
        self.assertFalse(row['completed'])
        self.assertEqual(row['completion_status'], 'in_progress')

    def test_generated_degree_claim_is_removed_even_with_other_ongoing_training(self):
        profile = {'headline': 'Doctor en Informática',
            'summary': 'Es Doctor en Informática. Además cursa un programa en curso.',
            'education': [{'level': 'doctorate', 'field': 'Informática', 'completed': False, 'completion_status': 'in_progress'}]}
        original = copy.deepcopy(profile)
        safe = safe_academic_narrative(profile)
        self.assertIsNone(safe['headline'])
        self.assertIsNone(safe['summary'])
        self.assertEqual(profile, original)
        profile.update(headline='Estudiante de doctorado en Informática', summary='Cursa un doctorado en Informática.')
        self.assertEqual(safe_academic_narrative(profile), profile)
        profile['education'][0]['completed'] = True
        profile['headline'] = 'Doctor en Informática'
        self.assertEqual(safe_academic_narrative(profile)['headline'], 'Doctor en Informática')

    def test_completed_doctorate_in_another_field_cannot_support_claim(self):
        profile = {'headline': 'Doctor en Informática', 'education': [
            {'level': 'doctorate', 'field': 'Historia', 'completed': True},
            {'level': 'doctorate', 'field': 'Informática', 'completed': False},
        ]}
        self.assertIsNone(safe_academic_narrative(profile)['headline'])

    def test_fragment_keeps_study_state_and_cannot_treat_a_course_as_a_skill(self):
        source = 'Doctorado en Informática (en curso). Curso de Excel.'
        expanded, compact = validate_fragment({'h': 'Doctor en Informática', 'ed': [{
            'l': 'doctorate', 'f': 'Informática', 'c': True,
            'q': ['Doctorado en Informática (en curso).'],
        }]}, source)
        self.assertIsNone(expanded['headline'])
        self.assertFalse(expanded['education'][0]['completed'])
        self.assertEqual(compact['ed'][0]['st'], 'in_progress')
        with self.assertRaises(ValueError):
            validate_fragment({'sk': [{'n': 'Curso de Excel', 'k': 'technical',
                'e': ['Menciona un curso'], 'q': ['Curso de Excel.']}]}, source)

    def test_conflicting_academic_completion_facts_are_kept_separate(self):
        fragments = [{'ed': [
            {'l': 'doctorate', 'f': 'Informática', 'c': False, 'st': 'in_progress', 'q': ['Doctorado en curso.']},
            {'l': 'doctorate', 'f': 'Informática', 'c': True, 'st': 'completed', 'q': ['Doctorado finalizado.']},
        ]}]
        result = validate_and_build_profile({'sk': [], 'x': [], 'ed': [['ed0', 'ed1']], 'l': [], 'c': []}, fragments, 42)
        self.assertEqual(len(result['education']), 2)
        self.assertEqual([row['completed'] for row in result['education']], [False, True])

    def test_pending_certification_is_not_an_awarded_credential(self):
        source = 'Certificación en gestión: en curso.'
        with self.assertRaisesRegex(ValueError, 'credencial obtenida'):
            validate_fragment({'c': [{'n': 'Certificación en gestión',
                'e': ['Título obtenido'], 'q': [source]}]}, source)

    def test_another_graduation_does_not_award_a_pending_credential(self):
        source = 'Licenciatura en Letras completada. Certificado de primeros auxilios en curso.'
        with self.assertRaisesRegex(ValueError, 'credencial obtenida'):
            validate_fragment({'c': [{'n': 'Certificado de primeros auxilios',
                'e': ['Certificado obtenido'], 'q': [source]}]}, source)


class AcademicPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_degree_state_and_closed_soft_taxonomy_survive_pipeline_and_checkpoint(self):
        text = 'Doctorado en Informática (en curso). Cooperé con colegas para entregar el trabajo. Curso de Excel (en curso).'
        checkpoints = []

        async def provider(_system, payload, marker, **kwargs):
            request = json.loads(payload)
            catalog = request['source_quotes']
            quote = next(iter(catalog))
            return {'h': 'Doctor en Informática', 's': 'Cursa un doctorado en Informática.',
                'sk': [{'n': 'Trabajo en equipo', 'k': 'soft', 'e': ['Coopera con colegas'], 'q': [quote]}],
                'ed': [{'l': 'doctorate', 'f': 'Informática', 'c': True, 'q': [quote]}]}

        with patch.object(settings, 'CV_PACKET_CHARACTERS', 12000), \
             patch.object(settings, 'CV_BLOQUE_CARACTERES', 12000), \
             patch('app.services.cv_pipeline._call_async', new_callable=AsyncMock, side_effect=provider):
            result = await process_cv_text(text, 42, on_checkpoint=checkpoints.append)
        self.assertIsNone(result['headline'])
        self.assertFalse(result['education'][0]['completed'])
        self.assertEqual(result['education'][0]['completion_status'], 'in_progress')
        self.assertEqual([skill['name'] for skill in result['skills']], ['Colaboración'])
        self.assertEqual(checkpoints[-1]['consolidation']['result'], result)
