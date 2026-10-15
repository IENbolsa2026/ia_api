import unittest
from unittest.mock import AsyncMock, patch

from app.services.cv_pipeline import normalize_candidate_profile, process_offer_snapshot, process_cv_text


class ProfileContractTests(unittest.IsolatedAsyncioTestCase):
    @patch('app.services.cv_pipeline._call_async', new_callable=AsyncMock)
    async def test_offer_prompt_contains_schema_and_preserves_id(self, call):
        call.return_value = {'offer_id': '12', 'title': 'Developer'}
        result = await process_offer_snapshot({'title': 'Developer'}, 12)
        self.assertEqual(result['offer_id'], '12')
        self.assertIn('"$defs"', call.call_args.args[0])
        self.assertIn('free text in Spanish', call.call_args.args[0])
        self.assertIn('hard skills', call.call_args.args[0])
        self.assertIn('soft skills', call.call_args.args[0])

    @patch('app.services.cv_pipeline._call_async', new_callable=AsyncMock)
    async def test_candidate_id_cannot_be_changed_by_provider(self, call):
        call.side_effect = [{'skills': []}, {'profile_id': '999'}]
        with self.assertRaisesRegex(ValueError, 'another candidate ID'):
            await process_cv_text('Synthetic CV', 12)
        self.assertIn('free text in Spanish', call.call_args.args[0])
        self.assertIn('hard skills', call.call_args.args[0])
        self.assertIn('soft skills', call.call_args.args[0])

    @patch('app.services.cv_pipeline._call_async', new_callable=AsyncMock)
    async def test_profile_edits_keep_spanish_free_text(self, call):
        call.return_value = {'profile_id': '12'}
        await normalize_candidate_profile({'profile_id': '12'}, 12)
        self.assertIn('free text in Spanish', call.call_args.args[0])
