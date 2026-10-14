import unittest
from unittest.mock import AsyncMock, patch

from app.tasks.cv_analysis_tasks import analyze_cv_task
from app.services.channel_events import publish_cv_analysis_complete


class CVNotificationTaskTests(unittest.TestCase):
    @patch('app.services.channel_events.RedisChannelLayer')
    def test_publisher_sends_to_user_group(self, layer_type):
        layer = layer_type.return_value
        layer.group_send = AsyncMock()
        layer.close_pools = AsyncMock()

        publish_cv_analysis_complete(9, 17, 'completado')

        layer.group_send.assert_awaited_once_with('user_9', {
            'type': 'cv.analysis.complete',
            'payload': {'analysis_id': 17, 'status': 'completado'},
        })
        layer.close_pools.assert_awaited_once()

    @patch('app.tasks.cv_analysis_tasks.publish_cv_analysis_complete')
    @patch('app.tasks.cv_analysis_tasks.notify_cv_result')
    def test_completed_task_publishes_after_callback(self, notify, publish):
        result = analyze_cv_task.apply(kwargs={
            'analysis_id': 17,
            'candidate_id': 4,
            'user_id': 9,
            'cv_pdf_base64': '',
            'analysis_result': {'habilidades_analizadas': []},
        })

        self.assertTrue(result.successful())
        self.assertEqual(notify.call_args.args[0]['status'], 'completed')
        publish.assert_called_once_with(9, 17, 'completado')

    @patch('app.tasks.cv_analysis_tasks.publish_cv_analysis_complete')
    @patch('app.tasks.cv_analysis_tasks.notify_cv_result')
    def test_legacy_client_without_user_id_keeps_callback(self, notify, publish):
        result = analyze_cv_task.apply(kwargs={
            'analysis_id': 17,
            'candidate_id': 4,
            'cv_pdf_base64': '',
            'analysis_result': {},
        })

        self.assertTrue(result.successful())
        notify.assert_called_once()
        publish.assert_not_called()


if __name__ == '__main__':
    unittest.main()
