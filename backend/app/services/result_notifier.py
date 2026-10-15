"""Entrega autenticada de eventos de extracción a Bolsa."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import httpx

from app.core.config import settings


def _signature(timestamp: str, body: bytes) -> str | None:
    secret = settings.CV_RESULT_CALLBACK_HMAC_SECRET or settings.CV_RESULT_CALLBACK_SECRET
    if not secret:
        return None
    return hmac.new(secret.encode("utf-8"), timestamp.encode("ascii") + b"." + body, hashlib.sha256).hexdigest()


def notify_extraction_result(payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    timestamp = str(int(time.time()))
    headers = {"Content-Type": "application/json", "X-IA-Event-Timestamp": timestamp}
    signature = _signature(timestamp, body)
    if signature:
        headers["X-IA-Callback-Signature"] = signature
    if settings.CV_RESULT_CALLBACK_SECRET:
        headers["X-IA-Callback-Token"] = settings.CV_RESULT_CALLBACK_SECRET
    with httpx.Client(timeout=httpx.Timeout(30.0)) as client:
        response = client.post(str(settings.CV_RESULT_CALLBACK_URL), content=body, headers=headers)
        response.raise_for_status()
