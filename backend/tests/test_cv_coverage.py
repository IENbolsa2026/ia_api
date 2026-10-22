"""Offline lossless-transport regressions; live fixtures measure LLM coverage.

These stubs supply the expected skills. They verify that unknown levels and
shared source citations do not cause the server to drop or combine skills;
they do not assert that a model follows the prompt.
"""

import copy
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from app.core.config import settings
from app.services.cv_compact_contract import validate_fragment
from app.services.cv_consolidation import validate_and_build_profile_with_effective_plan
from app.services.cv_packets import _quote_catalog, _resolve_quote_ids, validate_manifest
from app.services.cv_pipeline import process_cv_text
from app.services.cv_skill_taxonomy import SOFT_SKILL_TAXONOMY
from ien_contracts.models import SkillLevel


FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "cv_coverage_multisector.json").read_text(encoding="utf-8"))


def rows_for(case):
    return [
        {"n": item["name"], "k": kind, "l": item["level"], "o": item["origin"],
         "e": [item["evidence"]], "q": [item["quote_clue"]],
         **({"ci": item["concept_id"]} if item.get("concept_id") else {})}
        for key, kind in (("expected_atomics", "technical"), ("expected_soft_supported", "soft"))
        for item in case[key]
    ]


class CoveragePipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_dense_multisector_facts_keep_unknown_and_estimated_levels_without_dropping_skills(self):
        for case in FIXTURE["cases"]:
            with self.subTest(sector=case["sector"]):
                supplied = rows_for(case)
                checkpoints = []
                quote_ids_by_skill = {}
                attempts = []

                async def provider(_system, payload, marker, max_output_tokens=None):
                    self.assertFalse(marker.endswith("consolidation-plan"))
                    request = json.loads(payload)
                    attempts.append(marker)
                    result = {"h": "Perfil sintético", "s": "Experiencia sintética documentada.",
                              "sk": copy.deepcopy(supplied)}
                    if len(attempts) == 1 and case.get('rejected_soft_skills'):
                        result['sk'].extend({
                            'n': item['name'], 'k': 'soft', 'o': item['origin'], 'l': item['level'],
                            'ci': item['concept_id'], 'e': [item['evidence']], 'q': [item['quote_clue']],
                        } for item in case['rejected_soft_skills'])
                    for skill in result["sk"]:
                        clue = skill["q"][0]
                        quote_id = next(key for key, span in request["source_quotes"].items() if clue in span)
                        skill["q"] = [quote_id]
                        quote_ids_by_skill[skill["n"]] = quote_id
                    return result

                with patch.object(settings, "CV_PACKET_CHARACTERS", 20000), \
                     patch.object(settings, "CV_BLOQUE_CARACTERES", 20000), \
                     patch.object(settings, "CV_EXTRACTION_MAX_TOKENS", 16384), \
                     patch("app.services.cv_pipeline._call_async", new_callable=AsyncMock, side_effect=provider) as call:
                    profile = await process_cv_text(case["text"], 42, on_checkpoint=checkpoints.append)
                self.assertEqual(call.await_count, 2 if case.get('rejected_soft_skills') else 1)
                skills = {item["name"]: item for item in profile["skills"]}
                self.assertEqual(set(skills), {item["n"] for item in supplied})
                self.assertEqual(len(profile["skills"]), len(supplied))
                for row in supplied:
                    saved = skills[row["n"]]
                    self.assertEqual(saved["level"], row["l"])
                    self.assertIn(saved["level"], {level.value for level in SkillLevel})
                    self.assertEqual(saved["origin"], row["o"])
                    self.assertEqual(saved["kind"], row["k"])
                    if saved['kind'] == 'soft':
                        self.assertIn(saved['name'], SOFT_SKILL_TAXONOMY)
                    self.assertEqual(saved["evidence"], row["e"])
                    self.assertIsNone(saved["years_experience"])
                    self.assertTrue(any(row["q"][0] in quote for quote in saved["source_quotes"]))
                for combined in case.get("forbidden_combined_labels", []):
                    self.assertNotIn(combined, skills)
                manifest = checkpoints[-1]
                self.assertEqual(manifest["consolidation"]["result"], dict(profile))
                validate_manifest(manifest, case["text"], manifest["identity"])
                for excluded in case.get('rejected_soft_skills', []):
                    self.assertNotIn(excluded['name'], skills)
                    self.assertTrue(all(excluded['name'] != row.get('n') for snapshot in checkpoints
                                        for node in snapshot['packets'].values()
                                        for row in (node.get('result') or {}).get('sk', [])))
                if case["id"] == "educacion_listas_y_acciones":
                    self.assertEqual(quote_ids_by_skill["Microsoft Word"], quote_ids_by_skill["Microsoft Excel"])
                    self.assertEqual(skills["Microsoft Word"]["source_quotes"], skills["Microsoft Excel"]["source_quotes"])
                    self.assertEqual(skills["Microsoft Word"]["level"], "unknown")
                    self.assertEqual(skills["Microsoft Excel"]["level"], "unknown")


class CoverageConsolidationTests(unittest.TestCase):
    def test_internal_concept_identifier_cannot_be_published_as_a_skill_name(self):
        raw = {"sk": [{"n": "soft.communication", "k": "soft", "o": "declared",
                       "e": ["Habilidad expresamente declarada."], "q": ["Comunicación"]}]}
        with self.assertRaises(ValueError):
            validate_fragment(raw, "Comunicación")
        raw["sk"][0].update(n="Comunicación", ci="soft.communication")
        expanded, _ = validate_fragment(raw, "Comunicación")
        self.assertEqual(expanded["skills"][0]["name"], "Comunicación")

    def test_shared_quote_does_not_allow_merging_distinct_tool_concepts(self):
        source = "Usé el termómetro y el oxímetro siguiendo una guía y con supervisión."
        raw = {"sk": [
            {"n": name, "ci": concept, "k": "technical", "o": "declared", "l": "beginner",
             "e": [f"Usa {name.lower()} con guía y supervisión: nivel inicial."], "q": ["quote_0"]}
            for name, concept in (("Termómetro", "equipment.thermometer"), ("Oxímetro", "equipment.oximeter"))
        ]}
        resolved = _resolve_quote_ids(raw, _quote_catalog(source))
        _, compact = validate_fragment(resolved, source)
        # Even an unsafe LLM grouping is repaired losslessly by the existing
        # concept boundary; identical evidence does not imply identical skills.
        plan = {"h": None, "s": None, "sk": [["sk0", "sk1"]], "x": [], "ed": [], "l": [], "c": []}
        profile, effective = validate_and_build_profile_with_effective_plan(plan, [compact], 42)
        self.assertEqual([skill["name"] for skill in profile["skills"]], ["Termómetro", "Oxímetro"])
        self.assertEqual(effective["sk"], [["sk0"], ["sk1"]])
        self.assertEqual([skill["level"] for skill in profile["skills"]], ["beginner", "beginner"])
        self.assertEqual([skill["source_quotes"] for skill in profile["skills"]], [[source], [source]])

    def test_exact_alias_group_does_not_absorb_another_tool_with_the_same_quote(self):
        source = "Herramientas declaradas: Microsoft Word, Word y Microsoft Excel."
        rows = [
            {"n": name, "ci": concept, "k": "technical", "o": "declared", "l": "unknown",
             "e": ["Menciona la herramienta sin describir nivel de uso."], "q": [source]}
            for name, concept in (("Microsoft Word", "office.word"), ("Word", "office.word"),
                                  ("Microsoft Excel", "office.excel"))
        ]
        _, compact = validate_fragment({"sk": rows}, source)
        plan = {"h": None, "s": None, "sk": [["sk0", "sk1"], ["sk2"]], "x": [], "ed": [], "l": [], "c": []}
        profile, _ = validate_and_build_profile_with_effective_plan(plan, [compact], 42)
        self.assertEqual([skill["name"] for skill in profile["skills"]], ["Microsoft Word", "Microsoft Excel"])
        self.assertTrue(all(skill["level"] == "unknown" for skill in profile["skills"]))
