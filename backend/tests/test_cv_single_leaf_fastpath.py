"""Deterministic assembly keeps validated facts and avoids only safe LLM calls."""

import copy
import json
import unittest
from unittest.mock import patch

from app.services.cv_packets import (
    PacketsIdentityError, _single_fragment_plan, make_packet_identity,
    process_packetized_cv_text, validate_manifest,
)
from app.services.cv_consolidation import compact_facts_for_plan
from app.services.rate_limiter import ProviderResponseTruncated


SOURCE = "Registré facturas y concilié diferencias. Expliqué opciones a clientes. Secundario finalizado."


def fragment():
    return {
        "h": "Auxiliar de administración", "s": "Registro de facturas y atención de consultas.",
        "sk": [
            {"n": "Registro de facturas", "k": "technical", "e": ["Registró facturas."],
             "q": ["Registré facturas"], "ci": "administration.invoice_recording"},
            {"n": "Comunicación", "k": "soft", "o": "inferred", "l": "unknown",
             "e": ["Explicó opciones a clientes."], "q": ["Expliqué opciones a clientes."],
             "ci": "soft.communication"},
        ],
        "x": [{"t": "Auxiliar", "o": "Comercio sintético", "h": ["Registro de facturas"]}],
        "ed": [{"l": "secondary", "c": True, "q": ["Secundario finalizado."]}], "l": [{"n": "Español", "l": "native"}],
        "p": {"l": ["Ciudad sintética"], "w": ["onsite"]},
    }


def singleton_plan(facts):
    return {"h": None, "s": None,
            **{alias: [[row["id"]] for row in rows] for alias, rows in facts.items()}}


class SingleLeafAmbiguityTests(unittest.TestCase):
    def test_possible_duplicates_in_every_section_require_semantic_consolidation(self):
        cases = [
            {"sk": [{"n": "Comunicación"}, {"n": " comunicación "}]},
            {"sk": [{"n": "Comunicación", "ci": "soft.communication"},
                    {"n": "Comunicación interpersonal", "ci": "soft.communication"}]},
            {"x": [{"t": "Auxiliar", "o": "Comercio"}, {"t": "Auxiliar"}]},
            {"ed": [{"l": "secondary", "i": "Escuela"}, {"l": "unknown"}]},
            {"l": [{"n": "Español"}, {"n": " español "}]},
            {"c": [{"n": "Carnet", "i": "Municipio"}, {"n": "Carnet"}]},
        ]
        for value in cases:
            with self.subTest(section=next(iter(value))):
                self.assertIsNone(_single_fragment_plan([value], compact_facts_for_plan([value])))

    def test_known_distinct_identity_components_allow_singletons(self):
        value = {
            "sk": [{"n": "Atención al cliente"}, {"n": "Comunicación"}],
            "x": [{"t": "Auxiliar", "o": "Comercio A"}, {"t": "Auxiliar", "o": "Comercio B"}],
            "ed": [{"l": "secondary"}, {"l": "bachelor"}],
            "l": [{"n": "Español"}, {"n": "Inglés"}],
            "c": [{"n": "Carnet", "d": "2020"}, {"n": "Carnet", "d": "2024"}],
        }
        facts = compact_facts_for_plan([value])
        self.assertEqual(_single_fragment_plan([value], facts), singleton_plan(facts))


class SingleLeafFastPathTests(unittest.IsolatedAsyncioTestCase):
    def identity(self, text=SOURCE, packet_chars=12000):
        return make_packet_identity(
            job_id="synthetic-fastpath", analysis_id=800, candidate_id=42, cv_version_id=100,
            source_hash="a" * 64, text=text, prompt_version="cv-extraction.v2-packets-soft.v4",
            provider="gemini", model="synthetic-model", packet_chars=packet_chars,
            block_chars=12000, budget=8192,
        )

    async def run_pipeline(self, provider, *, checkpoint=None, text=SOURCE, packet_chars=12000):
        snapshots = []
        with patch("app.services.cv_packets.load_prompt", return_value="synthetic prompt"):
            result = await process_packetized_cv_text(
                text, 42, identity=self.identity(text, packet_chars), checkpoint=checkpoint,
                on_checkpoint=snapshots.append, on_heartbeat=None, call_provider=provider,
            )
        return result, snapshots

    async def test_one_valid_leaf_skips_consolidation_and_checkpoint_rebuild_preserves_summary(self):
        calls = []

        async def provider(_system, _payload, marker, **_kwargs):
            calls.append(marker)
            self.assertFalse(marker.endswith("consolidation-plan"))
            return {**fragment(), "__provider_model": "synthetic-provider-version"}

        (profile, model), snapshots = await self.run_pipeline(provider)
        self.assertEqual(len(calls), 1)
        self.assertEqual(profile["headline"], fragment()["h"])
        self.assertEqual(profile["summary"], fragment()["s"])
        self.assertEqual(model, "synthetic-provider-version")
        soft = profile["skills"][1]
        self.assertEqual((soft["origin"], soft["level"]), ("inferred", "unknown"))
        self.assertEqual(soft["source_quotes"], ["Expliqué opciones a clientes."])
        self.assertEqual(profile["preferences"]["locations"], ["Ciudad sintética"])
        checkpoint = snapshots[-1]
        self.assertEqual(checkpoint["consolidation"]["plan"]["sk"], [["sk0"], ["sk1"]])

        async def forbidden(*_args, **_kwargs):
            self.fail("Completed or pending local consolidation must not call a provider.")

        (restored, restored_model), _ = await self.run_pipeline(forbidden, checkpoint=checkpoint)
        self.assertEqual((restored, restored_model), (profile, model))
        # Recovery after the leaf save also assembles locally without re-extraction.
        (resumed, _), _ = await self.run_pipeline(forbidden, checkpoint=snapshots[-2])
        self.assertEqual(resumed, profile)
        tampered = copy.deepcopy(checkpoint)
        tampered["consolidation"]["result"]["summary"] = "Invented summary"
        with self.assertRaises(PacketsIdentityError):
            validate_manifest(tampered, SOURCE, self.identity())

    async def test_canonical_soft_aliases_merge_before_plan_and_preserve_evidence_without_extra_call(self):
        calls = []

        async def provider(_system, payload, marker, **_kwargs):
            calls.append(marker)
            if marker.endswith("consolidation-plan"):
                value = singleton_plan(json.loads(payload)["facts"])
                value["sk"] = [["sk0", "sk1"]]
                return value
            return {"sk": [
                {"n": "Comunicación", "k": "soft", "ci": "soft.communication",
                 "e": ["Explicó opciones."], "q": ["Expliqué opciones"]},
                {"n": "Comunicación interpersonal", "k": "soft", "ci": "soft.communication",
                 "e": ["Atendió clientes."], "q": ["clientes."]},
            ]}

        (profile, _), _ = await self.run_pipeline(provider)
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[-1].endswith("consolidation-plan"))
        self.assertEqual(len(profile["skills"]), 1)
        self.assertEqual(profile["skills"][0]["source_quotes"], ["Expliqué opciones", "clientes."])

    async def test_one_root_split_into_two_leaves_still_consolidates(self):
        text = "Primera tarea de administración. " * 4 + "Segunda tarea de atención. " * 4
        calls = []

        async def provider(_system, payload, marker, **_kwargs):
            calls.append(marker)
            if marker.endswith("consolidation-plan"):
                return singleton_plan(json.loads(payload)["facts"])
            if len(calls) == 1:
                raise ProviderResponseTruncated("Synthetic truncation")
            return {}

        (_, _), snapshots = await self.run_pipeline(provider, text=text)
        self.assertEqual(len(snapshots[-1]["roots"]), 1)
        self.assertEqual(len(calls), 4)
        self.assertTrue(calls[-1].endswith("consolidation-plan"))
