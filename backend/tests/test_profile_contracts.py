import unittest
import json
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.cv_pipeline import normalize_candidate_profile, process_offer_snapshot, process_cv_text


def empty_cv_plan(**overrides):
    plan = {"h": None, "s": None, "sk": [], "x": [], "ed": [], "l": [], "c": []}
    plan.update(overrides)
    return plan


class ProfileContractTests(unittest.IsolatedAsyncioTestCase):
    def test_truncated_block_split_falls_back_to_40_character_chunks_without_loss(self):
        from app.services.cv_packets import _split_text_exact
        from app.services.rate_limiter import ProviderResponseTruncated

        block = "X" * 316
        left, right = _split_text_exact(block)
        self.assertEqual((len(left), len(right)), (158, 158))
        self.assertEqual(left + right, block)

        left, right = _split_text_exact("Y" * 80)
        self.assertEqual((len(left), len(right)), (40, 40))
        self.assertEqual(left + right, "Y" * 80)

        with self.assertRaises(ProviderResponseTruncated):
            _split_text_exact("Z" * 79)

    @patch("app.services.cv_pipeline._call_async", new_callable=AsyncMock)
    async def test_offer_prompt_uses_v2_and_preserves_authoritative_requirements(self, call):
        call.return_value = {"schema_version": "job-offer.v2", "offer_id": "12", "title": "Developer"}
        result = await process_offer_snapshot({"title": "Developer", "requirements": []}, 12)
        self.assertEqual(result["schema_version"], "job-offer.v2")
        self.assertEqual(result["offer_id"], "12")
        prompt = call.call_args.args[0]
        self.assertIn("job-offer.v2", prompt)
        self.assertIn("obligatorio", prompt)
        self.assertIn("## Extracción de oferta job-offer.v2", prompt)

    @patch("app.services.cv_pipeline._call_async", new_callable=AsyncMock)
    async def test_offer_keeps_original_requirement_conditions_by_id(self, call):
        call.return_value = {
            "schema_version": "job-offer.v2", "offer_id": "12", "title": "Chofer",
            "requirements": [{"id": "exp-1", "category": "experience", "name": "3 años de experiencia",
                              "mandatory": True}],
        }
        snapshot = {"requirements": [{"id": "exp-1", "category": "experience", "name": "2 años de experiencia",
                                      "mandatory": True, "minimum_years": 2.0, "source": "Oferta original"}]}
        result = await process_offer_snapshot(snapshot, 12)
        self.assertEqual(result["requirements"][0]["name"], "2 años de experiencia")
        self.assertEqual(result["requirements"][0]["minimum_years"], 2.0)

    @patch("app.services.cv_pipeline._call_async", new_callable=AsyncMock)
    async def test_extraction_restores_single_leaf_at_cv_budget_and_validates_ambiguous_plan(self, call):
        async def fragment_then_profile(_system, payload, marker, max_output_tokens=None):
            request = json.loads(payload)
            self.assertEqual(max_output_tokens, 950)
            if marker.endswith("consolidation-plan"):
                self.assertEqual(request["candidate_id"], "12")
                return empty_cv_plan(h="Conductor")
            return {"h": "Conductor"}

        call.side_effect = fragment_then_profile
        result = await process_cv_text("Synthetic CV", 12)
        self.assertEqual(result["profile_id"], "12")
        self.assertEqual(result["schema_version"], "candidate-profile.v2")
        self.assertEqual(call.await_count, 1)
        self.assertEqual(result["headline"], "Conductor")
        self.assertIn("-p-", call.await_args_list[0].args[2])
        self.assertIn("aliases compactos", call.await_args_list[0].args[0])

        async def wrong_identity(_system, _payload, marker, max_output_tokens=None):
            if marker.endswith("consolidation-plan"):
                return {"v": "candidate-profile.v2", "id": "wrong"}
            return {"l": [{"n": "Español"}, {"n": "español"}]}
        call.side_effect = wrong_identity
        with self.assertRaisesRegex(ValueError, "plan de consolidación"):
            await process_cv_text("Synthetic CV", 12)

    @patch("app.services.cv_pipeline._call_async", new_callable=AsyncMock)
    async def test_extraction_rejects_unquoted_skill_and_user_rejections(self, call):
        call.return_value = {"sk": [{"n": "Grua", "o": "declared", "k": "technical",
                                     "e": ["Experiencia declarada"], "q": ["grúa"]}]}
        with self.assertRaisesRegex(ValueError, "citas que no aparecen"):
            await process_cv_text("Synthetic CV", 12)

    @patch("app.services.cv_pipeline._call_async", new_callable=AsyncMock)
    async def test_extraction_splits_into_blocks_then_consolidates_and_rejects_truncation(self, call):
        from app.core.config import settings
        from app.services.rate_limiter import ProviderResponseTruncated

        with patch.object(settings, "CV_PACKET_CHARACTERS", 80):
            async def responder(_system, payload, marker, max_output_tokens=None):
                self.assertEqual(max_output_tokens, settings.CV_EXTRACTION_MAX_TOKENS)
                if marker.endswith("consolidation-plan"):
                    return empty_cv_plan()
                return {"s": json.loads(payload)["text"]}

            call.side_effect = responder
            result = await process_cv_text("A" * 160, 42)
            self.assertEqual(result["schema_version"], "candidate-profile.v2")
            self.assertEqual(call.await_count, 3)
            self.assertTrue(all(item.kwargs["max_output_tokens"] == settings.CV_EXTRACTION_MAX_TOKENS for item in call.await_args_list))

            call.reset_mock()
            call.side_effect = ProviderResponseTruncated("incomplete")
            with self.assertRaises(ProviderResponseTruncated):
                await process_cv_text("block-1\nblock-2", 42)
            self.assertEqual(call.await_count, 1)

    @patch("app.services.cv_pipeline._call_async", new_callable=AsyncMock)
    async def test_truncated_block_splits_losslessly_and_merges_original_quotes(self, call):
        from app.services.cv_pipeline import process_cv_text
        from app.services.rate_limiter import ProviderResponseTruncated

        text = "Manejo de camiones. " * 18
        child_blocks = []
        initial_truncated = False

        async def responder(_system, payload, marker, max_output_tokens=None):
            nonlocal initial_truncated
            request = json.loads(payload)
            if "-p-" in marker and len(request["text"]) == len(text) and not initial_truncated:
                initial_truncated = True
                raise ProviderResponseTruncated("incomplete synthetic response")
            if marker.endswith("consolidation-plan"):
                self.assertEqual([item["id"] for item in request["facts"]["sk"]], ["sk0", "sk1"])
                return empty_cv_plan(sk=[["sk0", "sk1"]], h="Conductor de camiones")
            child_blocks.append(request["text"])
            return {"sk": [{
                "n": "Conducción de camiones", "k": "technical", "o": "inferred",
                "e": ["El CV describe manejo de camiones."], "q": [request["text"][:20].strip()],
            }]}

        call.side_effect = responder
        result = await process_cv_text(text, 42)
        self.assertEqual("".join(child_blocks), text)
        self.assertEqual(result["skills"][0]["source_quotes"], ["Manejo de camiones."])
        self.assertEqual(len(result["skills"][0]["evidence"]), 1)
        self.assertEqual(call.await_count, 4)

    @patch("app.services.cv_pipeline._call_async", new_callable=AsyncMock)
    async def test_cv_rate_limit_wait_keeps_previous_blocks_and_renews_lease(self, call):
        from app.core.config import settings
        from app.services.rate_limiter import RateLimitExceeded

        attempts = {}

        async def responder(_system, payload, marker, max_output_tokens=None):
            attempts[marker] = attempts.get(marker, 0) + 1
            self.assertEqual(max_output_tokens, settings.CV_EXTRACTION_MAX_TOKENS)
            request = json.loads(payload)
            if "packet_index" in request and request["packet_index"] == 2 and attempts[marker] == 1:
                raise RateLimitExceeded(1)
            if marker.endswith("consolidation-plan"):
                return empty_cv_plan()
            return {}

        call.side_effect = responder
        heartbeat = MagicMock()
        with patch.object(settings, "CV_PACKET_CHARACTERS", 80), \
             patch("app.services.cv_pipeline.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await process_cv_text("A" * 160, 42, on_heartbeat=heartbeat)

        packet_markers = [marker for marker in attempts if "-p-" in marker]
        self.assertEqual(len(packet_markers), 2)
        counts = [attempts[marker] for marker in packet_markers]
        self.assertEqual(counts, [1, 2])
        self.assertEqual(sum(value for marker, value in attempts.items() if marker.endswith("-consolidation-plan")), 1)
        self.assertEqual(sleep.await_args_list[0].args, (1.0,))
        self.assertGreaterEqual(heartbeat.call_count, 5)

    @patch("app.services.cv_pipeline._call_async", new_callable=AsyncMock)
    async def test_lost_lease_during_cv_rate_wait_aborts_without_retrying_provider(self, call):
        from app.services.cv_pipeline import process_cv_text
        from app.services.job_store import LockOwnershipLost
        from app.services.rate_limiter import RateLimitExceeded

        call.side_effect = RateLimitExceeded(60)
        heartbeat = MagicMock(side_effect=[None, LockOwnershipLost("lease lost")])
        with patch("app.services.cv_pipeline.asyncio.sleep", new_callable=AsyncMock):
            with self.assertRaises(LockOwnershipLost):
                await process_cv_text("Synthetic CV", 42, on_heartbeat=heartbeat)
        self.assertEqual(call.await_count, 1)

    @patch("app.services.cv_pipeline._call_async", new_callable=AsyncMock)
    async def test_normalization_translates_only_existing_paths_and_requires_complete_map(self, call):
        profile = {
            "schema_version": "candidate-profile.v2", "profile_id": "12",
            "headline": "Professional driver", "skills": [{"name": "Truck driving", "origin": "user_declared",
                "kind": "technical", "evidence": ["Added by candidate"]}],
            "languages": [{"name": "English", "level": "professional"}],
            "credentials": [{"name": "Class A CDL", "issuer": "State DOT", "expires_at": "2027-01-01",
                "origin": "user_declared", "evidence": ["Candidate declaration"]}],
            "rejected_skills": [{"name": "Forklift", "reason": "Corrección del postulante"}],
        }
        call.return_value = {"translations": {
            "/headline": "Conductor profesional", "/skills/0/name": "Conducción de camiones",
            "/skills/0/evidence/0": "Agregada por el postulante", "/languages/0/name": "Inglés",
        }}
        result = await normalize_candidate_profile(profile, 12)
        self.assertEqual(result["skills"][0]["origin"], "user_declared")
        self.assertEqual(result["credentials"][0]["name"], "Class A CDL")
        self.assertEqual(result["credentials"][0]["expires_at"], "2027-01-01")
        self.assertEqual(result["rejected_skills"][0]["name"], profile["rejected_skills"][0]["name"])
        self.assertEqual(result["rejected_skills"][0]["reason"], profile["rejected_skills"][0]["reason"])
        call.return_value = {"translations": {"/profile_id": "attacker"}}
        with self.assertRaisesRegex(ValueError, "omitió textos"):
            await normalize_candidate_profile(profile, 12)


if __name__ == "__main__":
    unittest.main()
