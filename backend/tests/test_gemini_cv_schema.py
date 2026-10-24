"""Constrained CV schemas preserve wire shapes while unrelated flows opt out."""

import copy
import json
import unittest

from app.services.cv_compact_contract import fragment_schema, validate_fragment
from app.services.cv_consolidation import ConsolidationPlan
from app.services.gemini_cv_schema import cv_response_schema, flatten_cv_schema


class GeminiCVSchemaTests(unittest.TestCase):
    def test_real_fragment_resolves_refs_and_preserves_enum_array_and_nullable_types(self):
        original = fragment_schema()
        before = copy.deepcopy(original)
        flattened = flatten_cv_schema(original)
        self.assertEqual(original, before)
        self.assertEqual(flattened["type"], "object")
        self.assertEqual(set(flattened["properties"]), {"h", "s", "sk", "x", "pr", "ed", "l", "p", "c"})
        self.assertEqual(flattened["properties"]["h"], {"type": ["string", "null"]})
        skill = flattened["properties"]["sk"]["items"]
        self.assertEqual(skill["properties"]["l"]["enum"],
                         ["beginner", "intermediate", "advanced", "expert", "unknown"])
        self.assertEqual(skill["properties"]["k"]["enum"], ["technical", "soft", "unknown"])
        self.assertEqual(skill["properties"]["q"], {"type": "array", "items": {"type": "string"}})
        self.assertEqual(skill["properties"]["y"], {"type": ["number", "null"]})
        self.assertEqual(skill["required"], ["n"])
        self.assertFalse(skill["additionalProperties"])
        experience = flattened["properties"]["x"]["items"]
        self.assertEqual(experience["properties"]["m"], {"type": ["integer", "null"]})
        education = flattened["properties"]["ed"]["items"]
        self.assertEqual(education["properties"]["c"], {"type": ["boolean", "null"]})

    def test_plan_retains_mandatory_sections_and_nested_reference_groups(self):
        flattened = flatten_cv_schema(ConsolidationPlan.model_json_schema(by_alias=True))
        self.assertEqual(flattened["required"], ["sk", "x", "ed", "l", "c"])
        for alias in flattened["required"]:
            self.assertEqual(flattened["properties"][alias],
                             {"type": "array", "items": {"type": "array", "items": {"type": "string"}}})

    def test_decoder_schema_omits_bounds_but_local_validation_still_rejects_oversized_evidence(self):
        flattened = flatten_cv_schema(fragment_schema())
        skill = flattened["properties"]["sk"]["items"]
        self.assertNotIn("maxItems", skill["properties"]["e"])
        self.assertNotIn("maxLength", skill["properties"]["n"])
        source = "Herramienta sintética."
        invalid = {"sk": [{"n": "Herramienta sintética", "e": ["x" * 161], "q": [source]}]}
        with self.assertRaises(ValueError):
            validate_fragment(invalid, source)

    def test_selects_cv_packet_and_consolidation_envelopes_only(self):
        packet = {"text": "Synthetic document.", "fragment_schema": fragment_schema()}
        plan = {"facts": {}, "candidate_profile_version": "candidate-profile.v2",
                "plan_schema": ConsolidationPlan.model_json_schema(by_alias=True)}
        selected = cv_response_schema(json.dumps(packet))
        self.assertIn("sk", selected["required"])
        skill = selected["properties"]["sk"]["items"]
        self.assertEqual(set(skill["required"]), {"n", "k", "l", "o", "e", "q"})
        self.assertNotIn("y", skill["required"])
        self.assertNotIn("ci", skill["required"])
        self.assertIn("unknown", skill["properties"]["l"]["enum"])
        expected_plan = flatten_cv_schema(plan["plan_schema"])
        expected_plan["required"].append("pr")
        self.assertEqual(cv_response_schema(json.dumps(plan)), expected_plan)
        unrelated = [
            "plain prompt", "[]", "null", "42",
            json.dumps({"candidate_profile": {}, "job_offer": {}, "requirements": []}),
            json.dumps({"data": {}, "dimensions": {}}),
            json.dumps({"profile": {}, "profile_schema": fragment_schema()}),
            json.dumps({"snapshot": {}, "schema": {"type": "object"}}),
            json.dumps({"data": {"text": "CV", "fragment_schema": fragment_schema()}}),
            json.dumps({"fragment_schema": fragment_schema()}),
            json.dumps({"plan_schema": plan["plan_schema"], "facts": {}}),
            json.dumps({**packet, **plan}),
        ]
        for payload in unrelated:
            with self.subTest(payload=payload[:50]):
                self.assertIsNone(cv_response_schema(payload))

    def test_invalid_or_recursive_shapes_fail_without_echoing_schema_content(self):
        recursive = {"$defs": {"Node": {"type": "object", "properties": {
            "next": {"$ref": "#/$defs/Node"}}}}, "$ref": "#/$defs/Node"}
        invalid = [
            None, [], {"type": "string"},
            {"$ref": "https://invalid.example/private-sentinel"}, recursive,
            {"type": "object", "properties": {"x": {"$ref": "#/$defs/missing-private-sentinel"}}},
            {"type": "object", "properties": {"x": {"type": "array"}}},
            {"type": "object", "properties": {}, "required": ["private-sentinel"]},
            {"type": "object", "properties": {"x": {"anyOf": [{"type": "string"}, {"type": "number"}]}}},
        ]
        for schema in invalid:
            with self.subTest(schema_type=type(schema).__name__):
                with self.assertRaises(ValueError) as error:
                    flatten_cv_schema(schema)
                self.assertNotIn("private-sentinel", str(error.exception))

    def test_pydantic_ref_with_default_metadata_is_flattened_without_losing_its_enum(self):
        original = {"$defs": {"Choice": {"type": "string", "enum": ["yes", "no"]}},
                    "type": "object", "properties": {"choice": {
                        "$ref": "#/$defs/Choice", "default": "yes", "title": "Choice"}}}
        self.assertEqual(flatten_cv_schema(original)["properties"]["choice"],
                         {"type": "string", "enum": ["yes", "no"]})
