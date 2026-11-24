"""Fair CV turns and bounded retry windows, independent of Celery counters."""
from datetime import datetime, timedelta, timezone
import math

from app.core.config import settings


class CVTurnComplete(Exception):
    """A saved packet yields the worker to the next user's CV."""


class CVQueueExpired(ValueError):
    def __str__(self):
        return ('El análisis no pudo avanzar dentro del tiempo máximo de espera. '
                'Los proveedores no tienen capacidad suficiente. Podés volver a intentarlo más tarde.')


def utc_now():
    return datetime.now(timezone.utc)


def _parse(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def start_tracking(job):
    now = utc_now().isoformat()
    first = job.get('first_started_at') if _parse(job.get('first_started_at')) else now
    progress = job.get('last_progress_at') if _parse(job.get('last_progress_at')) else first
    return {'first_started_at': first, 'last_progress_at': progress}


def remaining_seconds(job):
    first = _parse(job.get('first_started_at'))
    if first is None:
        return None
    progress = _parse(job.get('last_progress_at')) or first
    now = utc_now()
    return min(settings.CV_MAX_PROCESSING_SECONDS - (now - first).total_seconds(),
               settings.CV_MAX_NO_PROGRESS_SECONDS - (now - progress).total_seconds())


def ensure_window(job):
    remaining = remaining_seconds(job)
    if remaining is not None and remaining <= 0:
        raise CVQueueExpired()


def retry_delay(job, requested):
    delay = max(1, math.ceil(requested))
    remaining = remaining_seconds(job)
    return delay if remaining is None else min(delay, max(1, math.ceil(remaining)))


def wait_state(job, reason, countdown):
    return {'wait_reason': reason,
            'next_attempt_at': (utc_now() + timedelta(seconds=countdown)).isoformat()}


def public_progress(job):
    packets = (job.get('extraction_packets') or {}).get('packets', {})
    leaves = [p for p in packets.values() if p.get('status') != 'split']
    next_attempt = _parse(job.get('next_attempt_at'))
    status = job.get('status')
    message = {
        'accepted': 'Tu CV está en cola. Podés seguir usando el sitio.',
        'queued': 'Tu CV está en cola y conserva el progreso realizado.',
        'processing': 'Estamos analizando una parte de tu CV.',
        'rate_limited': 'Esperando disponibilidad del proveedor. Tu CV conserva su progreso.',
    }.get(status)
    return {
        'message': message,
        'completed_packets': sum(p.get('status') == 'completed' for p in leaves),
        'total_packets': len(leaves),
        'wait_reason': job.get('wait_reason') if message else None,
        'next_attempt_at': job.get('next_attempt_at') if message else None,
        'retry_after_seconds': max(0, math.ceil((next_attempt - utc_now()).total_seconds())) if next_attempt and message else None,
    }
