import copy
import json
import unittest
from unittest.mock import AsyncMock, patch

from app.core.config import settings
from app.services.cv_compact_contract import validate_fragment
from app.services.cv_consolidation import validate_and_build_profile
from app.services.cv_education_guard import ground_education, retain_explicit_academic_education, safe_academic_narrative
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

    def test_abbreviated_licenciatura_cursada_is_preserved_without_awarding_degree(self):
        source = 'Lic. en Comunicación social\ncursado 2016-2018 UNNE'
        row = ground_education([{'level': 'bachelor', 'field': 'Comunicación social',
                                 'source_quotes': [source]}], source)[0]
        self.assertEqual(row['completion_status'], 'unknown')
        self.assertIsNone(row['completed'])

    def test_wrapped_course_keeps_its_name_and_explicit_completion(self):
        source = 'Curso de análisis de datos -\nInnova finalizado 05/2026'
        row = ground_education([{'level': 'other', 'field': 'Curso de análisis de datos',
                                 'source_quotes': source.splitlines()}], source)[0]
        self.assertEqual(row['field'], 'Curso de análisis de datos')
        self.assertEqual(row['completion_status'], 'completed')

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


class ExplicitCourseTests(unittest.TestCase):
    def test_explicit_courses_survive_even_when_provider_omits_them(self):
        from app.services.cv_compact_contract import validate_expanded_profile
        source = ('EDUCACIÓN\nCurso de conceptos básicos de\nMicrosoft Word y Excel\n'
                  'Curso de análisis de datos -\nInnova finalizado 05/2026\n'
                  'HABILIDADES\nManejo de caja.')
        profile = {'schema_version': 'candidate-profile.v2', 'profile_id': '42',
                   'headline': None, 'summary': None, 'skills': [], 'work_experience': [],
                   'education': [], 'languages': [], 'preferences': {'locations': [], 'work_modes': []},
                   'credentials': [], 'rejected_skills': []}
        result = validate_expanded_profile(profile, source, 42)
        self.assertEqual([row['field'] for row in result['education']],
                         ['Curso de conceptos básicos de Microsoft Word y Excel', 'Curso de análisis de datos'])
        self.assertEqual([row['completion_status'] for row in result['education']], ['unknown', 'completed'])
        self.assertEqual(result['skills'], [])


class ExplicitEducationRecoveryTests(unittest.TestCase):
    def test_omitted_vocational_training_is_recovered_only_from_education_section(self):
        source = ('FORMACIÓN ACADÉMICA\nPiloto comercial\nEscuela de aviación\n'
                  'EXPERIENCIA LABORAL\nVendedor de mostrador')
        rows = retain_explicit_academic_education([], source)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['field'], 'Piloto comercial')
        self.assertEqual(rows[0]['level'], 'other')
        self.assertIsNone(ground_education(rows, source)[0]['completed'])
        self.assertEqual(retain_explicit_academic_education([], 'EXPERIENCIA LABORAL\nPiloto comercial'), [])

    def test_technical_label_without_formal_degree_does_not_invent_tecnicatura(self):
        source = 'EDUCACIÓN\nPiloto comercial\n'
        row = ground_education([{'level': 'technical', 'field': 'Piloto comercial',
                                 'source_quotes': ['Piloto comercial']}], source)[0]
        self.assertEqual(row['level'], 'other')
        self.assertIsNone(row['completed'])
        expanded, _compact = validate_fragment({'ed': [{'l': 'technical', 'f': 'Piloto comercial',
                                                        'q': ['Piloto comercial']}]}, source)
        self.assertEqual(expanded['education'][0]['level'], 'other')

    def test_formal_study_recovery_keeps_status_and_deduplicates_provider_fact(self):
        source = 'ESTUDIOS\nSecundario completo\nLicenciatura en Historia, en curso\n'
        rows = retain_explicit_academic_education([], source)
        self.assertEqual([(row['level'], row['field']) for row in rows],
                         [('secondary', None), ('bachelor', 'Historia')])
        grounded = ground_education(rows, source)
        self.assertEqual([row['completion_status'] for row in grounded], ['completed', 'in_progress'])
        self.assertEqual(len(retain_explicit_academic_education(grounded, source)), 2)

    def test_wrapped_program_name_is_one_study(self):
        source = 'EDUCACIÓN\nLicenciatura en\nComunicación social\nen curso\n'
        rows = retain_explicit_academic_education([], source)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['field'], 'Comunicación social')
        self.assertEqual(ground_education(rows, source)[0]['completion_status'], 'in_progress')

    def test_final_profile_restores_omitted_training(self):
        from app.services.cv_compact_contract import validate_expanded_profile

        source = 'FORMACIÓN PROFESIONAL\nPiloto comercial\nEXPERIENCIA LABORAL\nVendedor'
        profile = {'schema_version': 'candidate-profile.v2', 'profile_id': '42',
                   'headline': None, 'summary': None, 'skills': [], 'work_experience': [],
                   'education': [], 'languages': [], 'preferences': {'locations': [], 'work_modes': []},
                   'credentials': [], 'rejected_skills': []}
        result = validate_expanded_profile(profile, source, 42)
        self.assertEqual([(row['level'], row['field']) for row in result['education']],
                         [('other', 'Piloto comercial')])


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
