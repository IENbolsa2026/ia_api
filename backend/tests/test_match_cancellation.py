"""La cancelación del outbox Django también detiene el trabajo de la API."""

import json
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.services.match_job_store import MatchCancelled, MatchJobStore
from app.tasks.match_tasks import evaluate_match_task


def test_cancel_endpoint_requires_service_authentication():
    with patch.object(settings, 'SERVICE_API_TOKEN', 'test-token'):
        response = TestClient(app).post('/api/v1/matches/job-1/cancel')
    assert response.status_code == 401


def test_cancel_endpoint_confirms_cancelled_job():
    with patch.object(settings, 'SERVICE_API_TOKEN', 'test-token'), \
         patch('app.api.v1.endpoints.matches.MatchJobStore') as store:
        store.return_value.cancel.return_value = {'status': 'cancelled'}
        response = TestClient(app).post('/api/v1/matches/job-1/cancel', headers={'Authorization': 'Bearer test-token'})
    assert response.json() == {'request_id': 'job-1', 'status': 'cancelled'}
    store.return_value.cancel.assert_called_once_with('job-1')


def test_cancel_before_job_arrives_is_idempotent():
    client = MagicMock()
    transaction = client.pipeline.return_value.__enter__.return_value
    transaction.get.return_value = None
    store = MatchJobStore(client)
    cancelled = store.cancel('job-1')
    assert cancelled['status'] == 'cancelled'
    client.get.side_effect = [None, json.dumps(cancelled)]
    client.set.return_value = False
    job, created = store.create_or_get({'request_id': 'job-1'})
    assert job['status'] == 'cancelled'
    assert created is False


def test_cancel_transaction_preserves_arrays_and_invalidates_worker_lease():
    client = MagicMock()
    transaction = client.pipeline.return_value.__enter__.return_value
    transaction.get.return_value = json.dumps({'status': 'processing', 'items': []})
    store = MatchJobStore(client)
    result = store.cancel('job-1')
    assert result['status'] == 'cancelled'
    assert json.loads(transaction.set.call_args.args[1])['items'] == []
    transaction.delete.assert_called_once_with('jev-match:lock:job-1')
    transaction.execute.assert_called_once()


def test_cancel_does_not_discard_a_completed_result():
    client = MagicMock()
    transaction = client.pipeline.return_value.__enter__.return_value
    transaction.get.return_value = json.dumps({'status': 'completed', 'result': {'score': 80}})
    assert MatchJobStore(client).cancel('job-1')['result'] == {'score': 80}
    transaction.set.assert_not_called()


def test_cancelled_worker_does_not_call_provider():
    store = MagicMock()
    store.get.return_value = {'status': 'cancelled'}
    with patch('app.tasks.match_tasks.MatchJobStore', return_value=store), \
         patch('app.tasks.match_tasks.GroqClient') as provider:
        result = evaluate_match_task.run('job-1')
    assert result['status'] == 'cancelled'
    provider.assert_not_called()


def test_late_provider_result_cannot_overwrite_cancellation():
    client = MagicMock()
    # El worker leyó processing; la cancelación invalida su lease antes del CAS.
    client.get.side_effect = [json.dumps({'status': 'processing'}), json.dumps({'status': 'cancelled'})]
    client.eval.return_value = 0
    store = MatchJobStore(client)
    store._lock_tokens['job-1'] = 'stale-worker-token'
    try:
        store.update_owned('job-1', status='completed', result={'score': 80})
    except MatchCancelled:
        pass
    else:
        raise AssertionError('El resultado tardío reemplazó la cancelación.')


def test_api_starts_in_production_without_antivirus():
    from app.core.config import ModeEnum

    with patch.object(settings, 'MODE', ModeEnum.production), \
         patch.object(settings, 'SERVICE_API_TOKEN', 'test-token'), \
         patch.object(settings, 'CV_RESULT_CALLBACK_HMAC_SECRET', 'test-hmac'):
        with TestClient(app) as client:
            assert client.get('/').status_code == 200
