"""Rúbrica v1: Groq entrega juicios atómicos y este módulo combina pesos."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import math

from ien_contracts.models import (
    MATCH_DIMENSIONS,
    MatchBreakdown,
    MatchDimensionBreakdown,
    MatchResult,
    RequirementAssessment,
    ScoringConfig,
)


WEIGHTS = {
    "mandatory_skills": 0.45,
    "experience": 0.25,
    "education_and_languages": 0.15,
    "preferences_and_desirables": 0.15,
}
RUBRIC_PROMPT_VERSION = 'match-rubric.v1'
RUBRIC_PROMPT = (Path(__file__).resolve().parents[1] / 'prompts' / 'match-rubric.v1.md').read_text(encoding='utf-8').strip()


def questions() -> dict[str, Any]:
    levels = ["Sin evidencia de adecuación", "Adecuación limitada", "Adecuación parcial", "Adecuación clara", "Adecuación completa"]
    return {
        "mandatory_skills": {"type": "score", "instructions": [RUBRIC_PROMPT, "Evaluá habilidades obligatorias usando únicamente evidencia del perfil del candidato."], "criteria": levels},
        "experience": {"type": "score", "instructions": [RUBRIC_PROMPT, "Evaluá si la experiencia documentada cumple lo solicitado. La falta de evidencia no demuestra incumplimiento."], "criteria": levels},
        "education_and_languages": {"type": "score", "instructions": [RUBRIC_PROMPT, "Evaluá únicamente los estudios e idiomas solicitados de forma explícita."], "criteria": levels},
        "preferences_and_desirables": {"type": "score", "instructions": [RUBRIC_PROMPT, "Evaluá modalidad, ubicación y requisitos no obligatorios sin volverlos excluyentes."], "criteria": levels},
        "exclusionary_requirements": {"type": "noul", "instructions": [RUBRIC_PROMPT, "¿La evidencia del perfil respalda todos los requisitos obligatorios excluyentes? Respondé sí solo con evidencia."], "criteria": {"true": "Todos los requisitos excluyentes están respaldados", "false": "Al menos uno no está respaldado o falta evidencia"}},
    }


def _score(answer: dict[str, Any]) -> float:
    value = float(answer.get("score", 0))
    return min(1.0, max(0.0, value / 4.0))


def _confidence(answer: dict[str, Any]) -> float:
    return min(1.0, max(0.0, float(answer.get("confidence", 0))))


def _assess_requirements(candidate: dict[str, Any], offer: dict[str, Any]) -> tuple[list[RequirementAssessment], float]:
    skills = {str(item.get("name", "")).casefold(): item for item in candidate.get("skills", [])}
    levels = {"unknown": 0, "beginner": 1, "intermediate": 2, "advanced": 3, "expert": 4}
    assessments: list[RequirementAssessment] = []
    known = 0
    requirements = offer.get("requirements", [])
    for requirement in requirements:
        name = str(requirement["name"])
        found = skills.get(name.casefold())
        mandatory = bool(requirement.get("mandatory", True))
        if found is None:
            assessments.append(RequirementAssessment(name=name, score=0, status="unknown", mandatory=mandatory, explanation="No se encontró evidencia de este requisito en el perfil."))
            continue
        known += 1
        expected = levels.get(requirement.get("minimum_level", "unknown"), 0)
        actual = levels.get(found.get("level", "unknown"), 0)
        score = 1.0 if expected == 0 else min(1.0, actual / expected)
        status = "met" if score >= 1 else "partial" if score > 0 else "not_met"
        assessments.append(RequirementAssessment(name=name, score=score, status=status, mandatory=mandatory, explanation="Evaluación basada en la habilidad registrada y su evidencia."))
    coverage = (known / len(requirements)) if requirements else 1.0
    return assessments, coverage


def build_result(request: dict[str, Any], provider_response: dict[str, Any]) -> MatchResult:
    if request.get("protocol_version") == "match.v2":
        return _build_v2_result(request, provider_response)
    answers = provider_response["answers"]
    for key in WEIGHTS:
        answer = answers.get(key, {})
        for field, maximum in [('score', 4), ('confidence', 1)]:
            value = answer.get(field)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 <= value <= maximum:
                raise ValueError(f'Invalid provider answer: {key}.{field}')
    weighted = sum(WEIGHTS[key] * _score(answers.get(key, {})) for key in WEIGHTS)
    assessments, coverage = _assess_requirements(request["candidate_profile"], request["job_offer"])
    exclusionary = [item for item in request["job_offer"].get("requirements", []) if item.get("exclusionary")]
    exclusionary_supported = float(answers.get("exclusionary_requirements", {}).get("noul", 0)) >= 0.5
    if exclusionary and not exclusionary_supported:
        weighted = min(weighted, 0.40)
    confidence_answers = [answers.get(key, {}) for key in WEIGHTS]
    confidence = sum(_confidence(answer) for answer in confidence_answers) / len(confidence_answers)
    return MatchResult(
        request_id=request["request_id"],
        match_percentage=round(weighted * 100, 2),
        coverage_percentage=round(coverage * 100, 2),
        confidence_percentage=round(confidence * 100, 2),
        provider_model=provider_response.get("model"),
        assessments=assessments,
        summary="La compatibilidad se calcula con la evidencia documentada del perfil y los criterios de la oferta.",
    )


_V2_SCORE = {"met": 1.0, "partial": 0.5, "not_met": 0.0, "unknown": 0.0}


def candidate_evidence_index(profile: dict[str, Any]) -> dict[str, str]:
    """Índice de hechos con referencias JSON Pointer estables al snapshot."""
    evidence: dict[str, str] = {}

    def add(pointer: str, value: Any) -> None:
        if isinstance(value, (str, int, float)) and not isinstance(value, bool) and str(value).strip():
            evidence[pointer] = str(value)

    for key in ("summary", "headline"):
        add(f"/{key}", profile.get(key))
    groups = {
        "skills": ("name", "level", "years_experience", "kind", "origin", "concept_id", "evidence", "source_quotes"),
        "work_experience": ("job_title", "organization", "duration_months", "highlights"),
        "projects": ("name", "highlights", "source_quotes"),
        "education": ("level", "field", "institution", "completed"),
        "languages": ("name", "level"),
        "credentials": ("name", "issuer", "issued_at", "expires_at", "evidence", "source_quotes"),
    }
    for group, fields in groups.items():
        for index, item in enumerate(profile.get(group, [])):
            for field in fields:
                value = item.get(field)
                if isinstance(value, list):
                    for subindex, subvalue in enumerate(value):
                        add(f"/{group}/{index}/{field}/{subindex}", subvalue)
                else:
                    add(f"/{group}/{index}/{field}", value)
    for key in ("locations", "work_modes"):
        for index, value in enumerate(profile.get("preferences", {}).get(key, [])):
            add(f"/preferences/{key}/{index}", value)
    return evidence


def _build_v2_result(request: dict[str, Any], provider_response: dict[str, Any]) -> MatchResult:
    """Calcula match.v2 a partir de juicios atómicos; el proveedor nunca elige pesos."""
    config = ScoringConfig.model_validate(request.get("scoring_config"))
    requirements = request["job_offer"].get("requirements", [])
    ids = [item.get("id") for item in requirements]
    if any(not item_id for item_id in ids) or len(ids) != len(set(ids)):
        raise ValueError("Los requisitos de match.v2 deben tener identificadores únicos.")
    if any(item.get("category") not in MATCH_DIMENSIONS for item in requirements):
        raise ValueError("Cada requisito de match.v2 debe tener una categoría válida.")
    if any(not item.get("mandatory", True) and item.get("category") != "desirables" for item in requirements):
        raise ValueError("Todo requisito no obligatorio debe clasificarse como deseable.")

    raw_assessments = provider_response.get("assessments")
    if not isinstance(raw_assessments, list):
        raise ValueError("La evaluación del proveedor no contiene juicios por requisito.")
    expected_ids = set(ids)
    if any(not isinstance(item, dict) for item in raw_assessments):
        raise ValueError("La evaluación del proveedor contiene un juicio inválido.")
    returned_ids = [item.get("requirement_id") for item in raw_assessments]
    if len(returned_ids) != len(set(returned_ids)) or set(returned_ids) != expected_ids:
        raise ValueError("La evaluación del proveedor no coincide con los requisitos de la oferta.")

    candidate_evidence = candidate_evidence_index(request["candidate_profile"])
    by_id = {item["requirement_id"]: item for item in raw_assessments}
    assessments: list[RequirementAssessment] = []
    scores_by_dimension: dict[str, list[float]] = {name: [] for name in MATCH_DIMENSIONS}
    mandatory_states: list[str] = []
    for requirement in requirements:
        requirement_id = requirement["id"]
        category = requirement["category"]
        raw = by_id[requirement_id]
        state = raw.get("status")
        if state not in _V2_SCORE:
            raise ValueError(f"El juicio del requisito {requirement_id} tiene un estado inválido.")
        explanation = raw.get("explanation")
        evidence_refs = raw.get("evidence_refs", [])
        if not isinstance(explanation, str) or not explanation.strip() or len(explanation) > 500:
            raise ValueError(f"El juicio del requisito {requirement_id} no incluye una explicación válida.")
        if not isinstance(evidence_refs, list) or len(evidence_refs) > 8 or any(
            not isinstance(ref, str) or not ref or ref not in candidate_evidence for ref in evidence_refs
        ):
            raise ValueError(f"El juicio del requisito {requirement_id} cita evidencia que no está en el perfil.")
        if state in {"met", "partial", "not_met"} and not evidence_refs:
            raise ValueError(f"El juicio del requisito {requirement_id} necesita evidencia para acreditar este estado.")
        if state in {"met", "partial", "not_met"} and category in {"technical_skills", "soft_skills"} and not any(
            ref.startswith(("/skills/", "/work_experience/", "/projects/")) for ref in evidence_refs
        ):
            state = "unknown"
            evidence_refs = []
            explanation = "La formación o el resumen no acreditan por sí solos esta habilidad en el perfil."
        if state == "unknown":
            evidence_refs = []
        mandatory = bool(requirement.get("mandatory", True)) and category != "desirables"
        if mandatory:
            mandatory_states.append(state)
        scores_by_dimension[category].append(_V2_SCORE[state])
        assessments.append(RequirementAssessment(
            requirement_id=requirement_id,
            name=requirement["name"],
            score=_V2_SCORE[state],
            status=state,
            mandatory=mandatory,
            explanation=explanation.strip(),
            category=category,
            source=requirement.get("source"),
            evidence_refs=evidence_refs,
        ))

    applicable = {key: bool(scores_by_dimension[key]) for key in MATCH_DIMENSIONS}
    weighted_dimensions = [key for key in MATCH_DIMENSIONS if applicable[key] and config.weights[key] > 0]
    applicable_weight = sum(config.weights[key] for key in weighted_dimensions)
    if not weighted_dimensions or applicable_weight <= 0:
        raise ValueError("No hay criterios suficientes para calcular compatibilidad.")
    dimensions: dict[str, MatchDimensionBreakdown] = {}
    total = 0.0
    for key in MATCH_DIMENSIONS:
        is_applicable = applicable[key]
        score = sum(scores_by_dimension[key]) / len(scores_by_dimension[key]) if scores_by_dimension[key] else 0.0
        effective = (config.weights[key] / applicable_weight * 10000) if key in weighted_dimensions else 0.0
        dimensions[key] = MatchDimensionBreakdown(
            score=round(score, 6),
            configured_weight_bp=config.weights[key],
            effective_weight_bp=round(effective, 4),
            applicable=is_applicable,
        )
    for assessment in assessments:
        category = assessment.category.value if hasattr(assessment.category, 'value') else assessment.category
        if category in weighted_dimensions:
            possible = 100 * config.weights[category] / applicable_weight / len(scores_by_dimension[category])
            assessment.points_possible = round(possible, 4)
            assessment.points_awarded = round(possible * assessment.score, 4)
            total += assessment.points_awarded
        else:
            assessment.points_possible = 0.0
            assessment.points_awarded = 0.0

    known_count = sum(1 for item in assessments if item.status != "unknown")
    coverage = known_count / len(requirements) if requirements else 1.0
    if any(state in {"partial", "not_met"} for state in mandatory_states):
        mandatory_status = "not_met"
    elif any(state == "unknown" for state in mandatory_states):
        mandatory_status = "unknown"
    else:
        mandatory_status = "all_met"

    return MatchResult(
        schema_version="match-result.v2",
        request_id=request["request_id"],
        match_percentage=round(total, 2),
        coverage_percentage=round(coverage * 100, 2),
        confidence_percentage=None,
        confidence_available=False,
        assessments=assessments,
        summary="La compatibilidad refleja los requisitos solicitados y la evidencia disponible. La falta de evidencia se informa por separado del incumplimiento.",
        breakdown=MatchBreakdown(**dimensions),
        scoring_config=config,
        mandatory_status=mandatory_status,
        provider_model=provider_response.get("model"),
    )
