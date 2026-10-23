"""Closed aliases, evidence-preserving merges and conservative non-skill guards."""

from copy import deepcopy
import unittest

from app.services.cv_compact_contract import contract_profile_to_wire, validate_fragment
from app.services.cv_skill_taxonomy import (
    SOFT_SKILL_TAXONOMY, SkillTaxonomyError, non_competency_kind, normalize_expanded_skills,
)


def skill(name, *, kind='soft', concept=None, level='unknown', origin='declared',
          evidence=None, quotes=None, years=None):
    return {'name': name, 'kind': kind, 'concept_id': concept, 'level': level, 'origin': origin,
            'years_experience': years, 'evidence': evidence or ['Competencia mencionada en el documento sintético.'],
            'source_quotes': quotes or [name]}


class SoftSkillTaxonomyTests(unittest.TestCase):
    def test_exact_closed_catalog_is_available_without_forcing_all_ten(self):
        self.assertEqual(list(SOFT_SKILL_TAXONOMY), [
            'Pensamiento crítico', 'Alfabetización en IA', 'Inteligencia emocional', 'Learnability',
            'Comunicación', 'Colaboración', 'Adaptabilidad al cambio', 'Creatividad',
            'Responsabilidad', 'Proactividad',
        ])
        self.assertEqual(normalize_expanded_skills([]), [])
        result = normalize_expanded_skills([skill('Creatividad')])
        self.assertEqual([row['name'] for row in result], ['Creatividad'])
        self.assertEqual(result[0]['concept_id'], 'soft.creativity')

    def test_canonical_names_and_prudent_aliases_normalize_without_local_inference(self):
        cases = {
            '  PENSAMIENTO   CRITICO ': 'Pensamiento crítico',
            'Alfabetización en inteligencia artificial': 'Alfabetización en IA',
            'emotional intelligence': 'Inteligencia emocional',
            'aprendizaje continuo': 'Learnability',
            'Comunicación efectiva verbal y escrita': 'Comunicación',
            'Trabajo en equipo': 'Colaboración',
            'adaptación al cambio': 'Adaptabilidad al cambio',
            'creativity': 'Creatividad', 'Responsabilidad': 'Responsabilidad', 'Iniciativa': 'Proactividad',
        }
        for alias, canonical in cases.items():
            with self.subTest(alias=alias):
                result = normalize_expanded_skills([skill(alias)])[0]
                self.assertEqual((result['name'], result['concept_id']),
                                 (canonical, SOFT_SKILL_TAXONOMY[canonical]))

    def test_known_names_override_unknown_or_technical_kind_without_bypassing_catalog(self):
        for kind in ('unknown', 'technical'):
            for name in ('Alfabetización en IA', 'Trabajo en equipo'):
                with self.subTest(kind=kind, name=name):
                    self.assertEqual(normalize_expanded_skills([skill(name, kind=kind)])[0]['kind'], 'soft')
        technical = skill('Inteligencia artificial', kind='technical', concept='technical.ai')
        self.assertEqual(normalize_expanded_skills([technical]), [technical])

    def test_unlisted_blanks_are_rejected_instead_of_inventing_relationships(self):
        for name in ('Liderazgo', 'Organización', 'Pensamiento analítico', 'Atención al detalle', 'Empatía'):
            with self.subTest(name=name):
                with self.assertRaises(SkillTaxonomyError) as error:
                    normalize_expanded_skills([skill(name)])
                self.assertEqual(error.exception.code, 'unsupported_soft_skill')
                self.assertNotIn(name, str(error.exception))

    def test_recognized_concept_cannot_override_an_unrecognized_or_conflicting_name(self):
        cases = [
            skill('Liderazgo', concept='soft.proactivity'),
            skill('Organización', kind='technical', concept='soft.responsibility'),
            skill('Comunicación', concept='soft.creativity'),
            skill('Python', kind='unknown', concept='soft.collaboration'),
        ]
        for row in cases:
            with self.subTest(name=row['name']):
                with self.assertRaises(SkillTaxonomyError) as error:
                    normalize_expanded_skills([row])
                self.assertEqual(error.exception.code, 'concept_name_conflict')

    def test_unknown_kind_and_unlisted_soft_identifier_cannot_bypass_the_catalog(self):
        with self.assertRaises(SkillTaxonomyError) as error:
            normalize_expanded_skills([skill('Liderazgo', kind='unknown', concept='soft.leadership')])
        self.assertEqual(error.exception.code, 'unsupported_soft_skill')

    def test_old_soft_names_cannot_bypass_catalog_by_changing_kind_or_omitting_concept(self):
        for name in ('Liderazgo', 'Organización', 'Gestión del tiempo', 'Resolución de problemas',
                     'Atención al detalle', 'Orientación al cliente', 'Pensamiento analítico',
                     'Pensamiento estructurado', 'Pensamiento sistémico'):
            for kind in ('unknown', 'technical'):
                with self.subTest(name=name, kind=kind):
                    with self.assertRaises(SkillTaxonomyError) as error:
                        normalize_expanded_skills([skill(name, kind=kind)])
                    self.assertEqual(error.exception.code, 'unsupported_soft_skill')

    def test_course_title_alone_does_not_support_a_skill_but_practical_training_can(self):
        for quote in ('Curso de Excel', 'Curso Excel inicial.', 'Curso: Excel para presupuestos'):
            with self.subTest(quote=quote):
                with self.assertRaises(SkillTaxonomyError) as error:
                    normalize_expanded_skills([skill('Excel', kind='technical', quotes=[quote])])
                self.assertEqual(error.exception.code, 'training_only_evidence')
        for quote in ('Curso de Excel. Usé Excel para preparar presupuestos.',
                      'Curso de Excel donde practiqué fórmulas y preparé presupuestos.',
                      'Curso de Excel\nProyecto final: presupuesto con fórmulas.',
                      'Curso de Excel. Proyecto final: presupuesto con fórmulas.',
                      'Curso de Excel donde desarrollé un presupuesto con fórmulas.',
                      'Curso de Excel donde implementé una planilla de gastos.'):
            with self.subTest(quote=quote):
                row = skill('Excel', kind='technical', quotes=[quote])
                self.assertEqual(normalize_expanded_skills([row]), [row])

    def test_wrapped_course_line_cannot_become_a_skill(self):
        source = ('EDUCACIÓN\nCurso de conceptos básicos de\nMicrosoft Word y Excel\n'
                  'HABILIDADES\nComunicación efectiva\nEXPERIENCIA\nUsé Excel para facturar.')
        course_quote = 'Microsoft Word y Excel'
        with self.assertRaises(SkillTaxonomyError) as error:
            normalize_expanded_skills([skill('Microsoft Word', kind='technical',
                                             quotes=[course_quote])], source_text=source)
        self.assertEqual(error.exception.code, 'training_only_evidence')
        # A separate statement of use supports Excel, but not Word.
        self.assertEqual(len(normalize_expanded_skills([skill('Microsoft Excel', kind='technical',
            quotes=['Usé Excel para facturar.'])], source_text=source)), 1)

    def test_alias_dedup_keeps_literal_evidence_quotes_origin_and_known_metadata(self):
        rows = [
            skill('Trabajo en equipo', concept='soft.teamwork', level='unknown', origin='inferred',
                  evidence=['Colaboró con colegas.'], quotes=['Colaboré con colegas.']),
            skill('Colaboración', concept='soft.collaboration', level='intermediate', years=2,
                  evidence=['Declara colaboración.'], quotes=['Colaboración']),
        ]
        before = deepcopy(rows)
        result = normalize_expanded_skills(rows)
        self.assertEqual(rows, before)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['name'], 'Colaboración')
        self.assertEqual(result[0]['concept_id'], 'soft.collaboration')
        self.assertEqual((result[0]['level'], result[0]['years_experience'], result[0]['origin']),
                         ('intermediate', 2, 'declared'))
        self.assertEqual(result[0]['evidence'], ['Colaboró con colegas.', 'Declara colaboración.'])
        self.assertEqual(result[0]['source_quotes'], ['Colaboré con colegas.', 'Colaboración'])
        self.assertEqual(normalize_expanded_skills(result), result)

    def test_evidence_union_deduplicates_only_exact_strings_and_never_rewrites_quotes(self):
        rows = [skill('Comunicación', quotes=['Texto original.', 'TEXTO ORIGINAL.']),
                skill('Comunicación efectiva', quotes=['Texto original.', 'Otro texto.'])]
        merged = normalize_expanded_skills(rows)[0]
        self.assertEqual(merged['source_quotes'], ['Texto original.', 'TEXTO ORIGINAL.', 'Otro texto.'])

    def test_metadata_conflicts_are_retryable_and_do_not_select_a_higher_level_or_years(self):
        for overrides in ({'level': 'advanced'}, {'years': 3}):
            rows = [skill('Iniciativa', level='intermediate', years=2),
                    skill('Proactividad', level=overrides.get('level', 'intermediate'), years=overrides.get('years', 2))]
            original = deepcopy(rows)
            with self.assertRaises(SkillTaxonomyError) as error:
                normalize_expanded_skills(rows)
            self.assertEqual(error.exception.code, 'incompatible_metadata')
            self.assertEqual(rows, original)

    def test_oversized_union_is_rejected_without_silently_truncating_evidence_or_quotes(self):
        for field in ('evidence', 'source_quotes'):
            rows = [skill('Iniciativa'), skill('Proactividad')]
            rows[0][field] = [f'Hecho {index}' for index in range(5)]
            rows[1][field] = [f'Hecho {index}' for index in range(5, 9)]
            with self.assertRaises(SkillTaxonomyError) as error:
                normalize_expanded_skills(rows)
            self.assertEqual(error.exception.code, 'evidence_limit')

    def test_invalid_evidence_and_automatic_user_declared_origin_are_rejected(self):
        rows = [skill('Comunicación', evidence=['x' * 161]), skill('Comunicación', origin='user_declared')]
        missing = skill('Comunicación')
        missing['source_quotes'] = []
        for row in [*rows, missing]:
            with self.subTest(field=row['origin']):
                with self.assertRaises(SkillTaxonomyError):
                    normalize_expanded_skills([row])

    def test_fragment_roundtrip_preserves_original_quotes_and_inferred_level_provenance(self):
        source = 'Trabajo en equipo. Colaboré con colegas para completar la tarea.'
        raw = {'sk': [
            {'n': 'Trabajo en equipo', 'k': 'soft', 'o': 'declared', 'l': 'intermediate',
             'e': ['Menciona la habilidad y describe una tarea colaborativa.'], 'q': ['Trabajo en equipo.']},
            {'n': 'Colaboración', 'k': 'soft', 'o': 'inferred', 'l': 'unknown',
             'e': ['Colabora con colegas para completar la tarea.'], 'q': ['Colaboré con colegas para completar la tarea.']},
        ]}
        expanded, _ = validate_fragment(raw, source)
        expanded['skills'] = normalize_expanded_skills(expanded['skills'])
        wire = contract_profile_to_wire({'schema_version': 'candidate-profile.v2', 'profile_id': '42', **expanded})
        wire.pop('v', None)
        wire.pop('id', None)
        restored, compact = validate_fragment(wire, source)
        self.assertEqual(len(restored['skills']), 1)
        self.assertEqual(restored['skills'][0]['name'], 'Colaboración')
        self.assertEqual(restored['skills'][0]['level'], 'intermediate')
        self.assertEqual(restored['skills'][0]['origin'], 'declared')
        self.assertEqual(compact['sk'][0]['q'], ['Trabajo en equipo.', 'Colaboré con colegas para completar la tarea.'])


class NonCompetencyNameTests(unittest.TestCase):
    def test_explicit_course_degree_and_role_names_are_detected(self):
        cases = {'Curso de Excel': 'course', 'Curso Excel': 'course', 'Curso: Excel inicial': 'course',
                 'Taller de soldadura': 'course', 'Doctor en Informática': 'degree',
                 'Doctorado en Biología': 'degree', 'Licenciatura en Administración': 'degree',
                 'Técnico en Enfermería': 'degree', 'Auxiliar administrativa': 'role',
                 'Gerente de operaciones': 'role', 'Enfermero': 'role'}
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(non_competency_kind(name), expected)
                with self.assertRaises(SkillTaxonomyError) as error:
                    normalize_expanded_skills([skill(name, kind='technical')])
                self.assertEqual(error.exception.code, 'non_competency_name')

    def test_domains_design_and_training_activities_are_not_confused_with_credentials_or_jobs(self):
        for name in ('Excel', 'Diseño de cursos', 'Administración', 'Enfermería',
                     'Técnicas de enfermería', 'Capacitación de personal', 'Master data management', 'Certificación digital',
                     'Diseño de cursos en línea', 'Administración de cursos', 'Cursores de bases de datos'):
            with self.subTest(name=name):
                self.assertIsNone(non_competency_kind(name))
                row = skill(name, kind='technical')
                self.assertEqual(normalize_expanded_skills([row]), [row])
