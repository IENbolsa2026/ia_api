from unittest.mock import MagicMock, patch
import httpx
import redis
from fastapi.testclient import TestClient
from app.core.config import settings
from app.main import app
from app.services import usage_metrics


def test_numeric_usage_is_recorded_without_payload_or_credentials():
    client = MagicMock()
    pipeline = client.pipeline.return_value
    with patch.object(usage_metrics,'client',return_value=client), patch.object(usage_metrics.time,'time',return_value=1800000000):
        usage_metrics.record_response('groq','openai/gpt-oss-120b',200,{
            'prompt_tokens':123,'completion_tokens':45,'total_tokens':168,
            'private CV': 'secret','cached_tokens':True,'thought_tokens':-1,
        })
    increments = {call.args[1]:call.args[2] for call in pipeline.hincrby.call_args_list}
    assert increments['total_tokens']==168 and increments['requests']==1
    assert increments['usage_responses']==1 and increments['errors']==0
    assert 'cached_tokens' not in increments and 'thought_tokens' not in increments
    assert 'secret' not in str(pipeline.mock_calls) and 'private CV' not in str(pipeline.mock_calls)


def test_metrics_outage_never_breaks_inference():
    with patch.object(usage_metrics,'client',side_effect=redis.ConnectionError):
        usage_metrics.record_response('gemini','gemini-3.5-flash-lite',503)


def test_snapshot_adds_hours_models_and_distinguishes_missing_usage():
    client=MagicMock();now=1800000000;hour=now//3600*3600
    client.zrangebyscore.return_value=['one','two'];client.get.return_value=str(now)
    client.pipeline.return_value.execute.return_value=[
        {'provider':'gemini','model':'gemini-test','hour':str(hour),'requests':'2','errors':'1','usage_responses':'1','total_tokens':'150','prompt_tokens':'100','completion_tokens':'50'},
        {'provider':'groq','model':'test-model','hour':str(hour),'requests':'1','errors':'0','usage_responses':'0'},
    ]
    with patch.object(usage_metrics,'client',return_value=client),patch.object(usage_metrics.time,'time',return_value=now):
        result=usage_metrics.snapshot()
    assert result['totals']['requests']==3 and result['totals']['usage_responses']==1
    assert result['totals']['total_tokens']==150 and len(result['hours'])==24
    assert sum(row['total_tokens'] for row in result['hours'])==150
    assert len(result['models'])==2


def test_monitor_endpoint_requires_service_token():
    with patch.object(settings,'SERVICE_API_TOKEN','synthetic-service-token'),patch('app.api.v1.endpoints.llm_usage.snapshot',return_value={'totals':{}}) as read:
        client=TestClient(app)
        assert client.get('/api/v1/llm-usage').status_code==401
        read.assert_not_called()
        result=client.get('/api/v1/llm-usage',headers={'Authorization':'Bearer synthetic-service-token'})
        assert result.status_code==200 and result.headers['cache-control']=='no-store'


def test_successful_but_truncated_response_still_counts_usage(monkeypatch):
    from app.services import llm_router as router
    from app.services.rate_limiter import ProviderResponseTruncated
    import pytest
    limiter=MagicMock();route=router.Route('groq','synthetic-model','synthetic-key')
    body={'usage':{'prompt_tokens':10,'completion_tokens':20,'total_tokens':30},
          'choices':[{'finish_reason':'length','message':{'content':'incomplete'}}]}
    with patch.object(router,'_limiter',return_value=limiter),patch.object(router.httpx,'post',return_value=httpx.Response(200,json=body)),patch.object(router,'record_usage_metrics') as record:
        with pytest.raises(ProviderResponseTruncated):router._http_json(route,'system','payload','marker',100,10,limiter)
        record.assert_called_once_with('groq','synthetic-model',200,body['usage'])


def test_quota_snapshot_is_readonly_and_never_exposes_key_or_fingerprint():
    import json
    from app.services import llm_monitor
    from app.services.llm_router import Route
    connection=MagicMock();now=1800000000000
    connection.get.side_effect=lambda key: now+5000 if key=='model-cooldown' else 0
    connection.zrangebyscore.return_value=['one','two'];connection.hmget.return_value=['10','20'];connection.zcount.return_value=6
    limiter=MagicMock();limiter.cooldown_key='model-cooldown'
    credential=MagicMock();credential.cooldown_key='credential-cooldown'
    route=Route('gemini','gemini-test','private-synthetic-key')
    empty={'totals':{},'models':[],'hours':[]}
    with patch.object(llm_monitor.usage_metrics,'snapshot',return_value=empty),patch.object(llm_monitor.usage_metrics,'client',return_value=connection),patch.object(llm_monitor,'routes',return_value=[route]),patch.object(llm_monitor,'gemini_rate_limiter',return_value=limiter),patch.object(llm_monitor,'_limiter',return_value=credential),patch.object(llm_monitor.time,'time',return_value=now/1000):
        result=llm_monitor.snapshot()
    assert len(result['routes'])==1
    assert result['routes'][0]['input_tokens_minute']==30
    assert result['routes'][0]['requests_day']==6
    assert result['routes'][0]['cooldown_seconds']==5
    assert 'private-synthetic-key' not in json.dumps(result) and route.scope not in json.dumps(result)
    connection.set.assert_not_called();connection.eval.assert_not_called()
