"""Acceptance tests for job-isolated, resumable packet extraction.

Every provider response below is synthetic and derived only from the supplied
synthetic packet text. No credentials or live provider calls are used.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from app.core.config import settings
from app.services import cv_pipeline
from app.services.cv_packets import PacketsIdentityError, make_packet_identity
from app.services.rate_limiter import ProviderResponseTruncated


def identity_for(text: str, *, job_id: str, analysis_id: int, candidate_id: int, cv_version_id: int, source_salt: str):
    provider = settings.LLM_PROVIDER.strip().lower()
    model = settings.OPENAI_MODEL if provider == "openai" else settings.CV_MODEL
    source_hash = hashlib.sha256(source_salt.encode("utf-8")).hexdigest()
    return make_packet_identity(
        job_id=job_id,
        analysis_id=analysis_id,
        candidate_id=candidate_id,
        cv_version_id=cv_version_id,
        source_hash=source_hash,
        text=text,
        prompt_version=cv_pipeline.CV_EXTRACTION_PROMPT_VERSION,
        provider=settings.LLM_PROVIDER,
        model=model,
        packet_chars=settings.CV_PACKET_CHARACTERS,
        block_chars=settings.CV_BLOQUE_CARACTERES,
        budget=settings.CV_EXTRACTION_MAX_TOKENS,
    )


def profile_plan(request: dict) -> dict:
    facts = request["facts"]
    return {
        "h": None,
        "s": None,
        "sk": [[fact["id"]] for fact in facts["sk"]],
        "x": [[fact["id"]] for fact in facts["x"]],
        "ed": [[fact["id"]] for fact in facts["ed"]],
        "l": [[fact["id"]] for fact in facts["l"]],
        "c": [[fact["id"]] for fact in facts["c"]],
    }


def packet_fragment(packet_text: str, *, label: str | None = None) -> dict:
    quote = packet_text.strip()
    name = label or f"Competencia {hashlib.sha256(quote.encode()).hexdigest()[:8]}"
    return {
        "sk": [{
            "n": name,
            "k": "technical",
            "l": "advanced",
            "o": "declared",
            "e": [f"Evidencia documentada para {name}"],
            "q": [quote],
        }],
        "x": [], "ed": [], "l": [], "p": {}, "c": [],
    }


async def fake_provider(_system: str, payload: str, marker: str, *, on_heartbeat=None):
    request = json.loads(payload)
    if marker.endswith("consolidation-plan"):
        return profile_plan(request)
    return packet_fragment(request["text"])


class CVPacketIsolationAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_quote_is_retried_once_without_publishing_invalid_packet(self):
        text = "Usé Excel para registrar facturas."
        identity = identity_for(text, job_id=str(uuid4()), analysis_id=1101,
                                candidate_id=73, cv_version_id=9201, source_salt="quote-retry")
        packet_calls = []
        snapshots = []

        async def provider(system, payload, marker, *, on_heartbeat=None):
            request = json.loads(payload)
            if marker.endswith("consolidation-plan"):
                return profile_plan(request)
            packet_calls.append(system)
            result = packet_fragment(request["text"], label="Excel")
            if len(packet_calls) == 1:
                result["sk"][0]["q"] = ["Texto inventado que no aparece en el CV"]
            return result

        result = await self.process(text, identity, provider=provider,
                                    on_checkpoint=lambda value: snapshots.append(copy.deepcopy(value)))
        self.assertEqual(len(packet_calls), 2)
        self.assertIn("La respuesta anterior no pasó la validación", packet_calls[1])
        self.assertEqual(result["skills"][0]["source_quotes"], [text])
        self.assertNotIn("Texto inventado", json.dumps(snapshots))

    async def test_repeated_invalid_quote_stops_after_two_calls(self):
        text = "Usé Excel para registrar facturas."
        identity = identity_for(text, job_id=str(uuid4()), analysis_id=1102,
                                candidate_id=73, cv_version_id=9202, source_salt="quote-stop")
        calls = []

        async def provider(system, payload, marker, *, on_heartbeat=None):
            calls.append(marker)
            result = packet_fragment(text)
            result["sk"][0]["q"] = ["Una cita inventada"]
            return result

        with self.assertRaises(ValueError):
            await self.process(text, identity, provider=provider)
        self.assertEqual(len(calls), 2)

    async def process(self, text, identity, *, checkpoint=None, on_checkpoint=None, provider=None):
        with patch.object(cv_pipeline, "_call_cv_provider", side_effect=provider or fake_provider):
            return await cv_pipeline.process_cv_text(
                text,
                identity["candidate_id"],
                identity=identity,
                checkpoint=checkpoint,
                on_checkpoint=on_checkpoint,
            )

    async def test_same_candidate_jobs_interleave_without_crossing_cv_facts(self):
        candidate_id = 73
        job_a, job_b = str(uuid4()), str(uuid4())
        text_a = "CV_A: Licencia de grúa volcánica código A-172"
        text_b = "CV_B: Carnet sanitario del puerto código B-284"
        id_a = identity_for(text_a, job_id=job_a, analysis_id=501, candidate_id=candidate_id, cv_version_id=9001, source_salt="file-a")
        id_b = identity_for(text_b, job_id=job_b, analysis_id=502, candidate_id=candidate_id, cv_version_id=9002, source_salt="file-b")
        both_packet_calls = asyncio.Event()
        packet_entries: list[tuple[str, str]] = []
        events: list[tuple[str, str]] = []
        checkpoint_copies: dict[str, list[dict]] = {job_a: [], job_b: []}

        async def provider(_system, payload, marker, *, on_heartbeat=None):
            request = json.loads(payload)
            job = job_a if job_a in marker else job_b
            if marker.endswith("consolidation-plan"):
                events.append((job, "plan"))
                return profile_plan(request)
            packet_text = request["text"]
            packet_entries.append((job, packet_text))
            events.append((job, "packet-enter"))
            if len(packet_entries) == 2:
                both_packet_calls.set()
            await asyncio.wait_for(both_packet_calls.wait(), timeout=3)
            events.append((job, "packet-exit"))
            return packet_fragment(packet_text, label=f"Habilidad {job[-6:]}")

        async def save_a(manifest): checkpoint_copies[job_a].append(manifest)
        async def save_b(manifest): checkpoint_copies[job_b].append(manifest)

        with patch.object(cv_pipeline, "_call_cv_provider", side_effect=provider):
            result_a, result_b = await asyncio.gather(
                cv_pipeline.process_cv_text(text_a, candidate_id, identity=id_a, on_checkpoint=save_a),
                cv_pipeline.process_cv_text(text_b, candidate_id, identity=id_b, on_checkpoint=save_b),
            )

        self.assertEqual(len(packet_entries), 2)
        self.assertEqual({event for _, event in events[:2]}, {"packet-enter"})
        self.assertTrue(any(event == "packet-exit" for _, event in events[2:]))
        self.assertEqual(result_a["profile_id"], str(candidate_id))
        self.assertEqual(result_b["profile_id"], str(candidate_id))
        self.assertEqual(result_a["skills"][0]["source_quotes"], [text_a])
        self.assertEqual(result_b["skills"][0]["source_quotes"], [text_b])
        self.assertNotIn(text_b, result_a["skills"][0]["source_quotes"])
        self.assertNotIn(text_a, result_b["skills"][0]["source_quotes"])
        self.assertEqual(checkpoint_copies[job_a][-1]["identity"]["job_id"], job_a)
        self.assertEqual(checkpoint_copies[job_b][-1]["identity"]["job_id"], job_b)

    async def test_checkpoint_from_another_job_or_changed_source_is_rejected_before_provider(self):
        text = "CV synthetic: acredita grúa A-172"
        job_a, job_b = str(uuid4()), str(uuid4())
        base = identity_for(text, job_id=job_a, analysis_id=101, candidate_id=73, cv_version_id=8001, source_salt="source-one")
        checkpoints: list[dict] = []
        original = await self.process(text, base, on_checkpoint=checkpoints.append)
        saved = checkpoints[-1]
        self.assertEqual(original["skills"][0]["source_quotes"], [text])

        other_job = identity_for(text, job_id=job_b, analysis_id=102, candidate_id=73, cv_version_id=8002, source_salt="source-two")
        other_text = "CV synthetic: acredita grúa B-284"
        same_job_changed_source = identity_for(other_text, job_id=job_a, analysis_id=101, candidate_id=73, cv_version_id=8001, source_salt="source-three")
        for identity, source in ((other_job, text), (same_job_changed_source, other_text)):
            provider_calls = []

            async def provider(*args, **kwargs):
                provider_calls.append(args)
                return await fake_provider(*args, **kwargs)

            with self.subTest(job_id=identity["job_id"], source=source), self.assertRaises(PacketsIdentityError):
                await self.process(source, identity, checkpoint=saved, provider=provider)
            self.assertEqual(provider_calls, [])

    async def test_mutated_packet_id_hash_or_range_is_rejected_before_provider(self):
        text = "CV synthetic: operación de grúa con licencia A-172"
        identity = identity_for(text, job_id=str(uuid4()), analysis_id=111, candidate_id=73, cv_version_id=8111, source_salt="tamper-source")
        checkpoints: list[dict] = []
        await self.process(text, identity, on_checkpoint=checkpoints.append)
        valid = checkpoints[-1]

        variants = []
        changed_id = copy.deepcopy(valid)
        changed_id["roots"][0] = "p-forged-packet-id"
        variants.append(("id", changed_id))
        changed_hash = copy.deepcopy(valid)
        changed_hash["packets"][changed_hash["roots"][0]]["text_hash"] = "0" * 64
        variants.append(("hash", changed_hash))
        changed_range = copy.deepcopy(valid)
        changed_range["packets"][changed_range["roots"][0]]["start"] = 1
        variants.append(("range", changed_range))

        for label, invalid in variants:
            provider_calls = []

            async def provider(*args, **kwargs):
                provider_calls.append(args)
                return await fake_provider(*args, **kwargs)

            with self.subTest(label=label), self.assertRaises(PacketsIdentityError):
                await self.process(text, identity, checkpoint=invalid, provider=provider)
            self.assertEqual(provider_calls, [])

    async def test_callback_receives_deep_copies_and_cached_completed_profile_needs_no_provider(self):
        text = "CV synthetic: mantenimiento de maquinaria Z-991"
        identity = identity_for(text, job_id=str(uuid4()), analysis_id=121, candidate_id=73, cv_version_id=8121, source_salt="copy-source")
        snapshots: list[dict] = []
        result = await self.process(text, identity, on_checkpoint=snapshots.append)
        self.assertGreaterEqual(len(snapshots), 3)
        self.assertEqual(snapshots[0]["packets"][snapshots[0]["roots"][0]]["status"], "pending")
        self.assertEqual(snapshots[-1]["consolidation"]["status"], "completed")
        self.assertEqual(result, snapshots[-1]["consolidation"]["result"])

        calls = []

        async def no_provider(*args, **kwargs):
            calls.append(args)
            raise AssertionError("A completed job checkpoint should return its cached profile.")

        cached = await self.process(text, identity, checkpoint=snapshots[-1], provider=no_provider)
        self.assertEqual(cached, result)
        self.assertEqual(calls, [])

    async def test_completed_consolidation_with_pending_leaf_or_missing_facts_is_rejected(self):
        text = "CV synthetic: atención de equipo hidráulico H-551"
        identity = identity_for(text, job_id=str(uuid4()), analysis_id=131, candidate_id=73, cv_version_id=8131, source_salt="integrity-source")
        checkpoints: list[dict] = []
        await self.process(text, identity, on_checkpoint=checkpoints.append)
        valid = checkpoints[-1]

        pending_leaf = copy.deepcopy(valid)
        leaf = pending_leaf["packets"][pending_leaf["roots"][0]]
        leaf.update(status="pending", result=None, provider_model=None)
        missing_fact = copy.deepcopy(valid)
        missing_fact["consolidation"]["result"]["skills"] = []
        for label, invalid in (("pending leaf", pending_leaf), ("cached result missing skill", missing_fact)):
            provider_calls = []

            async def provider(*args, **kwargs):
                provider_calls.append(args)
                return await fake_provider(*args, **kwargs)

            with self.subTest(label=label), self.assertRaises(PacketsIdentityError):
                await self.process(text, identity, checkpoint=invalid, provider=provider)
            self.assertEqual(provider_calls, [])

    async def test_crash_after_a_packet_is_saved_resumes_without_resending_completed_text(self):
        text = "PAQUETE_A " + "a" * 82 + "\nPAQUETE_B " + "b" * 82 + "\nPAQUETE_C " + "c" * 82
        with patch.object(settings, "CV_PACKET_CHARACTERS", 80), patch.object(settings, "CV_BLOQUE_CARACTERES", 400):
            identity = identity_for(text, job_id=str(uuid4()), analysis_id=141, candidate_id=73, cv_version_id=8141, source_salt="resume-source")
            saved: list[dict] = []
            first_run_packets: list[str] = []
            checkpoint_calls = 0

            async def persist_then_crash(manifest):
                nonlocal checkpoint_calls
                saved.append(manifest)
                checkpoint_calls += 1
                # First callback initializes the pending manifest; second records
                # the first completed packet, then simulates a worker crash.
                if checkpoint_calls == 2:
                    raise RuntimeError("synthetic worker interruption after persisted page")

            async def provider(_system, payload, marker, *, on_heartbeat=None):
                request = json.loads(payload)
                if marker.endswith("consolidation-plan"):
                    return profile_plan(request)
                first_run_packets.append(request["text"])
                return packet_fragment(request["text"])

            with patch.object(cv_pipeline, "_call_cv_provider", side_effect=provider):
                with self.assertRaisesRegex(RuntimeError, "synthetic worker interruption"):
                    await cv_pipeline.process_cv_text(text, 73, identity=identity, on_checkpoint=persist_then_crash)
            checkpoint = saved[-1]
            completed = {packet["text_hash"] for packet in checkpoint["packets"].values() if packet["status"] == "completed"}
            self.assertTrue(completed)
            completed_texts = {text[packet["start"]:packet["end"]] for packet in checkpoint["packets"].values() if packet["status"] == "completed"}

            resumed_packets: list[str] = []

            async def resume_provider(_system, payload, marker, *, on_heartbeat=None):
                request = json.loads(payload)
                if marker.endswith("consolidation-plan"):
                    return profile_plan(request)
                resumed_packets.append(request["text"])
                return packet_fragment(request["text"])

            with patch.object(cv_pipeline, "_call_cv_provider", side_effect=resume_provider):
                result = await cv_pipeline.process_cv_text(text, 73, identity=identity, checkpoint=checkpoint)

        self.assertTrue(first_run_packets)
        self.assertTrue(resumed_packets)
        self.assertTrue(completed_texts.isdisjoint(resumed_packets))
        self.assertGreaterEqual(len(result["skills"]), len(resumed_packets) + len(completed_texts))

    async def test_split_parent_is_saved_and_skipped_when_resuming_children(self):
        text = "CV_SPLIT_START " + "x" * 50 + "\nCV_SPLIT_END " + "y" * 60
        with patch.object(settings, "CV_PACKET_CHARACTERS", 300), patch.object(settings, "CV_BLOQUE_CARACTERES", 500):
            identity = identity_for(text, job_id=str(uuid4()), analysis_id=151, candidate_id=73, cv_version_id=8151, source_salt="split-source")
            saved: list[dict] = []
            provider_calls = 0

            async def pause_after_split(_system, payload, marker, *, on_heartbeat=None):
                nonlocal provider_calls
                request = json.loads(payload)
                if marker.endswith("consolidation-plan"):
                    return profile_plan(request)
                provider_calls += 1
                if provider_calls == 1:
                    raise ProviderResponseTruncated("synthetic truncation of parent")
                raise RuntimeError("synthetic interruption after persisted split")

            with patch.object(cv_pipeline, "_call_cv_provider", side_effect=pause_after_split):
                with self.assertRaisesRegex(RuntimeError, "persisted split"):
                    await cv_pipeline.process_cv_text(text, 73, identity=identity, on_checkpoint=saved.append)
            split_checkpoint = saved[-1]
            root = split_checkpoint["packets"][split_checkpoint["roots"][0]]
            self.assertEqual(root["status"], "split")
            self.assertEqual(len(root["children"]), 2)
            expected_child_quotes = [text[split_checkpoint["packets"][child]["start"]:split_checkpoint["packets"][child]["end"]].strip() for child in root["children"]]
            parent_marker = f"cv-packet-{identity['job_id']}-{root['id']}"

            resumed_markers: list[str] = []

            async def resume_children(_system, payload, marker, *, on_heartbeat=None):
                if marker.endswith("consolidation-plan"):
                    return profile_plan(json.loads(payload))
                resumed_markers.append(marker)
                return packet_fragment(json.loads(payload)["text"])

            with patch.object(cv_pipeline, "_call_cv_provider", side_effect=resume_children):
                result = await cv_pipeline.process_cv_text(text, 73, identity=identity, checkpoint=split_checkpoint)

        self.assertTrue(resumed_markers)
        self.assertFalse(any(marker == parent_marker for marker in resumed_markers))
        self.assertEqual(len(result["skills"]), 2)
        self.assertEqual([item["source_quotes"][0] for item in result["skills"]], expected_child_quotes)


if __name__ == "__main__":
    unittest.main()
