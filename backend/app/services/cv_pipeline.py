"""Extractores versionados de CV y ofertas con salida validada."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import logging
import re
from functools import partial
from typing import Any
from uuid import uuid4

import redis
from groq import Groq
from pydantic import BaseModel, ConfigDict, ValidationError

from ien_contracts.models import CandidateProfile, JobOffer, Requirement

from app.core.config import settings
from app.services.cv_compact_contract import validate_expanded_profile, validate_fragment
from app.services.cv_packets import PacketsIdentityError, make_packet_identity, process_packetized_cv_text
from app.services.rate_limiter import (
    ProviderOutputLimitExceeded,
    ProviderResponseTruncated,
    RateLimitExceeded,
    estimate_request_tokens,
    groq_rate_limiter,
    retry_after_seconds,
)

CV_EXTRACTION_PROMPT_VERSION = "cv-extraction.v2-packets-soft.v2"
OFFER_EXTRACTION_PROMPT_VERSION = "offer-extraction.v2"
PROFILE_NORMALIZATION_PROMPT_VERSION = "profile-normalization.v2"


class PipelineOutput(dict):
    """JSON-compatible result that keeps provider metadata outside the payload."""

    def __init__(self, value: dict[str, Any], provider_model: str | None = None):
        super().__init__(value)
        self.provider_model = provider_model


logger = logging.getLogger(__name__)


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


def _provider_error_message(exc: Exception) -> str:
    """Lee solo el mensaje necesario para clasificar un error 429 de Groq."""
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        response = getattr(exc, "response", None)
        try:
            body = response.json() if response is not None else None
        except Exception:
            body = None
    if not isinstance(body, dict):
        return ""
    error = body.get("error", body)
    message = error.get("message", "") if isinstance(error, dict) else ""
    return message if isinstance(message, str) else ""


def _call_groq(system_prompt: str, user_payload: str, request_marker: str,
               max_output_tokens: int | None = None) -> dict[str, Any]:
    if not settings.GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not configured.")
    limiter = groq_rate_limiter(settings.CV_MODEL)
    estimated_tokens = estimate_request_tokens(
        system_prompt, user_payload, max_output_tokens or settings.GROQ_MAX_TOKENS,
    )
    limiter.reserve(request_marker, estimated_tokens)
    client = Groq(api_key=settings.GROQ_API_KEY, max_retries=0, timeout=60)
    try:
        request_params = dict(
            model=settings.CV_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_payload},
            ],
            max_tokens=max_output_tokens or settings.GROQ_MAX_TOKENS,
            temperature=0,
            response_format={"type": "json_object"},
        )
        if settings.CV_MODEL == "qwen/qwen3.8-27b":
            request_params["reasoning_effort"] = "none"
        raw_response = client.chat.completions.with_raw_response.create(**request_params)
        limiter.record_response(raw_response.headers, status_code=200, estimated_tokens=estimated_tokens)
        response = raw_response.parse()
    except Exception as exc:
        if getattr(exc, "status_code", None) == 429:
            response_obj = getattr(exc, "response", None)
            headers = getattr(response_obj, "headers", {}) if response_obj else {}
            wait = limiter.record_response(headers, status_code=429, estimated_tokens=estimated_tokens)
            provider_message = _provider_error_message(exc)
            lowered = provider_message.casefold()
            if (
                "request too large" in lowered
                and "output tokens" in lowered
                and (
                    ("expected output tokens" in lowered and "exceed" in lowered)
                    or "otpm" in lowered
                )
            ):
                limit_match = re.search(r"\blimit\s+(\d+)\b", provider_message, re.IGNORECASE)
                limit_num = int(limit_match.group(1)) if limit_match else None
                raise ProviderOutputLimitExceeded(limit_num) from None
            raise RateLimitExceeded(wait) from None
        raise RuntimeError("No se pudo completar la solicitud al proveedor de análisis.") from None
    choice = response.choices[0]
    if choice.finish_reason == "length":
        raise ProviderResponseTruncated("La respuesta quedó incompleta por el límite de tokens.")
    content = choice.message.content or "{}"
    result = json.loads(content)
    if isinstance(result, dict):
        result["__provider_model"] = response.model or settings.CV_MODEL
    return result


def _call_openai(system_prompt: str, user_payload: str, request_marker: str,
                 max_output_tokens: int | None = None) -> dict[str, Any]:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("The OpenAI SDK is not installed. Run: python -m pip install 'openai>=1.0.0,<2.0.0'.") from exc
    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured.")
    limiter = RedisRateLimiter(
        redis.from_url(settings.JOB_STORE_REDIS_URL, decode_responses=True),
        settings.OPENAI_REQUESTS_PER_MINUTE,
        cooldown_scope="openai", provider="openai",
    )
    limiter.reserve(request_marker)
    client = OpenAI(api_key=settings.OPENAI_API_KEY, max_retries=0, timeout=60)
    try:
        response = client.responses.create(
            model=settings.OPENAI_MODEL,
            instructions=system_prompt,
            input=user_payload,
            max_output_tokens=max_output_tokens or settings.OPENAI_MAX_OUTPUT_TOKENS,
            text={"format": {"type": "json_object"}},
            store=False,
        )
    except Exception as exc:
        if getattr(exc, "status_code", None) == 429:
            response_obj = getattr(exc, "response", None)
            headers = getattr(response_obj, "headers", {}) if response_obj else {}
            raise RateLimitExceeded(retry_after_seconds(headers.get("Retry-After"))) from None
        raise RuntimeError("No se pudo completar la solicitud al proveedor de análisis.") from None
    if getattr(response, "status", None) == "incomplete" or getattr(getattr(response, "incomplete_details", None), "reason", None) == "max_output_tokens":
        raise ProviderResponseTruncated("La respuesta quedó incompleta por el límite de tokens.")
    result = json.loads(response.output_text or "{}")
    if isinstance(result, dict):
        result["__provider_model"] = getattr(response, "model", settings.OPENAI_MODEL)
    return result


def _call_provider(system_prompt: str, user_payload: str, request_marker: str,
                   max_output_tokens: int | None = None) -> dict[str, Any]:
    provider = settings.LLM_PROVIDER.strip().lower()
    if provider == "openai":
        return _call_openai(system_prompt, user_payload, request_marker, max_output_tokens)
    if provider == "groq":
        return _call_groq(system_prompt, user_payload, request_marker, max_output_tokens)
    raise RuntimeError("LLM_PROVIDER must be 'openai' or 'groq'.")


async def _call_async(system_prompt: str, payload: str, marker: str,
                      max_output_tokens: int | None = None) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    call = partial(_call_provider, system_prompt, payload, marker, max_output_tokens)
    return await loop.run_in_executor(None, call)


async def _call_cv_provider(
    system_prompt: str,
    payload: str,
    marker: str,
    *,
    on_heartbeat=None,
) -> dict[str, Any]:
    """Rate-limit waits for CV stay within this task and preserve prior blocks in memory."""
    consecutive_waits = 0

    async def heartbeat() -> None:
        if on_heartbeat is None:
            return
        if inspect.iscoroutinefunction(on_heartbeat):
            result = await on_heartbeat()
        else:
            result = await asyncio.to_thread(on_heartbeat)
        if inspect.isawaitable(result):
            await result

    while True:
        await heartbeat()
        try:
            response = await _call_async(
                system_prompt,
                payload,
                marker,
                max_output_tokens=settings.CV_EXTRACTION_MAX_TOKENS,
            )
        except RateLimitExceeded as exc:
            if not exc.is_local_wait:
                if consecutive_waits >= 5:
                    raise
                consecutive_waits += 1
            remaining = max(1.0, float(exc.retry_after_seconds))
            while remaining > 0:
                interval = min(30.0, remaining)
                await asyncio.sleep(interval)
                remaining -= interval
                await heartbeat()
            continue
        await heartbeat()
        return response


async def process_cv_text(
    text: str,
    candidate_id: int,
    *,
    identity: dict[str, Any] | None = None,
    checkpoint: dict[str, Any] | None = None,
    on_checkpoint=None,
    on_heartbeat=None,
) -> dict[str, Any]:
    """Extract candidate-profile.v2 in job-isolated, resumable text packets."""
    external = identity or {}
    source_hash = external.get("source_hash") or hashlib.sha256(text.encode("utf-8")).hexdigest()
    packet_identity = make_packet_identity(
        job_id=external.get("job_id") or str(uuid4()),
        analysis_id=external.get("analysis_id", 0),
        candidate_id=candidate_id,
        cv_version_id=external.get("cv_version_id"),
        source_hash=source_hash,
        text=text,
        prompt_version=CV_EXTRACTION_PROMPT_VERSION,
        provider=settings.LLM_PROVIDER,
        model=settings.OPENAI_MODEL if settings.LLM_PROVIDER.strip().lower() == "openai" else settings.CV_MODEL,
        packet_chars=settings.CV_PACKET_CHARACTERS,
        block_chars=settings.CV_BLOQUE_CARACTERES,
        budget=settings.CV_EXTRACTION_MAX_TOKENS,
    )
    if identity is not None:
        supplied_identity = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        expected_identity = json.dumps(packet_identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if supplied_identity != expected_identity:
            raise PacketsIdentityError()
    profile, provider_model = await process_packetized_cv_text(
        text,
        candidate_id,
        identity=packet_identity,
        checkpoint=checkpoint,
        on_checkpoint=on_checkpoint,
        on_heartbeat=on_heartbeat,
        call_provider=_call_cv_provider,
    )
    return PipelineOutput(profile, provider_model)


def _split_truncated_cv_block(block: str, minimum_chars: int = 40) -> tuple[str, str]:
    """Split one failed block near its midpoint without dropping source characters."""
    if len(block) < minimum_chars * 2:
        raise ProviderResponseTruncated("El bloque mínimo del CV excede el límite de salida y no puede dividirse más.")
    midpoint = len(block) // 2
    lower, upper = minimum_chars, len(block) - minimum_chars
    candidates = [index for index in range(lower, upper) if block[index] in "\r\n"]
    if not candidates:
        candidates = [index for index in range(lower, upper) if block[index].isspace()]
    split_at = min(candidates, key=lambda index: abs(index - midpoint)) if candidates else midpoint
    left, right = block[:split_at], block[split_at:]
    if len(left) < minimum_chars or len(right) < minimum_chars or left + right != block:
        raise ProviderResponseTruncated("El bloque mínimo del CV excede el límite de salida y no puede dividirse más.")
    return left, right


def _normalize_quote_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


async def process_offer_snapshot(snapshot: dict[str, Any], offer_id: int) -> dict[str, Any]:
    """Convierte el snapshot de Bolsa en el contrato job-offer.v2."""

    final = await _call_async(
        load_prompt("offer_extraction.v2.system.md") + "\noffer_id debe coincidir con el valor suministrado. Esquema JSON:\n" + json.dumps(JobOffer.model_json_schema()),
        json.dumps({"offer_id": str(offer_id), "offer": snapshot}, ensure_ascii=False),
        f"offer-{offer_id}",
    )
    provider_model = final.pop("__provider_model", None)
    try:
        offer = JobOffer.model_validate(final)
        if offer.offer_id != str(offer_id):
            raise ValueError("El proveedor devolvió otra identidad de oferta.")
        if offer.schema_version != "job-offer.v2":
            raise ValueError("El proveedor devolvió una versión de oferta incompatible.")
        offer = _restore_offer_classification(offer, snapshot)
        return PipelineOutput(offer.model_dump(mode="json"), provider_model)
    except ValidationError as exc:
        raise ValueError("El proveedor devolvió una oferta inválida.") from exc


async def normalize_candidate_profile(snapshot: dict[str, Any], candidate_id: int) -> dict[str, Any]:
    """Traduce y valida una edición del usuario sin alterar hechos declarados."""

    original = CandidateProfile.model_validate(snapshot).model_dump(mode="json")
    if original["schema_version"] == "candidate-profile.v2":
        if original["profile_id"] != str(candidate_id):
            raise ValueError("El perfil editado no corresponde al candidato indicado.")
        allowed = _translatable_profile_fields(original)
        if not allowed:
            return original
        translated = await _call_async(
            load_prompt("profile_normalization.v2.system.md"),
            json.dumps({"translatable_fields": allowed}, ensure_ascii=False),
            f"profile-normalization-{candidate_id}",
        )
        provider_model = translated.pop("__provider_model", None)

        class TranslationResponse(BaseModel):
            model_config = ConfigDict(extra="forbid")
            translations: dict[str, str]

        try:
            response = TranslationResponse.model_validate(translated)
            if set(response.translations) != set(allowed):
                raise ValueError("La respuesta omitió textos o intentó traducir campos protegidos del perfil.")
            result = copy.deepcopy(original)
            for pointer, value in response.translations.items():
                if len(value) > 2000:
                    raise ValueError("La traducción supera el límite permitido.")
                _apply_translatable_field(result, pointer, value)
            validated = CandidateProfile.model_validate(result)
            return PipelineOutput(validated.model_dump(mode="json"), provider_model)
        except ValidationError as exc:
            raise ValueError("El proveedor devolvió traducciones de perfil inválidas.") from exc
    final = await _call_async(
        load_prompt("profile_normalization.v1.system.md") + "\nSet profile_id to the supplied value. JSON Schema:\n" + json.dumps(CandidateProfile.model_json_schema()),
        json.dumps({"profile_id": str(candidate_id), "profile": original}, ensure_ascii=False),
        f"profile-normalization-{candidate_id}",
    )
    provider_model = final.pop("__provider_model", None)
    try:
        profile = CandidateProfile.model_validate(final)
        if profile.profile_id != str(candidate_id):
            raise ValueError('The provider returned another candidate ID.')
        return PipelineOutput(profile.model_dump(mode="json"), provider_model)
    except ValidationError as exc:
        raise ValueError("The provider returned an invalid normalized candidate profile.") from exc


def _restore_offer_classification(offer: JobOffer, snapshot: dict[str, Any]) -> JobOffer:
    """Mantiene la clasificación y condición del snapshot como fuente de verdad."""
    source_requirements = snapshot.get("requirements")
    if not isinstance(source_requirements, list):
        raise ValueError("El snapshot de la oferta debe incluir requisitos estructurados.")
    try:
        restored = [Requirement.model_validate(source).model_dump(mode="json") for source in source_requirements]
    except ValidationError as exc:
        raise ValueError("El snapshot no incluye requisitos estructurados y clasificados por la plataforma.") from exc
    if len(offer.requirements) != len(restored):
        raise ValueError("La extracción cambió la cantidad de requisitos de la oferta.")
    source_ids = [item["id"] for item in restored]
    if any(not item_id for item_id in source_ids) or len(source_ids) != len(set(source_ids)):
        raise ValueError("Los requisitos originales deben tener identificadores únicos.")
    for source in source_requirements:
        if "mandatory" not in source or source.get("category") is None:
            raise ValueError("La plataforma debe clasificar cada requisito y marcar si es obligatorio.")
        if not source["mandatory"] and source["category"] != "desirables":
            raise ValueError("Los requisitos no obligatorios deben estar clasificados como deseables.")
        if source["mandatory"] and source["category"] == "desirables":
            raise ValueError("Un requisito deseable no puede ser obligatorio.")
    translated_ids = {item.id for item in offer.requirements}
    if translated_ids != {item["id"] for item in restored}:
        raise ValueError("La extracción cambió los identificadores de requisitos de la oferta.")
    # El nombre original puede codificar años, licencias o alternativas y forma parte
    # de la condición. Todo el requisito queda intacto; solo el resumen de oferta se traduce.
    data = offer.model_dump(mode="json")
    data["requirements"] = restored
    return JobOffer.model_validate(data)


def _translatable_profile_fields(profile: dict[str, Any]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for key in ("headline", "summary"):
        value = profile.get(key)
        if isinstance(value, str) and value.strip():
            fields[f"/{key}"] = value
    groups = {
        "skills": ("name", "evidence"),
        "work_experience": ("job_title", "highlights"),
        "education": ("field",),
        "languages": ("name",),
    }
    for group, keys in groups.items():
        for index, item in enumerate(profile.get(group, [])):
            for key in keys:
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    fields[f"/{group}/{index}/{key}"] = value
                elif isinstance(value, list):
                    for subindex, entry in enumerate(value):
                        if isinstance(entry, str) and entry.strip():
                            fields[f"/{group}/{index}/{key}/{subindex}"] = entry
    return fields


def _apply_translatable_field(profile: dict[str, Any], pointer: str, value: str) -> None:
    parts = pointer.strip("/").split("/")
    target: Any = profile
    for part in parts[:-1]:
        target = target[int(part)] if isinstance(target, list) else target[part]
    leaf = parts[-1]
    if isinstance(target, list):
        target[int(leaf)] = value
    else:
        target[leaf] = value
