"""Authenticated read-only operational counters; no inference calls."""
import redis
from fastapi import APIRouter, Depends, HTTPException, Response
from app.core.security import require_service_token
from app.services.llm_monitor import snapshot

router = APIRouter()


@router.get('/llm-usage', dependencies=[Depends(require_service_token)])
def llm_usage(response: Response):
    response.headers['Cache-Control'] = 'no-store'
    try:
        return snapshot()
    except redis.RedisError:
        raise HTTPException(status_code=503, detail='El monitor de consumo no está disponible.') from None
