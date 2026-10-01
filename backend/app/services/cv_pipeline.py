"""Extractores versionados de CV y ofertas con salida validada."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import redis
from groq import Groq
from pydantic import ValidationError

from ien_contracts.models import CandidateProfile, JobOffer

from app.core.config import settings
from app.services.prompt_loader import load_prompt
from app.services.rate_limiter import RedisRateLimiter


def split_text_into_blocks(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    blocks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            newline = text.rfind("\n", start, end)
            if newline > start:
                end = newline + 1
        blocks.append(text[start:end].strip())
        start = end
    return [block for block in blocks if block]


def _call_groq(system_prompt: str, user_payload: str, request_marker: str) -> dict[str, Any]:
    if not settings.GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not configured.")
    limiter = RedisRateLimiter(
        redis.from_url(settings.JOB_STORE_REDIS_URL, decode_responses=True),
        settings.GROQ_REQUESTS_PER_MINUTE,
    )
    limiter.reserve(request_marker)
    client = Groq(api_key=settings.GROQ_API_KEY)
    response = client.chat.completions.create(
        model=settings.CV_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_payload},
        ],
        max_tokens=settings.GROQ_MAX_TOKENS,
        temperature=0,
        response_format={"type": "json_object"},
    )
    content = response.choices[0].message.content or "{}"
    return json.loads(content)


def _call_openai(system_prompt: str, user_payload: str, request_marker: str) -> dict[str, Any]:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("The OpenAI SDK is not installed. Run: python -m pip install 'openai>=1.0.0,<2.0.0'.") from exc
    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured.")
    limiter = RedisRateLimiter(
        redis.from_url(settings.JOB_STORE_REDIS_URL, decode_responses=True),
        settings.OPENAI_REQUESTS_PER_MINUTE,
    )
    limiter.reserve(request_marker)
    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    response = client.responses.create(
        model=settings.OPENAI_MODEL,
        instructions=system_prompt,
        input=user_payload,
        max_output_tokens=settings.OPENAI_MAX_OUTPUT_TOKENS,
        text={"format": {"type": "json_object"}},
        store=False,
    )
    return json.loads(response.output_text or "{}")


def _call_provider(system_prompt: str, user_payload: str, request_marker: str) -> dict[str, Any]:
    provider = settings.LLM_PROVIDER.strip().lower()
    if provider == "openai":
        return _call_openai(system_prompt, user_payload, request_marker)
    if provider == "groq":
        return _call_groq(system_prompt, user_payload, request_marker)
    raise RuntimeError("LLM_PROVIDER must be 'openai' or 'groq'.")


async def _call_async(system_prompt: str, payload: str, marker: str) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _call_provider, system_prompt, payload, marker)


async def process_cv_text(text: str, candidate_id: int) -> dict[str, Any]:
    """Extrae bloques y consolida un perfil inglés validado."""

    system = load_prompt("cv_extraction.v1.system.md")
    fragments: list[dict[str, Any]] = []
    blocks = split_text_into_blocks(text, settings.CV_BLOQUE_CARACTERES)
    for index, block in enumerate(blocks, 1):
        fragments.append(
            await _call_async(
                system + "\nExtract factual fragments; do not fabricate a final profile yet.",
                f"Resume fragment {index}/{len(blocks)}:\n{block}",
                f"cv-{candidate_id}-fragment-{index}",
            )
        )
    final = await _call_async(
        system + "\nConsolidate the fragments into one complete candidate-profile.v1 object. Set profile_id to the supplied value.",
        json.dumps({"profile_id": str(candidate_id), "fragments": fragments}, ensure_ascii=False),
        f"cv-{candidate_id}-consolidation",
    )
    try:
        return CandidateProfile.model_validate(final).model_dump(mode="json")
    except ValidationError as exc:
        raise ValueError("The provider returned an invalid candidate profile.") from exc


async def process_offer_snapshot(snapshot: dict[str, Any], offer_id: int) -> dict[str, Any]:
    """Convierte el snapshot de Bolsa en el contrato job-offer.v1."""

    final = await _call_async(
        load_prompt("offer_extraction.v1.system.md") + "\nSet offer_id to the supplied value.",
        json.dumps({"offer_id": str(offer_id), "offer": snapshot}, ensure_ascii=False),
        f"offer-{offer_id}",
    )
    try:
        return JobOffer.model_validate(final).model_dump(mode="json")
    except ValidationError as exc:
        raise ValueError("The provider returned an invalid job offer profile.") from exc


async def normalize_candidate_profile(snapshot: dict[str, Any], candidate_id: int) -> dict[str, Any]:
    """Traduce y valida una edición del usuario sin alterar hechos declarados."""

    original = CandidateProfile.model_validate(snapshot).model_dump(mode="json")
    final = await _call_async(
        load_prompt("profile_normalization.v1.system.md") + "\nSet profile_id to the supplied value.",
        json.dumps({"profile_id": str(candidate_id), "profile": original}, ensure_ascii=False),
        f"profile-normalization-{candidate_id}",
    )
    try:
        return CandidateProfile.model_validate(final).model_dump(mode="json")
    except ValidationError as exc:
        raise ValueError("The provider returned an invalid normalized candidate profile.") from exc
