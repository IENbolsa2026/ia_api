from unittest.mock import patch
from uuid import uuid4

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core.config import settings
from app.services.match_job_store import MatchJobStore, MatchCancelled
from app.tasks.match_tasks import evaluate_match_task


def test_cancel_invalidates_owned_updates_and_survives_redelivery():
    store = MatchJobStore(fakeredis.FakeRedis(decode_responses=True))
    request_id = str(uuid4())
    request = {'request_id': request_id}
    store.create_or_get(request)
    assert store.acquire_lock(request_id)
    store.update_owned(request_id, status='processing')
    assert store.cancel(request_id)['status'] == 'cancelled'
    with pytest.raises(MatchCancelled):
        store.update_owned(request_id, status='completed', result={'match_percentage': 100})
    assert store.get(request_id)['status'] == 'cancelled'
    assert store.get(request_id)['result'] is None
    assert store.create_or_get(request)[0]['status'] == 'cancelled'
    with patch('app.tasks.match_tasks.MatchJobStore', return_value=store), patch('app.tasks.match_tasks.GroqClient') as provider:
        assert evaluate_match_task.run(request_id)['status'] == 'cancelled'
        provider.assert_not_called()


def test_cancel_before_enqueue_creates_a_tombstone_and_new_id_can_run():
    store = MatchJobStore(fakeredis.FakeRedis(decode_responses=True))
    request_id = str(uuid4())
    assert store.cancel(request_id)['status'] == 'cancelled'
    assert store.cancel(request_id)['status'] == 'cancelled'
    assert store.create_or_get({'request_id': request_id})[0]['status'] == 'cancelled'
    new_id = str(uuid4())
    assert store.create_or_get({'request_id': new_id})[0]['status'] == 'accepted'


def test_completion_winning_the_race_is_preserved():
    store = MatchJobStore(fakeredis.FakeRedis(decode_responses=True))
    request_id = str(uuid4())
    store.create_or_get({'request_id': request_id})
    store.acquire_lock(request_id)
    store.update_owned(request_id, status='completed', result={'match_percentage': 0})
    assert store.cancel(request_id)['status'] == 'completed'
    assert store.get(request_id)['result']['match_percentage'] == 0


def test_cancel_endpoint_requires_service_authentication(monkeypatch):
    monkeypatch.setattr(settings, 'SERVICE_API_TOKEN', 'test-service')
    store = MatchJobStore(fakeredis.FakeRedis(decode_responses=True))
    with patch('app.api.v1.endpoints.matches.MatchJobStore', return_value=store), TestClient(app) as client:
        url = f'/api/v1/matches/{uuid4()}/cancel'
        assert client.post(url).status_code == 401
        assert client.post(url, headers={'Authorization': 'Bearer test-service'}).json()['status'] == 'cancelled'


def test_cancel_during_provider_call_discards_the_late_result():
    store = MatchJobStore(fakeredis.FakeRedis(decode_responses=True))
    request_id = str(uuid4())
    store.create_or_get({'request_id': request_id, 'candidate_profile': {}, 'job_offer': {}})
    def answer(*args):
        store.cancel(request_id)
        return {}
    with patch('app.tasks.match_tasks.MatchJobStore', return_value=store), patch('app.tasks.match_tasks.GroqClient') as provider, patch('app.tasks.match_tasks.build_result') as result:
        provider.return_value.evaluate.side_effect = answer
        result.return_value.model_dump.return_value = {'match_percentage': 100}
        assert evaluate_match_task.run(request_id)['status'] == 'cancelled'
    assert store.get(request_id)['status'] == 'cancelled'
    assert store.get(request_id)['result'] is None
