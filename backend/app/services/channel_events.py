"""Notifica a Channels después de que Django confirmó guardar el resultado."""

import asyncio
import logging

from channels_redis.core import RedisChannelLayer

from app.core.config import settings

logger = logging.getLogger(__name__)


def publish_cv_analysis_complete(user_id: int, analysis_id: int, status: str) -> None:
    """El WebSocket es best-effort: Redis caído no debe reanalizar el CV."""

    async def send_event() -> None:
        layer = RedisChannelLayer(hosts=[settings.CHANNEL_REDIS_URL])
        try:
            await layer.group_send(
                f'user_{user_id}',
                {
                    'type': 'cv.analysis.complete',
                    'payload': {'analysis_id': analysis_id, 'status': status},
                },
            )
        finally:
            await layer.close_pools()

    try:
        asyncio.run(send_event())
    except Exception:
        logger.exception('Could not publish CV completion event for analysis %s', analysis_id)
