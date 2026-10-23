import unittest
import json
import copy
from uuid import uuid4
from unittest.mock import patch

from app.core.config import settings
from app.services.cv_consolidation import (
    compact_facts_for_plan,
    validate_and_build_profile,
    validate_and_build_profile_with_effective_plan,
)
from app.services.cv_packets import make_packet_identity, process_packetized_cv_text
from app.services.cv_pipeline import CV_EXTRACTION_PROMPT_VERSION


def plan(**overrides):
    value = {"h": None, "s": None, "sk": [], "x": [], "ed": [], "l": [], "c": []}
    value.update(overrides)
    return value


class CvConsolidationTests(unittest.TestCase):
    def test_broad_sales_name_merges_into_specific_name_with_same_concept(self):
        fragments = [{'sk': [
            {'n': 'Gestión de ventas', 'k': 'technical', 'ci': 'sales.sales',
             'e': ['Gestionó ventas'], 'q': ['Gestión de ventas']},
            {'n': 'Ventas', 'k': 'technical', 'ci': 'sales.sales',
             'e': ['Cumplió objetivos'], 'q': ['Objetivos de ventas mensuales']},
        ]}]
        result = validate_and_build_profile(plan(sk=[['sk0', 'sk1']]), fragments, 42)
        self.assertEqual([row['name'] for row in result['skills']], ['Gestión de ventas'])
        self.assertEqual(len(result['skills'][0]['source_quotes']), 2)

    def test_shared_generic_concept_does_not_collapse_distinct_languages(self):
        fragments = [{"sk": [
            {"n": "Python", "k": "technical", "ci": "technical.programming",
             "e": ["Programa en Python"], "q": ["Python"]},
            {"n": "Java", "k": "technical", "ci": "technical.programming",
             "e": ["Programa en Java"], "q": ["Java"]},
        ]}]
        profile, effective = validate_and_build_profile_with_effective_plan(
            plan(sk=[["sk0", "sk1"]]), fragments, 42)
        self.assertEqual(effective["sk"], [["sk0"], ["sk1"]])
        self.assertEqual([row["name"] for row in profile["skills"]], ["Python", "Java"])

    def test_plan_merges_compatible_skill_records_and_preserves_server_evidence(self):
        fragments = [
            {"sk": [{"n": "Python", "k": "technical", "l": "advanced", "o": "declared",
                     "y": 5, "ci": "programming.python", "e": ["Experiencia en Python"], "q": ["Uso Python"]}]},
            {"sk": [{"n": "Python", "k": "unknown", "l": "unknown", "y": None,
                     "e": ["Desarrolló automatizaciones"], "q": ["Python para automatizaciones"]}]},
        ]
        facts = compact_facts_for_plan(fragments)
        self.assertEqual(facts["sk"], [
            {"id": "sk0", "n": "Python", "k": "technical", "l": "advanced", "y": 5.0, "ci": "programming.python"},
            {"id": "sk1", "n": "Python"},
        ])
        result = validate_and_build_profile(plan(sk=[["sk0", "sk1"]]), fragments, 42)
        skill = result["skills"][0]
        self.assertEqual(skill["name"], "Python")
        self.assertEqual(skill["level"], "advanced")
        self.assertEqual(skill["years_experience"], 5)
        self.assertEqual(skill["concept_id"], "programming.python")
        self.assertEqual(skill["evidence"], ["Experiencia en Python", "Desarrolló automatizaciones"])
        self.assertEqual(skill["source_quotes"], ["Uso Python", "Python para automatizaciones"])

    def test_plan_requires_exactly_once_section_correct_fact_coverage(self):
        fragments = [{"sk": [{"n": "Python", "e": ["La menciona"], "q": ["Python"]}]}]
        invalid_plans = [
            plan(),  # omitted fact
            plan(sk=[["sk0", "sk0"]]),  # duplicate within a group
            plan(sk=[["x0"]]),  # cross-section or invented ID
            {"h": None, "s": None, "sk": [["sk0"]]},  # required arrays omitted
        ]
        for invalid in invalid_plans:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    validate_and_build_profile(invalid, fragments, 42)

    def test_incompatible_skill_group_falls_back_to_lossless_singletons(self):
        skill_fragments = [{"sk": [
            {"n": "Python", "k": "technical", "l": "advanced", "ci": "python",
             "e": ["Conocimientos avanzados de Python"], "q": ["Python avanzado"]},
            {"n": "SQL", "k": "technical", "l": "intermediate", "ci": "sql",
             "e": ["Uso de SQL"], "q": ["SQL"]},
        ]}]
        profile, effective = validate_and_build_profile_with_effective_plan(
            plan(sk=[["sk0", "sk1"]]), skill_fragments, 42,
        )
        self.assertEqual(effective["sk"], [["sk0"], ["sk1"]])
        self.assertEqual([item["name"] for item in profile["skills"]], ["Python", "SQL"])
        self.assertEqual([item["level"] for item in profile["skills"]], ["advanced", "intermediate"])
        self.assertEqual(profile["skills"][0]["source_quotes"], ["Python avanzado"])
        self.assertEqual(profile["skills"][1]["source_quotes"], ["SQL"])
        self.assertEqual(profile["skills"][0]["evidence"], ["Conocimientos avanzados de Python"])
        self.assertEqual(profile["skills"][1]["evidence"], ["Uso de SQL"])

        experience_fragments = [{"x": [
            {"t": "Chofer", "o": "Ruta Sur", "m": 24},
            {"t": "Mecánico", "o": "Ruta Sur", "m": 18},
        ]}]
        profile, effective = validate_and_build_profile_with_effective_plan(
            plan(x=[["x0", "x1"]]), experience_fragments, 42,
        )
        self.assertEqual(effective["x"], [["x0"], ["x1"]])
        self.assertEqual([item["job_title"] for item in profile["work_experience"]], ["Chofer", "Mecánico"])
        self.assertEqual([item["duration_months"] for item in profile["work_experience"]], [24, 18])

        # Missing detail is not a contradiction, so the known duration survives.
        compatible = [{"x": [{"t": "Chofer", "o": "Ruta Sur", "m": 24},
                              {"t": "Chofer", "o": "Ruta Sur"}]}]
        merged = validate_and_build_profile(plan(x=[["x0", "x1"]]), compatible, 42)
        self.assertEqual(merged["work_experience"][0]["duration_months"], 24)

    def test_effective_plan_rebuilds_same_profile_and_invalid_coverage_stays_fatal(self):
        fragments = [{"sk": [
            {"n": "Python", "ci": "python", "e": ["Usa Python"], "q": ["Python"]},
            {"n": "SQL", "ci": "sql", "e": ["Usa SQL"], "q": ["SQL"]},
        ]}]
        profile, effective = validate_and_build_profile_with_effective_plan(
            plan(sk=[["sk0", "sk1"]]), fragments, 42,
        )
        rebuilt, rebuilt_effective = validate_and_build_profile_with_effective_plan(effective, fragments, 42)
        self.assertEqual(rebuilt, profile)
        self.assertEqual(rebuilt_effective, effective)
        for invalid in (
            plan(sk=[["sk0", "sk0"], ["sk1"]]),
            plan(sk=[["sk0"]]),
            plan(sk=[["sk0", "x0"], ["sk1"]]),
            plan(sk=[["sk0", "invented"], ["sk1"]]),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_and_build_profile_with_effective_plan(invalid, fragments, 42)


class CvEffectivePacketPlanTests(unittest.IsolatedAsyncioTestCase):
    async def test_packet_checkpoint_stores_repaired_effective_plan_and_rebuilds_cache(self):
        text = "Python SQL Inglés"
        provider = settings.LLM_PROVIDER.strip().lower()
        model = settings.OPENAI_MODEL if provider == "openai" else settings.CV_MODEL
        identity = make_packet_identity(
            job_id=str(uuid4()), analysis_id=9, candidate_id=42, cv_version_id=31,
            source_hash="a" * 64, text=text, prompt_version=CV_EXTRACTION_PROMPT_VERSION,
            provider=provider, model=model, packet_chars=500, block_chars=4000, budget=950,
        )
        snapshots = []
        provider_calls = []

        async def provider_call(_system, payload, marker, *, on_heartbeat=None):
            provider_calls.append(marker)
            if marker.endswith("consolidation-plan"):
                return {"h": None, "s": None, "sk": [["sk0", "sk1"]], "x": [], "ed": [], "l": [["l0", "l1"]], "c": []}
            return {"sk": [
                {"n": "Python", "k": "technical", "o": "declared", "e": ["Conoce Python"], "q": ["Python"], "ci": "python"},
                {"n": "SQL", "k": "technical", "o": "declared", "e": ["Conoce SQL"], "q": ["SQL"], "ci": "sql"},
            ], "l": [{"n": "Inglés", "l": "basic"}, {"n": "Inglés"}]}

        with patch("app.services.cv_packets.load_prompt", return_value="prompt"):
            profile, _model_name = await process_packetized_cv_text(
                text, 42, identity=identity, checkpoint=None, on_checkpoint=snapshots.append,
                on_heartbeat=None, call_provider=provider_call,
            )
            saved = snapshots[-1]
            self.assertEqual(saved["schema_version"], "cv-packets.v2")
            self.assertEqual(saved["consolidation"]["plan"]["sk"], [["sk0"], ["sk1"]])
            self.assertEqual([skill["name"] for skill in profile["skills"]], ["Python", "SQL"])

            async def forbidden_provider(*_args, **_kwargs):
                raise AssertionError("La caché completa debe validarse localmente, sin llamar al proveedor.")

            restored, _cached_model = await process_packetized_cv_text(
                text, 42, identity=identity, checkpoint=saved, on_checkpoint=None,
                on_heartbeat=None, call_provider=forbidden_provider,
            )
        self.assertEqual(restored, profile)
        self.assertEqual(len(provider_calls), 2)

    async def test_v1_pending_manifest_with_or_without_null_plan_migrates_after_validation(self):
        text = "Python SQL"
        provider = settings.LLM_PROVIDER.strip().lower()
        model = settings.OPENAI_MODEL if provider == "openai" else settings.CV_MODEL
        identity = make_packet_identity(
            job_id=str(uuid4()), analysis_id=10, candidate_id=42, cv_version_id=None,
            source_hash="b" * 64, text=text, prompt_version=CV_EXTRACTION_PROMPT_VERSION,
            provider=provider, model=model, packet_chars=500, block_chars=4000, budget=950,
        )
        fragment = {"sk": [
            {"n": "Python", "k": "technical", "o": "declared", "e": ["Conoce Python"], "q": ["Python"]},
            {"n": "SQL", "k": "technical", "o": "declared", "e": ["Conoce SQL"], "q": ["SQL"]},
        ]}

        async def initial_provider(_system, _payload, marker, *, on_heartbeat=None):
            return {"h": None, "s": None, "sk": [["sk0"], ["sk1"]], "x": [], "ed": [], "l": [], "c": []} if marker.endswith("consolidation-plan") else fragment

        async def plan_provider(_system, payload, marker, *, on_heartbeat=None):
            requests.append(marker)
            request = json.loads(payload)
            facts = request["facts"]
            if marker.endswith("consolidation-plan"):
                return {"h": None, "s": None,
                        "sk": [[item["id"]] for item in facts["sk"]],
                        "x": [], "ed": [], "l": [], "c": []}
            raise AssertionError("Los paquetes completos no deben volver a enviarse.")

        with patch("app.services.cv_packets.load_prompt", return_value="prompt"):
            saved = []
            _profile, _provider_model = await process_packetized_cv_text(
                text, 42, identity=identity, checkpoint=None, on_checkpoint=saved.append,
                on_heartbeat=None, call_provider=initial_provider,
            )
            for include_plan_key in (False, True):
                # Start from a fully validated leaf manifest but leave only the
                # final consolidation pending, as after the prior worker's failure.
                manifest = copy.deepcopy(saved[-1])
                manifest["schema_version"] = "cv-packets.v1"
                manifest["consolidation"] = {"status": "pending", "result": None, "provider_model": None}
                if include_plan_key:
                    manifest["consolidation"]["plan"] = None
                requests = []
                migrated = []
                await process_packetized_cv_text(
                    text, 42, identity=identity, checkpoint=manifest, on_checkpoint=migrated.append,
                    on_heartbeat=None, call_provider=plan_provider,
                )
                self.assertEqual(requests, [])  # One cached leaf assembles locally after migration.
                self.assertEqual(migrated[0]["schema_version"], "cv-packets.v2")
                self.assertEqual(migrated[-1]["consolidation"]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
