"""Autenticación de llamadas internas entre Bolsa y el extractor."""

import hmac

from fastapi import Header, HTTPException, status

from .config import settings


def _token_from_header(value: str | None) -> str | None:
    if not value or not value.startswith("Bearer "):
        return None
    return value[7:].strip()


async def require_service_token(authorization: str | None = Header(default=None)) -> None:
    if not settings.SERVICE_API_TOKEN:
        raise HTTPException(status_code=503, detail="Service authentication is not configured.")
    token = _token_from_header(authorization)
    if not token or not hmac.compare_digest(token, settings.SERVICE_API_TOKEN):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized.")
