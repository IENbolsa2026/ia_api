import unittest
from unittest.mock import AsyncMock, patch

from app.services.cv_packets import CV_EFFICIENT_PROMPT_VERSION, make_packet_identity, process_packetized_cv_text
from app.services.cv_pipeline import _call_cv_provider
from app.services.rate_limiter import RateLimitExceeded


class ValidationPacingTests(unittest.IsolatedAsyncioTestCase):
    async def test_persisted_repair_yields_worker_then_resumes_without_initial_request(self):
        source = 'Uso Python.'
        identity = make_packet_identity(
            job_id='persisted-pacing', analysis_id=1, candidate_id=42, cv_version_id=1,
            source_hash='a' * 64, text=source, prompt_version=CV_EFFICIENT_PROMPT_VERSION,
            provider='auto', model='synthetic', packet_chars=20000, block_chars=20000, budget=950,
        )
        invalid = {'sk': [{'n': 'Python', 'e': ['Declara Python.'], 'q': ['Inventada.']}]}
        valid = {'sk': [{'n': 'Python', 'e': ['Declara Python.'], 'q': [source]}]}
        snapshots, repairs = [], set()
        with patch('app.services.cv_packets.build_cv_skill_context', new_callable=AsyncMock, return_value=''), \
             patch('app.services.cv_pipeline._call_async', new_callable=AsyncMock,
                   side_effect=[invalid, RateLimitExceeded(4, reason='cooldown'), valid]) as calls, \
             patch('app.services.cv_pipeline.asyncio.sleep', new_callable=AsyncMock) as sleep:
            with self.assertRaises(RateLimitExceeded):
                await process_packetized_cv_text(
                    source, 42, identity=identity, checkpoint=None, on_checkpoint=snapshots.append,
                    on_heartbeat=None, call_provider=_call_cv_provider,
                    repair_packets=repairs, on_validation_retry=repairs.add,
                )
            self.assertEqual(len(repairs), 1)
            profile, _ = await process_packetized_cv_text(
                source, 42, identity=identity, checkpoint=snapshots[-1], on_checkpoint=snapshots.append,
                on_heartbeat=None, call_provider=_call_cv_provider,
                repair_packets=repairs, on_validation_retry=repairs.add,
            )
        self.assertEqual(calls.await_count, 3)
        self.assertEqual(calls.await_args_list[1].args[0], calls.await_args_list[2].args[0])
        self.assertTrue(calls.await_args_list[2].args[2].endswith('validation-1'))
        self.assertEqual(profile['skills'][0]['source_quotes'], [source])
        sleep.assert_not_awaited()

    async def test_short_fallback_cooldown_preserves_repair_prompt_and_completes(self):
        source = 'Uso Python.'
        identity = make_packet_identity(
            job_id='validation-pacing', analysis_id=1, candidate_id=42, cv_version_id=1,
            source_hash='a' * 64, text=source, prompt_version=CV_EFFICIENT_PROMPT_VERSION,
            provider='auto', model='synthetic', packet_chars=20000, block_chars=20000, budget=950,
        )
        invalid = {'sk': [{'n': 'Python', 'e': ['Declara Python.'], 'q': ['No existe.']}]}
        valid = {'sk': [{'n': 'Python', 'e': ['Declara Python.'], 'q': [source]}]}
        snapshots = []
        with patch('app.services.cv_packets.build_cv_skill_context', new_callable=AsyncMock, return_value=''), \
             patch('app.services.cv_pipeline._call_async', new_callable=AsyncMock,
                   side_effect=[invalid, RateLimitExceeded(4, reason='cooldown'), valid]) as calls, \
             patch('app.services.cv_pipeline.asyncio.sleep', new_callable=AsyncMock) as sleep:
            profile, _ = await process_packetized_cv_text(
                source, 42, identity=identity, checkpoint=None, on_checkpoint=snapshots.append,
                on_heartbeat=None, call_provider=_call_cv_provider,
            )
        self.assertEqual(calls.await_count, 3)
        self.assertIn('La respuesta anterior no pasó la validación', calls.await_args_list[1].args[0])
        self.assertEqual(calls.await_args_list[1].args[0], calls.await_args_list[2].args[0])
        self.assertEqual(profile['skills'][0]['source_quotes'], [source])
        self.assertEqual(snapshots[-1]['consolidation']['status'], 'completed')
        sleep.assert_awaited_once_with(4.0)
