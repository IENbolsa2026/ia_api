import unittest

from app.services.cv_compact_contract import contract_profile_to_wire, expand_profile, salvage_fragment, validate_fragment


class CompactCvContractTests(unittest.TestCase):
    def test_generated_html_entities_are_decoded_but_literal_quotes_remain_unchanged(self):
        source = 'Uso de sistema de facturación.'
        expanded, _compact = validate_fragment({
            's': 'Experiencia en facturaci&oacute;n.',
            'sk': [{'n': 'Sistemas de facturaci&oacute;n', 'k': 'technical',
                    'e': ['Uso de facturaci&oacute;n.'], 'q': [source]}],
        }, source)
        self.assertEqual(expanded['summary'], 'Experiencia en facturación.')
        self.assertEqual(expanded['skills'][0]['name'], 'Sistemas de facturación')
        self.assertEqual(expanded['skills'][0]['evidence'], ['Uso de facturación.'])
        self.assertEqual(expanded['skills'][0]['source_quotes'], [source])

    def test_salvage_keeps_grounded_facts_without_inventing_studies_or_credentials(self):
        source = "Usé Excel para registrar ventas. Curso de Administración en curso."
        raw = {
            "sk": [
                {"n": "Excel", "k": "technical", "e": ["Registró ventas"],
                 "q": ["Usé Excel para registrar ventas."]},
                {"n": "CRM", "k": "technical", "e": ["Usó CRM"], "q": ["No está en el CV"]},
            ],
            "ed": [{"l": "other", "f": "Administración y Marketing",
                    "q": ["Curso de Administración en curso."]}],
            "c": [{"n": "Certificado de ventas", "e": ["Obtenido"], "q": ["No está en el CV"]}],
            "x": [{"t": "Vendedor", "h": ["Registró ventas. " * 15]}],
        }
        with self.assertRaises(ValueError):
            validate_fragment(raw, source)
        expanded, compact, dropped = salvage_fragment(raw, source)
        self.assertEqual([skill["name"] for skill in expanded["skills"]], ["Excel"])
        self.assertEqual(expanded["education"][0]["field"], None)
        self.assertEqual(expanded["education"][0]["completion_status"], "in_progress")
        self.assertEqual(expanded["credentials"], [])
        self.assertLessEqual(len(compact["x"][0]["h"][0]), 160)
        self.assertEqual(dropped, {"sk": 1, "ed": 0, "c": 1})

    def test_salvage_rejects_a_fragment_with_no_grounded_facts(self):
        with self.assertRaisesRegex(ValueError, "hechos verificables"):
            salvage_fragment({"sk": [{"n": "CRM", "e": ["Usó CRM"],
                                      "q": ["No está en el CV"]}]}, "Solo nombre.")

    def test_compact_and_expand_preserve_candidate_profile_v2_facts(self):
        source = (
            "Manejo de camiones. Operación segura de grúa. Empresa Ruta Sur. "
            "Instituto Técnico. Inglés conversacional. Licencia CE, emitida 2022, vence 2030."
        )
        profile = {
            "schema_version": "candidate-profile.v2",
            "profile_id": "42",
            "headline": "Conductor profesional",
            "summary": "Experiencia en transporte y logística.",
            "skills": [{
                "name": "Conducción de camiones", "kind": "technical", "level": "advanced",
                "origin": "inferred", "years_experience": 4.5,
                "evidence": ["Condujo camiones en tareas de transporte."],
                "source_quotes": ["Manejo de camiones."], "concept_id": "driving.truck",
            }],
            "work_experience": [{
                "job_title": "Chofer", "organization": "Ruta Sur", "duration_months": 48,
                "highlights": ["Traslado de cargas en rutas nacionales."],
            }],
            "education": [{"level": "technical", "field": "Logística", "institution": "Instituto Técnico", "completed": True,
                           "completion_status": "unknown", "source_quotes": []}],
            "languages": [{"name": "Inglés", "level": "conversational"}],
            "preferences": {"locations": ["Buenos Aires"], "work_modes": ["onsite"]},
            "credentials": [{
                "name": "Licencia CE", "issuer": "Autoridad de transporte", "issued_at": "2022-01-01",
                "expires_at": "2030-01-01", "evidence": ["El CV declara una licencia CE vigente."],
                "source_quotes": ["Licencia CE, emitida 2022, vence 2030."], "origin": "declared",
            }],
            "rejected_skills": [],
        }

        wire = contract_profile_to_wire(profile)
        self.assertEqual(set(wire), {"v", "id", "h", "s", "sk", "x", "ed", "l", "p", "c"})
        self.assertEqual(set(wire["sk"][0]), {"n", "k", "l", "o", "y", "e", "q", "ci"})
        expanded = expand_profile(wire, source, 42)
        self.assertEqual(expanded, profile)

    def test_fragment_uses_aliases_and_checks_quotes_against_its_block(self):
        fragment, compact = validate_fragment({
            "sk": [{"n": "Conducción", "k": "technical", "o": "inferred",
                    "e": ["Conducción de camiones"], "q": ["Manejo de camiones."]}],
        }, "Manejo de camiones.")
        self.assertEqual(fragment["skills"][0]["name"], "Conducción")
        self.assertEqual(compact["sk"][0]["n"], "Conducción")
        with self.assertRaisesRegex(ValueError, "no aparecen"):
            validate_fragment({"sk": [{"n": "Inglés", "e": ["habla inglés"], "q": ["English"]}]}, "CV en inglés")

    def test_expansion_rejects_unknown_aliases_and_wrong_identity(self):
        with self.assertRaisesRegex(ValueError, "perfil compacto"):
            expand_profile({"schema_version": "candidate-profile.v2", "profile_id": "42"}, "", 42)
        with self.assertRaisesRegex(ValueError, "identidad"):
            expand_profile({"v": "candidate-profile.v2", "id": "41"}, "", 42)

    def test_compactor_does_not_silently_discard_rejections_or_invalid_contracts(self):
        with self.assertRaisesRegex(ValueError, "habilidades rechazadas"):
            contract_profile_to_wire({
                "schema_version": "candidate-profile.v2", "profile_id": "42",
                "rejected_skills": [{"name": "Conducción", "reason": "No está en el CV"}],
            })

    def test_fragment_schema_diagnostics_log_only_safe_paths_and_error_types(self):
        from app.services.cv_compact_contract import validate_fragment

        with self.assertLogs("app.services.cv_compact_contract", level="WARNING") as captured:
            try:
                validate_fragment({"sk": [{"n": "Private Applicant Name", "mystery": "PRIVATE_VALUE"}]}, "")
            except ValueError as exc:
                self.assertIsNone(exc.__cause__)
                self.assertEqual(str(exc), "El proveedor devolvió un fragmento compacto de CV inválido.")
            else:
                self.fail("Invalid compact data should be rejected")
        log_output = "\n".join(captured.output)
        self.assertIn("extra_forbidden", log_output)
        self.assertIn("?", log_output)
        self.assertNotIn("Private Applicant Name", log_output)
        self.assertNotIn("PRIVATE_VALUE", log_output)

    def test_single_evidence_or_quote_string_is_wrapped_only_for_skill_and_credential_transport(self):
        skill, _ = validate_fragment({
            "sk": [{"n": "Python", "e": "Automatizó tareas con Python", "q": "Uso Python"}],
        }, "Uso Python")
        self.assertEqual(skill["skills"][0]["evidence"], ["Automatizó tareas con Python"])
        self.assertEqual(skill["skills"][0]["source_quotes"], ["Uso Python"])

        credential, _ = validate_fragment({
            "c": [{"n": "Licencia CE", "e": "El CV declara la licencia", "q": "Licencia CE"}],
        }, "Licencia CE")
        self.assertEqual(credential["credentials"][0]["evidence"], ["El CV declara la licencia"])
        self.assertEqual(credential["credentials"][0]["source_quotes"], ["Licencia CE"])

        for bad_value in (17, {}, None):
            with self.subTest(bad_value=bad_value):
                with self.assertRaisesRegex(ValueError, "fragmento compacto"):
                    validate_fragment({"sk": [{"n": "Python", "e": bad_value, "q": "Python"}]}, "Python")
                with self.assertRaisesRegex(ValueError, "fragmento compacto"):
                    validate_fragment({"c": [{"n": "Licencia CE", "e": "Declarada", "q": bad_value}]}, "Licencia CE")
        with self.assertRaisesRegex(ValueError, "evidencia.*breve"):
            validate_fragment({"sk": [{"n": "Python", "e": "", "q": "Python"}]}, "Python")


if __name__ == "__main__":
    unittest.main()
