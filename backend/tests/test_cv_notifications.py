import unittest
from unittest.mock import AsyncMock, patch

from app.tasks.cv_analysis_tasks import deliver_extraction_result_task
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

    def deliver(self, user_id=9, result=None):
        job = {'job_id': 'job-1', 'idempotency_key': 'cv-1', 'status': 'completed',
               'result': result if result is not None else {}, 'error': None,
               'payload': {'kind': 'candidate_cv', 'analysis_id': 17, 'candidate_id': 4,
                           'user_id': user_id, 'source_hash': 'a' * 64}}
        return job

    @patch('app.tasks.cv_analysis_tasks.ExtractionJobStore')
    @patch('app.tasks.cv_analysis_tasks.publish_cv_analysis_complete')
    @patch('app.tasks.cv_analysis_tasks.notify_extraction_result')
    def test_completed_task_publishes_after_callback(self, notify, publish, store):
        store.return_value.get.return_value = self.deliver()
        events = []
        notify.side_effect = lambda payload: events.append(('callback', payload['status']))
        publish.side_effect = lambda *args: events.append(('notification', args))
        deliver_extraction_result_task.run('job-1')
        self.assertEqual(events, [('callback', 'completed'), ('notification', (9, 17, 'completado'))])

    @patch('app.tasks.cv_analysis_tasks.ExtractionJobStore')
    @patch('app.tasks.cv_analysis_tasks.publish_cv_analysis_complete')
    @patch('app.tasks.cv_analysis_tasks.notify_extraction_result')
    def test_client_without_user_id_keeps_callback(self, notify, publish, store):
        store.return_value.get.return_value = self.deliver(user_id=None)
        deliver_extraction_result_task.run('job-1')
        notify.assert_called_once()
        publish.assert_not_called()

    @patch('app.tasks.cv_analysis_tasks.ExtractionJobStore')
    @patch('app.tasks.cv_analysis_tasks.publish_cv_analysis_complete')
    @patch('app.tasks.cv_analysis_tasks.notify_extraction_result')
    def test_failed_delivery_retry_preserves_failed_status(self, notify, publish, store):
        job = self.deliver()
        job.update(status='delivery_failed', result=None)
        store.return_value.get.return_value = job
        deliver_extraction_result_task.run('job-1')
        self.assertEqual(notify.call_args.args[0]['status'], 'failed')
        publish.assert_called_once_with(9, 17, 'error')


if __name__ == '__main__':
    unittest.main()
