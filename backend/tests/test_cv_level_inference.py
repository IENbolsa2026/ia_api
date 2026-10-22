"""Offline transport regressions; rubric quality is evaluated with the live fixtures.

Provider stubs deliberately supply the level. These tests verify that validation,
local consolidation and checkpoints preserve it without changing skill origin
or inventing years; they do not pretend to measure a language model's reasoning.
"""

import copy
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from app.core.config import settings
from app.services.cv_compact_contract import validate_fragment
from app.services.cv_packets import _quote_catalog, _resolve_quote_ids, validate_manifest
from app.services.cv_pipeline import CV_EXTRACTION_PROMPT_VERSION, process_cv_text


FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "cv_level_inference.json").read_text(encoding="utf-8"))


class LevelInferenceContractTests(unittest.IsolatedAsyncioTestCase):
    async def extract(self, case, additional_skills=()):
        raw_skills = []
        for expected in case["expectations"]:
            raw_skills.append({
                "n": expected["name"], "k": expected["kind"], "o": expected["origin"],
                "l": expected["stub_level"], "e": [expected["stub_evidence"]],
                "q": list(expected["source_quote_clues"]),
            })
        raw_skills.extend(copy.deepcopy(additional_skills))
        checkpoints = []
        quote_ids = []

        async def provider(_system, payload, marker, max_output_tokens=None):
            self.assertFalse(marker.endswith("consolidation-plan"))
            request = json.loads(payload)
            catalog = request["source_quotes"]
            result = {"h": "Perfil sintético", "s": "Resumen de experiencia sintética.",
                      "sk": copy.deepcopy(raw_skills)}
            for skill in result["sk"]:
                # Exercise the source-owned quote-ID path, including the signal
                # supporting a level estimate. The server must restore literals.
                ids = []
                for clue in skill["q"]:
                    quote_id = next(key for key, value in catalog.items() if clue in value)
                    ids.append(quote_id)
                skill["q"] = list(dict.fromkeys(ids))
                quote_ids.extend(skill["q"])
            if case["id"] == "administracion_menciones_sin_tareas":
                result["x"] = [{"t": "Auxiliar administrativa", "m": 60}]
            return result

        with patch.object(settings, "CV_PACKET_CHARACTERS", 12000), \
             patch.object(settings, "CV_BLOQUE_CARACTERES", 12000), \
             patch.object(settings, "CV_EXTRACTION_MAX_TOKENS", 8192), \
             patch("app.services.cv_pipeline._call_async", new_callable=AsyncMock, side_effect=provider) as call:
            result = await process_cv_text(case["text"], 42, on_checkpoint=checkpoints.append)
        self.assertEqual(call.await_count, 1)
        self.assertTrue(quote_ids)
        manifest = checkpoints[-1]
        self.assertEqual(manifest["identity"]["prompt_version"], CV_EXTRACTION_PROMPT_VERSION)
        self.assertEqual(validate_manifest(manifest, case["text"], manifest["identity"]), manifest)
        return result, manifest

    async def test_multisector_levels_and_quote_evidence_survive_the_existing_contract(self):
        for case in FIXTURE["cases"]:
            with self.subTest(case=case["id"]):
                result, checkpoint = await self.extract(case)
                self.assertEqual(result["schema_version"], "candidate-profile.v2")
                self.assertEqual(len(result["skills"]), len(case["expectations"]))
                for skill, expected in zip(result["skills"], case["expectations"]):
                    self.assertIn(skill["level"], expected["expected_levels"])
                    self.assertEqual(skill["level"], expected["stub_level"])
                    self.assertEqual(skill["origin"], expected["origin"])
                    self.assertEqual(skill["evidence"], [expected["stub_evidence"]])
                    self.assertIsNone(skill["years_experience"])
                    for clue in expected["source_quote_clues"]:
                        self.assertTrue(any(clue in quote for quote in skill["source_quotes"]))
                    for quote in skill["source_quotes"]:
                        self.assertIn(quote, case["text"])
                self.assertEqual(checkpoint["consolidation"]["result"], dict(result))
                self.assertFalse(result["languages"])
                self.assertFalse(result["credentials"])
                if case.get("forbid_soft_skills"):
                    self.assertFalse(any(skill["kind"] == "soft" for skill in result["skills"]))

    async def test_estimated_level_does_not_change_a_declared_skill_to_inferred(self):
        case = next(case for case in FIXTURE["cases"] if case["id"] == "operaciones_casos_complejos_autonomos")
        result, _ = await self.extract(case, [{
            "n": "Pensamiento analítico", "k": "soft", "o": "inferred", "l": "advanced",
            "e": ["Compara mediciones y descarta causas en fallas no rutinarias resueltas sin supervisión."],
            "q": ["comparo mediciones, descarto causas y elijo la reparación"],
            "ci": "soft.analytical_thinking",
        }])
        self.assertEqual([(skill["origin"], skill["level"]) for skill in result["skills"]],
                         [("declared", "advanced"), ("inferred", "advanced")])

    def test_level_inference_cannot_add_new_contract_fields_or_unquoted_evidence(self):
        source = "Clasifiqué expedientes siguiendo una guía."
        catalog = _quote_catalog(source)
        raw = {"sk": [{"n": "Clasificación de expedientes", "k": "technical", "o": "declared",
                       "l": "beginner", "e": ["La ejecución guiada sustenta nivel inicial."], "q": ["quote_0"]}]}
        resolved = _resolve_quote_ids(raw, catalog)
        expanded, _ = validate_fragment(resolved, source)
        self.assertEqual(expanded["skills"][0]["level"], "beginner")
        extra = copy.deepcopy(resolved)
        extra["sk"][0]["level_origin"] = "inferred"
        with self.assertRaises(ValueError):
            validate_fragment(extra, source)
        invented = copy.deepcopy(resolved)
        invented["sk"][0]["q"] = ["Resolví casos complejos sin supervisión."]
        with self.assertRaises(ValueError):
            validate_fragment(invented, source)
